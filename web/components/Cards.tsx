// The four cards for one incident: what it is, what happened, the proposed fix, and the proof.
//
// Report text can quote log lines written by anyone. It is only ever rendered as
// text (React escapes it), never as HTML.

import {
  CAUSE, CONCLUSION, dateTime, describeAction, INCIDENT_STATUS, PLAN_STATUS, time, VERDICT_TONE,
} from "@/lib/labels";
import type { Evidence, Incident, IncidentAlert, IncidentDetail, Plan, Preview, SimilarIncident } from "@/lib/types";
import { Badge, ErrorText, Spinner, StatusBadge } from "./ui";

export function IncidentCard({ incident: i, alerts }: { incident: Incident; alerts: IncidentAlert[] }) {
  const joined = alerts.filter((a) => a.reason !== "opened the incident");
  const services = [...new Set(alerts.map((a) => a.service))];
  return (
    <div className="panel card">
      <p className="head-title">{i.title}</p>
      <div className="row small">
        <StatusBadge map={INCIDENT_STATUS} value={i.status} />
        <Badge text={i.severity} />
        <Badge text={i.environment} />
        <span className="muted">service {i.service} · started {dateTime(i.started_at)}</span>
        {joined.length > 0 && (
          <Badge tone="info" text={`${alerts.length} alerts, ${services.length} service${services.length > 1 ? "s" : ""}`} />
        )}
      </div>
      {joined.length > 0 && (
        <details>
          <summary>Alerts grouped into this incident ({alerts.length})</summary>
          <table>
            <thead><tr><th>Time</th><th>Service</th><th>Alert</th><th>Why it is here</th></tr></thead>
            <tbody>
              {alerts.map((a) => (
                <tr key={a.id}>
                  <td className="muted">{time(a.received_at)}</td>
                  <td>{a.service}</td>
                  <td>{a.alertname} <span className="muted small">{a.severity}</span></td>
                  <td className="muted">{a.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}
    </div>
  );
}

const OUTCOME_TONE: Record<string, "good" | "warn" | "bad"> = {
  verified: "good", partially_verified: "warn", unverified: "warn",
  rolled_back: "bad", apply_failed: "bad", undo_failed: "bad", interrupted: "bad",
};

export function SeenBeforeCard({ similar, onOpen }: { similar: SimilarIncident[]; onOpen: (id: string) => void }) {
  if (similar.length === 0) return null;
  const sameCause = similar.filter((s) => s.match === "same cause").length;
  return (
    <div className="panel card">
      <h3>Seen before</h3>
      <p className="muted small">
        {sameCause > 0
          ? `${sameCause} earlier incident${sameCause > 1 ? "s" : ""} had the same cause. What was done then:`
          : "Earlier incidents on the same service:"}
      </p>
      <ul className="steps">
        {similar.map((s) => (
          <li key={s.incident_id}>
            <span className="t">{dateTime(s.at)}</span>
            <div className="grow">
              <button className="link" onClick={() => onOpen(s.incident_id)}>{s.title}</button>
              <div className="small">
                {CAUSE[s.cause] ?? s.cause} in {s.cause_service}. Fix: {s.fix_text}.
              </div>
            </div>
            {s.outcome && <Badge tone={OUTCOME_TONE[s.outcome] ?? ""} text={PLAN_STATUS[s.outcome]?.[0] ?? s.outcome} />}
            {s.match === "same service" && <Badge text="same service" />}
          </li>
        ))}
      </ul>
    </div>
  );
}

function EvidenceChip({ id, evidence }: { id: string; evidence: Record<string, Evidence> }) {
  return <span className="chip" title={evidence[id]?.summary ?? ""}>{id}</span>;
}

export function InvestigationCard({ detail, canStart, busy, error, onInvestigate }: {
  detail: IncidentDetail;
  canStart: boolean;
  busy: boolean;
  error?: string;
  onInvestigate: (incidentId: string) => void;
}) {
  const inv = detail.investigation;
  const running = inv?.status === "queued" || inv?.status === "running";
  const head = (
    <div className="row">
      <h3>1. What happened</h3>
      <div className="spacer" />
      {canStart && (
        <button disabled={running || busy} onClick={() => onInvestigate(detail.incident.id)}>
          {inv ? "Investigate again" : "Investigate"}
        </button>
      )}
    </div>
  );

  if (!inv || running || inv.status === "failed" || !inv.report) {
    return (
      <div className="panel card">
        {head}
        {!inv ? (
          <p className="muted">No investigation yet.</p>
        ) : running ? (
          <p><Spinner /> The agent is collecting metrics, logs and traces…</p>
        ) : (
          <p className="error">{inv.summary || "The investigation failed. The worker\u2019s log shows why."}</p>
        )}
        <ErrorText text={error} />
      </div>
    );
  }

  const r = inv.report;
  const evidence = Object.fromEntries(r.evidence.map((e) => [e.id, e]));
  return (
    <div className="panel card">
      {head}
      <div className="row">
        <StatusBadge map={CONCLUSION} value={r.conclusion} />
        <span className="small muted">
          checked {time(r.window_start)}–{time(r.window_end)} · engine: {r.engine}
          {r.duration_seconds != null && <> · took {r.duration_seconds.toFixed(1)} s</>}
          {r.ai_usage && <> · AI cost {r.ai_usage.priced ? `$${r.ai_usage.cost_usd.toFixed(4)}` : "unknown"}</>}
        </span>
      </div>
      <p>{r.summary}</p>
      {r.notes.map((n, k) => <div key={k} className="note small">{n}</div>)}
      {r.hypotheses.map((hy, n) => {
        const pct = Math.round(hy.confidence * 100);
        return (
          <div key={n} className="cause">
            <div className="row">
              <b>{n + 1}. {CAUSE[hy.cause] ?? hy.cause}</b>
              <span className="muted">in {hy.service}</span>
              <div className="spacer" />
              <div className="bar" title={`${pct}% confident`}><span style={{ width: `${pct}%` }} /></div>
              <b>{pct}%</b>
            </div>
            <div>{hy.statement}</div>
            <div className="row small muted">
              Supported by:
              {hy.supporting_evidence.map((id) => <EvidenceChip key={id} id={id} evidence={evidence} />)}
              {hy.contradicting_evidence.length > 0 && (
                <>
                  · Against:
                  {hy.contradicting_evidence.map((id) => <EvidenceChip key={id} id={id} evidence={evidence} />)}
                </>
              )}
            </div>
          </div>
        );
      })}
      {r.timeline.length > 0 && (
        <details>
          <summary>Timeline ({r.timeline.length})</summary>
          <ul className="steps">
            {r.timeline.map((t, k) => (
              <li key={k}>
                <span className="t">{time(t.at)}</span>
                <span>{t.description} <EvidenceChip id={t.evidence_id} evidence={evidence} /></span>
              </li>
            ))}
          </ul>
        </details>
      )}
      {r.evidence.length > 0 && (
        <details>
          <summary>Evidence ({r.evidence.length})</summary>
          <table>
            <thead>
              <tr><th>Id</th><th>Kind</th><th>From</th><th>Service</th><th>What it shows</th></tr>
            </thead>
            <tbody>
              {r.evidence.map((e) => (
                <tr key={e.id}>
                  <td><span className="chip">{e.id}</span></td>
                  <td>{e.kind}</td><td>{e.source}</td><td>{e.service}</td><td>{e.summary}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}
      <ErrorText text={error} />
    </div>
  );
}

export function FixCard({ detail, canAct, canApprove, busy, error, preview, onPracticeRun, onDecide, onRunFix }: {
  detail: IncidentDetail;
  canAct: boolean;
  canApprove: boolean;
  busy: boolean;
  error?: string;
  preview?: Preview;
  onPracticeRun: (planId: string) => void;
  onDecide: (planId: string, decision: "approved" | "rejected") => void;
  onRunFix: (plan: Plan) => void;
}) {
  const plan = detail.plan;
  const report = detail.investigation?.report;
  if (!plan) {
    return (
      <div className="panel card">
        <h3>2. Proposed fix</h3>
        <p className="muted">
          {report && report.conclusion !== "cause_identified"
            ? "No fix is proposed, because the cause is not clear enough. A person should look."
            : "No fix proposed yet."}
        </p>
      </div>
    );
  }

  const showPractice = canAct && !["blocked", "rejected"].includes(plan.status) && !detail.execution;
  const awaiting = plan.status === "awaiting_approval";
  const showRun = plan.status === "ready" && canAct;
  const rules = plan.policy_decision?.results ?? [];

  return (
    <div className="panel card">
      <div className="row">
        <h3>2. Proposed fix</h3>
        <div className="spacer" />
        <StatusBadge map={PLAN_STATUS} value={plan.status} />
      </div>
      <p><b>{describeAction(plan.action)}</b></p>
      {report?.proposal && <p className="muted">{report.proposal.rationale}</p>}
      <p className="small muted">
        Expected: {plan.expected_effect} · risk {plan.action.risk} ·{" "}
        {plan.action.reversible ? "can be undone" : "cannot be undone"}
      </p>
      {(showPractice || awaiting || showRun) && (
        <div className="row">
          {busy && <Spinner />}
          {showPractice && <button disabled={busy} onClick={() => onPracticeRun(plan.id)}>Practice run</button>}
          {awaiting && canApprove && (
            <>
              <button className="primary" disabled={busy} onClick={() => onDecide(plan.id, "approved")}>Approve</button>
              <button className="danger" disabled={busy} onClick={() => onDecide(plan.id, "rejected")}>Reject</button>
            </>
          )}
          {awaiting && !canApprove && (
            <span className="small muted">A person with the approver role must approve this fix.</span>
          )}
          {showRun && <button className="primary" disabled={busy} onClick={() => onRunFix(plan)}>Run fix</button>}
        </div>
      )}
      {preview && (
        <div className="box small">
          <b>Practice run (nothing was changed):</b>
          <ul>{preview.changes.map((c, k) => <li key={k}>{c}</li>)}</ul>
          <div className="muted">Undo would be: {describeAction(preview.undo)}</div>
        </div>
      )}
      <ErrorText text={error} />
      {rules.length > 0 && (
        <details>
          <summary>Safety rules ({rules.filter((c) => c.passed).length}/{rules.length} passed)</summary>
          <ul className="steps small">
            {rules.map((c) => (
              <li key={c.rule_id}>
                <span>{c.passed ? "✓" : "✗"}</span>
                <span><code>{c.rule_id}</code> {c.reason}</span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

export function ProofCard({ detail }: { detail: IncidentDetail }) {
  const exe = detail.execution;
  if (!exe) {
    return (
      <div className="panel card">
        <h3>3. Fix and proof</h3>
        <p className="muted">Nothing has been changed yet. After the fix runs, each check on real metrics shows here.</p>
      </div>
    );
  }
  const live = exe.status === "queued" || exe.status === "executing";
  return (
    <div className="panel card">
      <div className="row">
        <h3>3. Fix and proof</h3>
        <div className="spacer" />
        {live && <Spinner />}
        <StatusBadge map={PLAN_STATUS} value={exe.status} />
      </div>
      {live && (
        <p className="small muted">
          The fix goes to a small share of traffic first and is checked before it reaches everyone.
        </p>
      )}
      <ul className="steps">
        {exe.steps.map((s, k) => (
          <li key={k}><span className="t">{time(s.at)}</span><span>{s.message}</span></li>
        ))}
      </ul>
      {[...detail.verifications].reverse().map((v) => (
        <div key={v.id} className="box">
          <div className="row">
            <b>Check after stage {v.verdict.stage ?? ""}</b>
            <Badge text={v.status.toUpperCase().replace("_", " ")} tone={VERDICT_TONE[v.status] ?? ""} />
          </div>
          <div className="small">{v.verdict.summary}</div>
          <details>
            <summary>Checks</summary>
            <table>
              <thead>
                <tr><th>Check</th><th>Needed</th><th>Result</th><th>Seen</th><th>Why</th></tr>
              </thead>
              <tbody>
                {(v.verdict.checks ?? []).map((c) => (
                  <tr key={c.check_id}>
                    <td><code>{c.check_id}</code></td>
                    <td>{c.required ? "yes" : "no"}</td>
                    <td>{c.outcome}</td>
                    <td>{c.observed === null || c.observed === undefined ? "–" : Number(c.observed).toPrecision(3)}</td>
                    <td>{c.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
        </div>
      ))}
    </div>
  );
}
