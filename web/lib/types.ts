// Shapes returned by the FastAPI backend (src/incident_agent/api/schemas.py).

export type Role = "admin" | "approver" | "responder" | "viewer";

export interface Me {
  id: string;
  org_id: string;
  email: string;
  roles: Role[];
  team_ids: string[];
}

export interface Incident {
  id: string;
  team_id: string;
  title: string;
  service: string;
  environment: string;
  severity: string;
  status: string;
  started_at: string;
  created_by: string;
}

export interface Action {
  type: string;
  service: string;
  environment?: string;
  params: Record<string, any>;
  risk: string;
  reversible: boolean;
  blast_radius_pct?: number;
}

export interface Evidence {
  id: string;
  kind: string;
  source: string;
  service: string;
  observed_at: string;
  locator: string;
  summary: string;
}

export interface Hypothesis {
  cause: string;
  service: string;
  statement: string;
  confidence: number;
  supporting_evidence: string[];
  contradicting_evidence: string[];
}

export interface Report {
  service: string;
  window_start: string;
  window_end: string;
  engine: string;
  conclusion: "cause_identified" | "insufficient_evidence" | "no_anomaly";
  summary: string;
  evidence: Evidence[];
  timeline: { at: string; description: string; evidence_id: string }[];
  hypotheses: Hypothesis[];
  proposal: { action: Action; rationale: string } | null;
  notes: string[];
  duration_seconds?: number;
  ai_usage?: { model: string; input_tokens: number; output_tokens: number; cost_usd: number; priced: boolean } | null;
}

export interface Investigation {
  id: string;
  incident_id: string;
  job_id: string;
  status: "queued" | "running" | "done" | "failed";
  conclusion: string | null;
  summary: string | null;
  plan_id: string | null;
  report: Report | null;
  created_at: string;
}

export interface Plan {
  id: string;
  incident_id: string;
  action: Action;
  expected_effect: string;
  requested_by: string;
  status: string;
  policy_decision: { allowed?: boolean; requires_approval?: boolean; results?: { rule_id: string; passed: boolean; reason: string }[] };
}

export interface Preview {
  action: Action;
  before: Record<string, any>;
  after: Record<string, any>;
  changes: string[];
  undo: Action;
  no_op: boolean;
}

export interface Execution {
  id: string;
  plan_id: string;
  status: string;
  steps: { at: string; kind: string; message: string }[];
  created_at: string;
  finished_at: string | null;
}

export interface Check {
  check_id: string;
  kind: string;
  required: boolean;
  outcome: string;
  reason: string;
  observed: number | null;
}

export interface Verification {
  id: string;
  plan_id: string;
  status: string;
  verdict: { stage?: string; summary?: string; checks?: Check[] };
  created_at: string;
}

export interface Stats {
  incidents_by_status: Record<string, number>;
  fixes_by_result: Record<string, number>;
  investigations_done: number;
  median_investigation_seconds: number | null;
  p95_investigation_seconds: number | null;
  ai_cost_usd_this_month: number;
  ai_investigations_this_month: number;
}

export interface IncidentDetail {
  incident: Incident;
  investigation: Investigation | null;
  plan: Plan | null;
  execution: Execution | null;
  verifications: Verification[];
  alerts: IncidentAlert[];
  similar: SimilarIncident[];
}

// --- what the agent learns over time ------------------------------------------------

export interface IncidentAlert {
  id: string;
  service: string;
  alertname: string;
  severity: string;
  source: string;
  reason: string; // "opened the incident", "same service", "checkout calls payments"
  received_at: string;
}

export interface SimilarIncident {
  incident_id: string;
  title: string;
  at: string;
  match: "same cause" | "same service";
  cause: string;
  cause_service: string;
  statement: string;
  fix: { type: string; service: string; params: Record<string, unknown> } | null;
  outcome: string | null;
  fix_text: string;
}

export interface Job {
  id: string;
  kind: string;
  status: string;
  attempts: number;
  error: string | null;
  created_at: string;
  finished_at: string | null;
}

export interface ServiceMap {
  services: { name: string; open_incident_id: string | null; open_incident_severity: string | null }[];
  edges: { caller: string; callee: string; calls: number; error_calls: number; last_seen: string }[];
  updated_at: string | null;
  last_refresh: Job | null;
}

// --- settings ---------------------------------------------------------------------

export type IntegrationKind = "prometheus" | "loki" | "jaeger" | "argocd" | "kubernetes" | "service_admin";

export interface Integration {
  kind: IntegrationKind;
  connected: boolean;
  url: string;
  endpoints: Record<string, string>;
  username: string | null;
  has_token: boolean;
  last_test_ok: boolean | null;
  last_test_message: string | null;
  last_tested_at: string | null;
  updated_at: string | null;
}

export interface IntegrationIn {
  url?: string;
  endpoints?: Record<string, string>;
  username?: string | null;
  token?: string | null; // null keeps the saved token, "" removes it
}

export interface Team {
  id: string;
  name: string;
}

export interface User {
  id: string;
  email: string;
  roles: Role[];
  team_ids: string[];
  is_active: boolean;
}

export interface AlertSource {
  id: string;
  name: string;
  team_id: string;
  environment: string;
  created_at: string;
}

export interface AlertSourceCreated extends AlertSource {
  token: string;
}

export interface NotificationTarget {
  id: string;
  url_hint: string;
  created_at: string;
}

export interface SsoConfig {
  configured: boolean;
  domain: string;
  issuer: string;
  client_id: string;
  has_secret: boolean;
  default_role: Role;
  enforce: boolean;
  redirect_uri: string;
  updated_at: string | null;
}

export interface SsoConfigIn {
  domain: string;
  issuer: string;
  client_id: string;
  client_secret: string | null; // null keeps the saved one
  default_role: Role;
  enforce: boolean;
}
