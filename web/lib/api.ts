// A small client for the backend. The token lives in sessionStorage, so closing
// the tab logs you out.

import type {
  AlertSource, AlertSourceCreated, Execution, IncidentAlert, IncidentDetail, Incident, Integration, IntegrationIn,
  IntegrationKind, Investigation, Job, Me, NotificationTarget, Plan, Preview, Role, ServiceMap, SimilarIncident,
  SsoConfig, SsoConfigIn, Stats, Team, User, Verification,
} from "./types";

const TOKEN_KEY = "incident-agent-token";
export const LOGGED_OUT_EVENT = "incident-agent-logged-out";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export function savedToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function saveToken(token: string | null): void {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    // storage blocked: the session just won't survive a reload
  }
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const token = savedToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let res: Response;
  try {
    res = await fetch(`/api/v1${path}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    throw new ApiError(0, "Cannot reach the server. Is the app (uvicorn) running?");
  }
  if (res.status === 401 && token) {
    // The token expired or was revoked: drop it and tell the page to show the login form.
    saveToken(null);
    window.dispatchEvent(new Event(LOGGED_OUT_EVENT));
    throw new ApiError(401, "Your session ended. Please log in again.");
  }
  const text = await res.text();
  let data: any = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (!res.ok) {
    let msg = data?.detail;
    if (Array.isArray(msg)) msg = msg.map((d: { msg: string }) => d.msg).join("; ");
    if (!msg && res.status >= 500) msg = "The server had a problem. Is the app (uvicorn) running?";
    throw new ApiError(res.status, msg || `Request failed (${res.status})`);
  }
  return data as T;
}

export const api = {
  login: (email: string, password: string) =>
    call<{ access_token: string }>("POST", "/auth/login", { email, password }),
  signup: (organization_name: string, email: string, password: string) =>
    call<{ access_token: string }>("POST", "/auth/signup", { organization_name, email, password }),
  me: () => call<Me>("GET", "/auth/me"),
  incidents: () => call<Incident[]>("GET", "/incidents"),
  stats: () => call<Stats>("GET", "/stats"),
  investigate: (incidentId: string) =>
    call<Investigation>("POST", `/incidents/${incidentId}/investigations`, { minutes: 15 }),
  dryRun: (planId: string) => call<Preview>("POST", `/plans/${planId}/dry-run`),
  decide: (planId: string, decision: "approved" | "rejected") =>
    call<unknown>("POST", `/plans/${planId}/approvals`, { decision }),
  execute: (planId: string) => call<Execution>("POST", `/plans/${planId}/execute`),
  serviceMap: () => call<ServiceMap>("GET", "/service-map"),
  refreshServiceMap: () => call<Job>("POST", "/service-map/refresh"),

  // settings (admins)
  integrations: () => call<Integration[]>("GET", "/integrations"),
  saveIntegration: (kind: IntegrationKind, body: IntegrationIn) =>
    call<Integration>("PUT", `/integrations/${kind}`, body),
  testIntegration: (kind: IntegrationKind) => call<Integration>("POST", `/integrations/${kind}/test`),
  removeIntegration: (kind: IntegrationKind) => call<null>("DELETE", `/integrations/${kind}`),
  teams: () => call<Team[]>("GET", "/teams"),
  users: () => call<User[]>("GET", "/users"),
  addUser: (email: string, password: string, roles: Role[], team_ids: string[]) =>
    call<User>("POST", "/users", { email, password, roles, team_ids }),
  setRoles: (userId: string, roles: Role[]) => call<User>("PUT", `/users/${userId}/roles`, { roles }),
  deactivate: (userId: string) => call<User>("POST", `/users/${userId}/deactivate`),
  activate: (userId: string) => call<User>("POST", `/users/${userId}/activate`),
  alertSources: () => call<AlertSource[]>("GET", "/alert-sources"),
  addAlertSource: (name: string, team_id: string, environment: string) =>
    call<AlertSourceCreated>("POST", "/alert-sources", { name, team_id, environment }),
  revokeAlertSource: (id: string) => call<null>("DELETE", `/alert-sources/${id}`),
  notificationTargets: () => call<NotificationTarget[]>("GET", "/notification-targets"),
  addNotificationTarget: (url: string) => call<NotificationTarget>("POST", "/notification-targets", { url }),
  testNotificationTarget: (id: string) =>
    call<{ ok: boolean; message: string }>("POST", `/notification-targets/${id}/test`),
  removeNotificationTarget: (id: string) => call<null>("DELETE", `/notification-targets/${id}`),
  sso: () => call<SsoConfig>("GET", "/sso"),
  saveSso: (body: SsoConfigIn) => call<SsoConfig>("PUT", "/sso", body),
  testSso: () => call<{ ok: boolean; message: string }>("POST", "/sso/test"),
  removeSso: () => call<null>("DELETE", "/sso"),

  async detail(incidentId: string): Promise<IncidentDetail> {
    const [incident, investigations, alerts, similar] = await Promise.all([
      call<Incident>("GET", `/incidents/${incidentId}`),
      call<Investigation[]>("GET", `/incidents/${incidentId}/investigations`),
      call<IncidentAlert[]>("GET", `/incidents/${incidentId}/alerts`),
      call<SimilarIncident[]>("GET", `/incidents/${incidentId}/similar`),
    ]);
    const investigation = investigations[0] ?? null;
    const base = { incident, investigation, alerts, similar };
    if (!investigation?.plan_id) return { ...base, plan: null, execution: null, verifications: [] };
    const planId = investigation.plan_id;
    const [plan, execution, verifications] = await Promise.all([
      call<Plan>("GET", `/plans/${planId}`),
      call<Execution>("GET", `/plans/${planId}/execution`).catch((e) => {
        if (e instanceof ApiError && e.status === 404) return null; // not run yet
        throw e;
      }),
      call<Verification[]>("GET", `/plans/${planId}/verifications`),
    ]);
    return { ...base, plan, execution, verifications };
  },
};

// Company sign-in: the browser goes to the company's provider and comes back with a token in the URL fragment.
export function startCompanySignIn(email: string): void {
  window.location.assign(`/api/v1/auth/sso/start?email=${encodeURIComponent(email)}`);
}

export function takeSignInResult(): { token?: string; error?: string } {
  const hash = window.location.hash.slice(1);
  if (!hash.includes("sso_")) return {};
  const params = new URLSearchParams(hash);
  history.replaceState(null, "", window.location.pathname + window.location.search); // keep the token out of history
  return { token: params.get("sso_token") ?? undefined, error: params.get("sso_error") ?? undefined };
}
