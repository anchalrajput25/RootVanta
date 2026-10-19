"use client";

import { useEffect, useState, type FormEvent } from "react";
import { api } from "@/lib/api";
import { dateTime } from "@/lib/labels";
import type { Integration, IntegrationKind } from "@/lib/types";
import { Badge, ErrorText, Spinner } from "../ui";

interface Info {
  title: string;
  what: string;
  placeholder: string;
  required?: boolean;
  // A list of services, one per line as name=value, instead of (service_admin) or as well as an address.
  list?: { label: string; placeholder: string };
  noAddress?: boolean;
  noUsername?: boolean;
}

const INFO: Record<IntegrationKind, Info> = {
  prometheus: {
    title: "Prometheus (metrics)",
    what: "Needed. The agent reads error rates and latency to find the cause and to prove a fix worked.",
    placeholder: "https://prometheus.example.com",
    required: true,
  },
  loki: {
    title: "Loki (logs)",
    what: "Optional. Lets the agent quote the exact error lines.",
    placeholder: "https://loki.example.com",
  },
  jaeger: {
    title: "Jaeger (traces)",
    what: "Optional. Lets the agent see which service a failing request depends on.",
    placeholder: "https://jaeger.example.com",
  },
  argocd: {
    title: "Argo CD (rollbacks)",
    what:
      "Optional. Rollbacks go through Argo CD, and its sync history shows the agent every deploy, even ones " +
      "no log mentions. The token needs get and sync rights. Auto-sync must be off for apps the agent rolls back.",
    placeholder: "https://argocd.example.com",
    required: true,
    noUsername: true,
    list: {
      label: "Services and their Argo CD application, one per line as service=application (leave empty if they have the same names)",
      placeholder: "checkout=shop-checkout",
    },
  },
  kubernetes: {
    title: "Kubernetes (scaling)",
    what:
      "Optional. Adding copies of a service goes through the Kubernetes API. The token needs get and patch on " +
      "deployments/scale only. Inside the cluster, use https://kubernetes.default.svc and leave the token empty.",
    placeholder: "https://kubernetes.default.svc",
    required: true,
    noUsername: true,
    list: { label: "Services and their Deployment, one per line as service=namespace/name", placeholder: "checkout=shop/checkout" },
  },
  service_admin: {
    title: "Practice or custom change system",
    what:
      "Optional. One admin address per service, each answering /admin/state like the practice shop. " +
      "Handles feature flags, and rollbacks or scaling when Argo CD or Kubernetes is not connected. " +
      "The token is sent as X-Admin-Token.",
    placeholder: "checkout=https://checkout.internal:8101",
    noAddress: true,
    list: { label: "Services, one per line as name=address", placeholder: "checkout=https://checkout.internal:8101" },
  },
};

export function connectionBadge(i: Integration) {
  if (!i.connected) return <Badge text="Not connected" />;
  if (i.last_test_ok === true) return <Badge text="Connected" tone="good" />;
  if (i.last_test_ok === false) return <Badge text="Test failed" tone="bad" />;
  return <Badge text="Saved, not tested" tone="warn" />;
}

export default function MonitoringTab() {
  const [items, setItems] = useState<Integration[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api.integrations().then(setItems).catch((e) => setError((e as Error).message));
  }, []);

  const replace = (next: Integration) => setItems((list) => list?.map((i) => (i.kind === next.kind ? next : i)) ?? null);

  if (error) return <ErrorText text={error} />;
  if (!items) return <div className="panel card muted"><Spinner /> Loading…</div>;
  return (
    <div className="detail">
      <p className="muted small intro">
        Connect your own monitoring. The agent only reads from it, and each company&apos;s connections are kept
        separate. Tokens are stored encrypted and are never shown again.
      </p>
      {items.map((i) => <ConnectionCard key={i.kind} item={i} onChange={replace} />)}
    </div>
  );
}

function toLines(endpoints: Record<string, string>) {
  return Object.entries(endpoints).map(([k, v]) => `${k}=${v}`).join("\n");
}

function fromLines(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of text.split("\n")) {
    const at = line.indexOf("=");
    if (at > 0) out[line.slice(0, at).trim()] = line.slice(at + 1).trim();
  }
  return out;
}

function ConnectionCard({ item, onChange }: { item: Integration; onChange: (i: Integration) => void }) {
  const info = INFO[item.kind];
  const services = !!info.noAddress;
  const [url, setUrl] = useState(item.url);
  const [lines, setLines] = useState(toLines(item.endpoints));
  const [username, setUsername] = useState(item.username ?? "");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function save(ev: FormEvent) {
    ev.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api.saveIntegration(item.kind, {
        url: services ? "" : url,
        endpoints: info.list ? fromLines(lines) : {},
        username: info.noUsername || services ? null : username || null,
        token: token ? token : null,
      });
      setToken("");
      onChange(await api.testIntegration(item.kind));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!window.confirm(`Disconnect ${info.title}?`)) return;
    setBusy(true);
    try {
      await api.removeIntegration(item.kind);
      onChange({ ...item, connected: false, url: "", endpoints: {}, username: null, has_token: false,
        last_test_ok: null, last_test_message: null, last_tested_at: null });
      setUrl("");
      setLines("");
      setUsername("");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="panel card" onSubmit={save}>
      <div className="row">
        <h3>{info.title}</h3>
        <div className="spacer" />
        {connectionBadge(item)}
      </div>
      <p className="small muted">{info.what}</p>
      {!services && (
        <div className="form-grid">
          <label className="field wide">
            Address
            <input type="url" required={info.required} value={url} placeholder={info.placeholder}
              onChange={(e) => setUrl(e.target.value)} />
          </label>
          {!info.noUsername && (
            <label className="field">
              Username <span className="muted">(only for basic sign-in)</span>
              <input value={username} autoComplete="off" onChange={(e) => setUsername(e.target.value)} />
            </label>
          )}
          <label className="field">
            Token or password <span className="muted">(optional)</span>
            <input type="password" value={token} autoComplete="new-password"
              placeholder={item.has_token ? "Saved. Type to replace it" : ""} onChange={(e) => setToken(e.target.value)} />
          </label>
        </div>
      )}
      {info.list && (
        <label className="field">
          {info.list.label}
          <textarea rows={3} value={lines} placeholder={info.list.placeholder} onChange={(e) => setLines(e.target.value)} />
        </label>
      )}
      {services && (
        <label className="field">
          Admin token
          <input type="password" value={token} autoComplete="new-password"
            placeholder={item.has_token ? "Saved. Type to replace it" : ""} onChange={(e) => setToken(e.target.value)} />
        </label>
      )}
      {item.last_test_message && (
        <p className={`small ${item.last_test_ok ? "ok-text" : "error"}`}>
          {item.last_test_message} <span className="muted">({dateTime(item.last_tested_at)})</span>
        </p>
      )}
      <div className="row">
        <button className="primary" type="submit" disabled={busy}>Save and test</button>
        {item.connected && (
          <>
            <button type="button" disabled={busy} onClick={async () => {
              setBusy(true);
              try { onChange(await api.testIntegration(item.kind)); } catch (e) { setError((e as Error).message); }
              finally { setBusy(false); }
            }}>Test again</button>
            <button type="button" className="danger" disabled={busy} onClick={remove}>Disconnect</button>
          </>
        )}
        {busy && <Spinner />}
      </div>
      <ErrorText text={error} />
    </form>
  );
}
