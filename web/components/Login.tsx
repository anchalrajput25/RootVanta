"use client";

import { useState, type FormEvent } from "react";
import { api, startCompanySignIn } from "@/lib/api";

export default function Login({ message, onToken }: { message: string; onToken: (token: string) => Promise<void> }) {
  const [signup, setSignup] = useState(false);
  const [company, setCompanyMode] = useState(false); // sign in with the company's own account (SSO)
  const [companyName, setCompanyName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(ev: FormEvent) {
    ev.preventDefault();
    setBusy(true);
    setError("");
    try {
      if (company) {
        startCompanySignIn(email); // leaves the page
        return;
      }
      const res = signup ? await api.signup(companyName, email, password) : await api.login(email, password);
      await onToken(res.access_token);
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  }

  return (
    <div className="panel login">
      <h1>Rootvanta</h1>
      <p className="muted small">
        {signup
          ? "Create a new company. You become its admin."
          : "Finds the cause of an incident, proposes a safe fix, and proves it worked."}
      </p>
      <form onSubmit={submit}>
        {signup && (
          <label>
            Company name
            <input type="text" required autoFocus value={companyName} onChange={(e) => setCompanyName(e.target.value)} />
          </label>
        )}
        <label>
          Email
          <input type="email" required autoFocus={!signup} autoComplete="username" value={email}
            onChange={(e) => setEmail(e.target.value)} />
        </label>
        {!company && (
          <label>
            Password
            <input type="password" required minLength={signup ? 10 : undefined}
              autoComplete={signup ? "new-password" : "current-password"} value={password}
              onChange={(e) => setPassword(e.target.value)} />
          </label>
        )}
        <button className="primary" type="submit" disabled={busy}>
          {signup ? "Create company" : company ? "Continue to your company sign-in" : "Log in"}
        </button>
        <div className="error">{error || (!signup && message)}</div>
      </form>
      {!signup && (
        <p className="small">
          <a href="#" onClick={(e) => { e.preventDefault(); setCompanyMode(!company); setError(""); }}>
            {company ? "Use a password instead" : "Sign in with your company account"}
          </a>
        </p>
      )}
      <p className="small muted">
        {signup ? "Already have an account? " : "New company? "}
        <a href="#" onClick={(e) => { e.preventDefault(); setSignup(!signup); setCompanyMode(false); setError(""); }}>
          {signup ? "Log in" : "Create one"}
        </a>
      </p>
      {!signup && (
        <p className="small muted">
          Practice shop: run <code>python -m incident_agent.workflow.demo setup</code> once, then log in
          as <code>lead@demo-shop.test</code> (can approve) or <code>oncall@demo-shop.test</code>, password{" "}
          <code>demo-password-123</code>.
        </p>
      )}
    </div>
  );
}
