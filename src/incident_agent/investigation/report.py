"""Turn an Investigation into a report people can read (Markdown) or tools can use (JSON)."""

from __future__ import annotations

import json
from typing import Any

from incident_agent.domain.models import RecoveryPlan, Role, User
from incident_agent.investigation.models import Investigation
from incident_agent.safety.policy import OrgPolicy, PolicyDecision, evaluate_plan

TITLES = {
    "cause_identified": "Cause found",
    "insufficient_evidence": "Not enough evidence to name a cause",
    "no_anomaly": "Nothing abnormal found",
}


def policy_preview(inv: Investigation) -> PolicyDecision | None:
    """Run the proposed fix through the default safety policy, as a responder would request it."""
    if inv.proposal is None:
        return None
    requester = User(org_id="preview", team_ids=frozenset({"preview"}), email="responder@preview",
                     roles=frozenset({Role.RESPONDER}))
    plan = RecoveryPlan(org_id="preview", team_id="preview", incident_id="preview", action=inv.proposal.action,
                        expected_effect=inv.proposal.rationale, requested_by=requester.id)
    return evaluate_plan(plan, requester, OrgPolicy.default("preview"))


def _ai_cost(inv: Investigation) -> str:
    u = inv.ai_usage
    if u is None:
        return ""
    cost = f"${u.cost_usd:.4f}" if u.priced else "cost unknown (model not in the price table)"
    return f" AI step: {u.model}, {u.input_tokens:,} tokens in, {u.output_tokens:,} out, {cost}."


def to_markdown(inv: Investigation) -> str:
    out = [f"# {TITLES[inv.conclusion]}: {inv.service} ({inv.environment})", "",
           inv.summary, "",
           f"Window: {inv.window_start:%Y-%m-%d %H:%M:%S} to {inv.window_end:%H:%M:%S} UTC. Engine: {inv.engine}. "
           f"Took {inv.duration_seconds:.1f} s.{_ai_cost(inv)}", ""]

    if inv.hypotheses:
        out += ["## Likely causes", ""]
        for i, h in enumerate(inv.hypotheses, 1):
            against = f"; against: {', '.join(h.contradicting_evidence)}" if h.contradicting_evidence else ""
            out.append(f"{i}. **{h.cause.value}** in {h.service}, {h.confidence:.0%} confident. {h.statement} "
                       f"(evidence: {', '.join(h.supporting_evidence)}{against})")
        out.append("")

    if inv.proposal:
        a = inv.proposal.action
        params = ", ".join(f"{k}={v}" for k, v in a.params.items())
        out += ["## Proposed fix", "", inv.proposal.rationale, "",
                f"Action: `{a.type.value}` on {a.service} ({params}). Risk {a.risk.value}, "
                f"blast radius {a.blast_radius_pct:g}%, reversible: {'yes' if a.reversible else 'no'}."]
        decision = policy_preview(inv)
        if decision:
            verdict = "allowed" if decision.allowed else "blocked"
            approval = ", needs a second person to approve" if decision.requires_approval else ""
            out.append(f"Safety policy: {verdict}{approval}.")
            out += [f"- blocked by {r.rule_id}: {r.reason}" for r in decision.violations]
        out += ["", "Nothing has been changed. After the fix is applied, the verifier checks the real "
                    "metrics to confirm it worked.", ""]
    elif inv.conclusion != "no_anomaly":
        out += ["## Proposed fix", "", "None. A human should decide the next step.", ""]

    if inv.timeline:
        out += ["## Timeline", ""]
        out += [f"- {t.at:%H:%M:%S} {t.description} ({t.evidence_id})" for t in inv.timeline]
        out.append("")

    out += ["## Evidence", ""]
    out += [f"- **{e.id}** [{e.source}] {e.summary}  \n  `{e.locator}`" for e in inv.evidence] or ["- none"]
    out.append("")

    if inv.notes:
        out += ["## Notes", ""] + [f"- {n}" for n in inv.notes] + [""]
    return "\n".join(out)


def to_json(inv: Investigation) -> dict[str, Any]:
    data = inv.model_dump(mode="json")
    decision = policy_preview(inv)
    data["policy_preview"] = decision.model_dump(mode="json") if decision else None
    return data


def dumps(inv: Investigation) -> str:
    return json.dumps(to_json(inv), indent=2)
