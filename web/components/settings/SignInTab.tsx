"use client";

// Company sign-in (SSO): people from the company's email domain sign in with their work account.

import { useEffect, useState, type FormEvent } from "react";
import { api } from "@/lib/api";
import type { Role, SsoConfig } from "@/lib/types";
import { Badge, ErrorText, Spinner } from "../ui";
import CopyBox from "./CopyBox";

const ROLES: [Role, string][] = [
  ["viewer", "Viewer: can only look"],
  ["responder", "Responder: investigates and runs fixes"],
  ["approver", "Approver: approves fixes"],
];

export default function SignInTab() {
  const [config, setConfig] = useState<SsoConfig | null>(null);
  const [domain, setDomain] = useState("");
  const [issuer, setIssuer] = useState("");
  const [clientId, setClientId] = useState("");
  const [secret, setSecret] = useState("");
  const [role, setRole] = useState<Role>("viewer");
  const [enforce, setEnforce] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; message: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  function show(c: SsoConfig) {
    setConfig(c);
    setDomain(c.domain);
    setIssuer(c.issuer);
    setClientId(c.client_id);
    setRole(c.default_role);
    setEnforce(c.enforce);
  }

  useEffect(() => {
    api.sso().then(show).catch((e) => setError((e as Error).message));
  }, []);

  async function run(fn: () => Promise<void>) {
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

  const save = (ev: FormEvent) => {
    ev.preventDefault();
    run(async () => {
      show(await api.saveSso({ domain, issuer, client_id: clientId, client_secret: secret || null,
        default_role: role, enforce }));
      setSecret("");
      setResult(await api.testSso());
    });
  };

  if (!config) return error ? <ErrorText text={error} /> : <div className="panel card muted"><Spinner /> Loading…</div>;
  return (
    <div className="detail">
      <p className="muted small intro">
        Let people sign in with their work account (Google Workspace, Microsoft Entra ID, Okta, or any OpenID
        Connect provider) instead of a separate password. The first time someone signs in, their account is made
        with the role you choose below.
      </p>
      <form className="panel card" onSubmit={save}>
        <div className="row">
          <h3>Company sign-in</h3>
          <div className="spacer" />
          {config.configured ? <Badge text={config.enforce ? "Required" : "On"} tone="good" /> : <Badge text="Off" />}
        </div>
        <ol className="small muted howto">
          <li>In your provider, create a web app (OpenID Connect) and add this sign-in address:</li>
        </ol>
        <CopyBox label="Redirect address to register" text={config.redirect_uri} />
        <ol className="small muted howto" start={2}>
          <li>Copy its issuer address, client ID and client secret here.</li>
        </ol>
        <div className="form-grid">
          <label className="field">
            Email domain
            <input required value={domain} placeholder="acme.com" onChange={(e) => setDomain(e.target.value)} />
          </label>
          <label className="field wide">
            Issuer address
            <input type="url" required value={issuer} placeholder="https://accounts.google.com"
              onChange={(e) => setIssuer(e.target.value)} />
          </label>
          <label className="field">
            Client ID
            <input required value={clientId} autoComplete="off" onChange={(e) => setClientId(e.target.value)} />
          </label>
          <label className="field">
            Client secret
            <input type="password" value={secret} autoComplete="new-password" required={!config.has_secret}
              placeholder={config.has_secret ? "Saved. Type to replace it" : ""} onChange={(e) => setSecret(e.target.value)} />
          </label>
          <label className="field">
            Role for new people
            <select value={role} onChange={(e) => setRole(e.target.value as Role)}>
              {ROLES.map(([r, label]) => <option key={r} value={r}>{label}</option>)}
            </select>
          </label>
        </div>
        <label className="check">
          <input type="checkbox" checked={enforce} onChange={(e) => setEnforce(e.target.checked)} />
          Require it: only admins may still use a password (so a provider outage cannot lock everyone out)
        </label>
        {result && <p className={`small ${result.ok ? "ok-text" : "error"}`}>{result.message}</p>}
        <div className="row">
          <button className="primary" type="submit" disabled={busy}>Save and test</button>
          {config.configured && (
            <button type="button" className="danger" disabled={busy} onClick={() => {
              if (!window.confirm("Turn off company sign-in? People will need passwords again.")) return;
              run(async () => {
                await api.removeSso();
                show(await api.sso());
                setResult(null);
              });
            }}>Turn off</button>
          )}
          {busy && <Spinner />}
        </div>
        <ErrorText text={error} />
      </form>
    </div>
  );
}
