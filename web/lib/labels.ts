// Words people can read, for the codes the backend uses.

import type { Action } from "./types";

export type Tone = "good" | "warn" | "bad" | "info" | "";

export const PLAN_STATUS: Record<string, [string, Tone]> = {
  awaiting_approval: ["Waiting for approval", "warn"],
  ready: ["Approved, ready to run", "info"],
  queued: ["Queued", "info"],
  executing: ["Running", "info"],
  verified: ["Fixed and proven", "good"],
  partially_verified: ["Partly proven", "warn"],
  unverified: ["Not proven: not enough data", "warn"],
  rolled_back: ["Made things worse: undone automatically", "bad"],
  apply_failed: ["Could not apply: undone", "bad"],
  undo_failed: ["Undo failed: a person must fix it", "bad"],
  interrupted: ["Interrupted: check by hand", "bad"],
  blocked: ["Blocked by safety rules", "bad"],
  rejected: ["Rejected", ""],
};

export const INCIDENT_STATUS: Record<string, [string, Tone]> = {
  open: ["Open", "bad"],
  investigating: ["Investigating", "info"],
  mitigating: ["Fixing", "info"],
  verifying: ["Checking the fix", "info"],
  resolved: ["Resolved", "good"],
  closed: ["Closed", ""],
};

export const CONCLUSION: Record<string, [string, Tone]> = {
  cause_identified: ["Cause found", "good"],
  insufficient_evidence: ["Not enough evidence", "warn"],
  no_anomaly: ["Nothing unusual found", ""],
};

export const VERDICT_TONE: Record<string, Tone> = {
  verified: "good",
  partially_verified: "warn",
  unverified: "warn",
  failed: "bad",
};

export const CAUSE: Record<string, string> = {
  bad_deploy: "Bad deploy",
  dependency_failure: "A service it depends on is failing",
  saturation: "Overloaded",
};

export const OPEN_STATUSES = ["open", "investigating", "mitigating", "verifying"];

export function describeAction(action: Action): string {
  const p = action.params ?? {};
  switch (action.type) {
    case "rollback_deployment":
      return `Roll back ${action.service} to version ${p.target_version}`;
    case "toggle_feature_flag": {
      const onOff = p.value ? "on" : "off";
      return p.value && p.rollout_pct !== undefined && p.rollout_pct < 100
        ? `Turn ${p.flag} ${onOff} for ${p.rollout_pct}% of ${action.service} traffic first, then for everyone`
        : `Turn ${p.flag} ${onOff} for ${action.service}`;
    }
    case "scale_service":
      return `Scale ${action.service} to ${p.replicas} copies`;
    case "restart_service":
      return `Restart ${action.service}`;
    default:
      return `${action.type} on ${action.service}`;
  }
}

export function time(iso?: string | null): string {
  if (!iso) return "";
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function dateTime(iso?: string | null): string {
  if (!iso) return "";
  return new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}
