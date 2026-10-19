"""The investigation as a LangGraph pipeline.

    collect -> screen -> (no anomaly? -> finish)
            -> timeline -> reason (rules, then optional Claude) -> propose -> finish

Each step is a plain function over a shared state, so steps can be tested
alone and later run in background workers.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from incident_agent.domain.models import EvidenceKind
from incident_agent.investigation import llm, rules
from incident_agent.investigation.collectors import Collectors, collect
from incident_agent.investigation.models import (
    AIUsage,
    Evidence,
    Hypothesis,
    Investigation,
    Proposal,
    ServiceCall,
    TimelineEvent,
)
from incident_agent.investigation.rules import CONFIDENCE_THRESHOLD, Runbook

# Log text that reads like an order to an AI or an operator. It is never acted
# on; flagging it tells the humans someone may be probing the system.
SUSPICIOUS = re.compile(
    r"ignore (all|any|previous|prior) (instructions|rules)|disregard .*instructions|system prompt|"
    r"you are now|drop table|rm -rf|delete (the )?(database|db|cluster)|run_shell|delete_resource",
    re.IGNORECASE,
)


class State(TypedDict, total=False):
    service: str
    environment: str
    start: datetime
    end: datetime
    use_llm: bool
    model: str
    evidence: list[Evidence]
    notes: list[str]
    timeline: list[TimelineEvent]
    hypotheses: list[Hypothesis]
    summary: str
    engine: str
    proposal: Optional[Proposal]
    conclusion: str
    ai_usage: Optional[AIUsage]
    calls: list[ServiceCall]


def build_graph(collectors: Collectors, runbook: Runbook, llm_client: Optional[object] = None):
    def collect_step(s: State) -> State:
        book = collect(collectors, s["service"], s["start"], s["end"])
        return {"evidence": book.items, "notes": [*s.get("notes", []), *book.notes], "calls": book.calls}

    def screen_step(s: State) -> State:
        flagged = [e.id for e in s["evidence"] if SUSPICIOUS.search(e.summary + " " + str(e.facts))]
        if not flagged:
            return {}
        return {"notes": [*s["notes"], f"Log text that looks like instructions was found and ignored: "
                                       f"{', '.join(flagged)}. Only allowed actions can ever be proposed."]}

    def has_anomaly(s: State) -> str:
        return "timeline" if rules.anomalies(s["evidence"], s["service"]) else "finish"

    def timeline_step(s: State) -> State:
        return {"timeline": rules.build_timeline(s["evidence"], s["service"])}

    def reason_step(s: State) -> State:
        found = rules.rank_hypotheses(s["evidence"], s["service"])
        update: State = {"hypotheses": found, "engine": "rules", "summary": ""}
        if s.get("use_llm"):
            try:
                ai, summary, notes, usage = llm.claude_hypotheses(s["service"], s["evidence"], found,
                                                                  s.get("model", llm.DEFAULT_MODEL), llm_client)
            except llm.LLMUnavailable as e:
                update["notes"] = [*s["notes"], f"AI step skipped ({e}); used the rule engine."]
                update["ai_usage"] = e.usage
            else:
                update["ai_usage"] = usage
                update["notes"] = [*s["notes"], *notes]
                if ai:
                    update.update(hypotheses=ai, summary=summary, engine=f"claude:{s.get('model', llm.DEFAULT_MODEL)}")
                else:
                    update["notes"].append("The AI step found no supported cause; kept the rule engine's ranking.")
        return update

    def propose_step(s: State) -> State:
        hyps = s["hypotheses"]
        if not hyps or hyps[0].confidence < CONFIDENCE_THRESHOLD:
            return {"conclusion": "insufficient_evidence", "proposal": None}
        proposal, note = rules.propose_fix(hyps, s["evidence"], s["service"], s["environment"], runbook)
        return {"conclusion": "cause_identified", "proposal": proposal,
                "notes": [*s["notes"], note] if note else s["notes"]}

    def finish_step(s: State) -> State:
        measured = any(e.kind == EvidenceKind.METRIC and e.service == s["service"] for e in s["evidence"])
        if not measured:
            # Missing data is not good news: never report "healthy" without metrics.
            return {"conclusion": "insufficient_evidence", "hypotheses": [], "timeline": [], "engine": "rules",
                    "proposal": None,
                    "summary": f"No metrics could be read for {s['service']}, so it is unknown whether "
                               f"anything is wrong. See the notes."}
        if not rules.anomalies(s["evidence"], s["service"]):
            minutes = int((s["end"] - s["start"]).total_seconds() // 60)
            return {"conclusion": "no_anomaly", "hypotheses": [], "timeline": [], "engine": "rules",
                    "summary": f"No abnormal error rate, latency or CPU found for {s['service']} "
                               f"in the last {minutes} minutes."}
        if s.get("summary"):
            return {}
        hyps = s.get("hypotheses", [])
        if s["conclusion"] == "cause_identified":
            return {"summary": hyps[0].statement}
        if hyps:
            return {"summary": f"Not enough evidence to name a cause. Best guess ({hyps[0].confidence:.0%} "
                               f"confidence): {hyps[0].statement}"}
        return {"summary": "Something is wrong, but the evidence does not match a known cause "
                           "(bad deploy, failing dependency, or overload). A human should look."}

    g = StateGraph(State)
    for name, fn in [("collect", collect_step), ("screen", screen_step), ("timeline", timeline_step),
                     ("reason", reason_step), ("propose", propose_step), ("finish", finish_step)]:
        g.add_node(name, fn)
    g.add_edge(START, "collect")
    g.add_edge("collect", "screen")
    g.add_conditional_edges("screen", has_anomaly, {"timeline": "timeline", "finish": "finish"})
    g.add_edge("timeline", "reason")
    g.add_edge("reason", "propose")
    g.add_edge("propose", "finish")
    g.add_edge("finish", END)
    return g.compile()


def investigate(
    collectors: Collectors,
    service: str,
    minutes: int = 15,
    environment: str = "production",
    runbook: Optional[Runbook] = None,
    use_llm: bool = False,
    model: str = llm.DEFAULT_MODEL,
    end: Optional[datetime] = None,
    llm_client: Optional[object] = None,
) -> Investigation:
    began = time.perf_counter()
    end = end or datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes)
    graph = build_graph(collectors, runbook or Runbook(), llm_client)
    s = graph.invoke({"service": service, "environment": environment, "start": start, "end": end,
                      "use_llm": use_llm, "model": model, "notes": []})
    return Investigation(
        service=service, environment=environment, window_start=start, window_end=end,
        engine=s.get("engine", "rules"), conclusion=s["conclusion"], summary=s["summary"],
        evidence=tuple(s["evidence"]), timeline=tuple(s.get("timeline", [])),
        hypotheses=tuple(s.get("hypotheses", [])), proposal=s.get("proposal"), notes=tuple(s.get("notes", [])),
        duration_seconds=round(time.perf_counter() - began, 3), ai_usage=s.get("ai_usage"),
        service_calls=tuple(s.get("calls", [])),
    )
