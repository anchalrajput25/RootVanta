"use client";

import { useEffect, useState, type FormEvent } from "react";
import { api } from "@/lib/api";
import { dateTime } from "@/lib/labels";
import type { AlertSource, AlertSourceCreated, Team } from "@/lib/types";
import { Badge, ErrorText, Spinner } from "../ui";
import CopyBox from "./CopyBox";

export default function AlertsTab() {
  const [sources, setSources] = useState<AlertSource[] | null>(null);
  const [teams, setTeams] = useState<Team[]>([]);
  const [created, setCreated] = useState<AlertSourceCreated | null>(null);
  const [name, setName] = useState("Production Alertmanager");
  const [teamId, setTeamId] = useState("");
  const [environment, setEnvironment] = useState("production");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function load() {
    const [s, t] = await Promise.all([api.alertSources(), api.teams()]);
    setSources(s);
    setTeams(t);
    setTeamId((current) => current || t[0]?.id || "");
  }

  useEffect(() => {
    load().catch((e) => setError((e as Error).message));
  }, []);

  async function create(ev: FormEvent) {
    ev.preventDefault();
    setBusy(true);
    setError("");
    try {
      setCreated(await api.addAlertSource(name, teamId, environment));
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function revoke(s: AlertSource) {
    if (!window.confirm(`Revoke "${s.name}"? Alerts sent with its token will be refused.`)) return;
    try {
      await api.revokeAlertSource(s.id);
      if (created?.id === s.id) setCreated(null);
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  const hookUrl = typeof window === "undefined" ? "" : `${window.location.origin}/api/v1/alerts/alertmanager`;
  const teamName = (id: string) => teams.find((t) => t.id === id)?.name ?? id;

  return (
    <div className="detail">
      <p className="muted small intro">
        Let your monitoring open incidents by itself. Each alert needs a <code>service</code> label; alerts for a
        service that already has an open incident are grouped into it.
      </p>
      {created && (
        <div className="panel card">
          <div className="row"><h3>Connect &quot;{created.name}&quot;</h3><Badge text="Shown only once" tone="warn" /></div>
          <p className="small">Copy the token now. Only a fingerprint of it is stored, so it cannot be shown again.</p>
          <CopyBox label="Webhook address" text={hookUrl} />
          <CopyBox label="Token" text={created.token} />
          <CopyBox label="Add this to alertmanager.yml" text={alertmanagerSnippet(hookUrl, created.token)} />
          <p className="small muted">
            For Grafana alerting, add a webhook contact point with the same address, and set the authorization
            header to <code>Bearer</code> plus the token.
          </p>
        </div>
      )}
      <form className="panel card" onSubmit={create}>
        <h3>Add an alert source</h3>
        <div className="form-grid">
          <label className="field wide">
            Name
            <input required maxLength={200} value={name} onChange={(e) => setName(e.target.value)} />
          </label>
          <label className="field">
            Team that handles its incidents
            <select value={teamId} onChange={(e) => setTeamId(e.target.value)}>
              {teams.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
            </select>
          </label>
          <label className="field">
            Environment
            <select value={environment} onChange={(e) => setEnvironment(e.target.value)}>
              <option value="production">Production</option>
              <option value="staging">Staging</option>
              <option value="development">Development</option>
            </select>
          </label>
        </div>
        <div className="row">
          <button className="primary" type="submit" disabled={busy || !teamId}>Create token</button>
          {busy && <Spinner />}
        </div>
        <ErrorText text={error} />
      </form>
      <div className="panel card">
        <h3>Alert sources</h3>
        {!sources ? <p className="muted"><Spinner /> Loading…</p> : sources.length === 0 ? (
          <p className="muted small">None yet.</p>
        ) : (
          <table>
            <thead><tr><th>Name</th><th>Team</th><th>Environment</th><th>Created</th><th /></tr></thead>
            <tbody>
              {sources.map((s) => (
                <tr key={s.id}>
                  <td>{s.name}</td><td>{teamName(s.team_id)}</td><td>{s.environment}</td>
                  <td>{dateTime(s.created_at)}</td>
                  <td><button className="danger" onClick={() => revoke(s)}>Revoke</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

function alertmanagerSnippet(url: string, token: string) {
  return `receivers:
  - name: incident-agent
    webhook_configs:
      - url: ${url}
        send_resolved: false
        http_config:
          authorization:
            credentials: ${token}`;
}
