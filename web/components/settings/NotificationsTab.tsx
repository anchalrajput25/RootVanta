"use client";

import { useEffect, useState, type FormEvent } from "react";
import { api } from "@/lib/api";
import { dateTime } from "@/lib/labels";
import type { NotificationTarget } from "@/lib/types";
import { ErrorText, Spinner } from "../ui";

export default function NotificationsTab() {
  const [targets, setTargets] = useState<NotificationTarget[] | null>(null);
  const [url, setUrl] = useState("");
  const [results, setResults] = useState<Record<string, { ok: boolean; message: string }>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = () => api.notificationTargets().then(setTargets);

  useEffect(() => {
    load().catch((e) => setError((e as Error).message));
  }, []);

  async function run(fn: () => Promise<unknown>) {
    setBusy(true);
    setError("");
    try {
      await fn();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function add(ev: FormEvent) {
    ev.preventDefault();
    run(async () => {
      const t = await api.addNotificationTarget(url.trim());
      setUrl("");
      await load();
      const r = await api.testNotificationTarget(t.id);
      setResults((all) => ({ ...all, [t.id]: r }));
    });
  }

  return (
    <div className="detail">
      <p className="muted small intro">
        The agent posts a short message when a fix needs approval, and when a fix is proven or undone. Any
        Slack-style incoming webhook works (Slack, Mattermost, Microsoft Teams workflows, and others).
      </p>
      <form className="panel card" onSubmit={add}>
        <h3>Add a channel</h3>
        <label className="field">
          Incoming webhook address
          <input type="url" required value={url} placeholder="https://hooks.slack.com/services/…"
            onChange={(e) => setUrl(e.target.value)} />
        </label>
        <div className="row">
          <button className="primary" type="submit" disabled={busy}>Add and send a test</button>
          {busy && <Spinner />}
        </div>
        <ErrorText text={error} />
      </form>
      <div className="panel card">
        <h3>Channels</h3>
        {!targets ? <p className="muted"><Spinner /> Loading…</p> : targets.length === 0 ? (
          <p className="muted small">None yet.</p>
        ) : (
          <table>
            <thead><tr><th>Address</th><th>Added</th><th /></tr></thead>
            <tbody>
              {targets.map((t) => (
                <tr key={t.id}>
                  <td>
                    <code>{t.url_hint}</code>
                    {results[t.id] && (
                      <div className={`small ${results[t.id].ok ? "ok-text" : "error"}`}>{results[t.id].message}</div>
                    )}
                  </td>
                  <td>{dateTime(t.created_at)}</td>
                  <td>
                    <div className="row">
                      <button disabled={busy} onClick={() => run(async () => {
                        const r = await api.testNotificationTarget(t.id);
                        setResults((all) => ({ ...all, [t.id]: r }));
                      })}>Send test</button>
                      <button className="danger" disabled={busy}
                        onClick={() => window.confirm("Remove this channel?") && run(async () => {
                          await api.removeNotificationTarget(t.id);
                          await load();
                        })}>Remove</button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
