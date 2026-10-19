"use client";

import { useEffect, useState, type FormEvent } from "react";
import { api } from "@/lib/api";
import type { Role, Team, User } from "@/lib/types";
import { Badge, ErrorText, Spinner } from "../ui";
import CopyBox from "./CopyBox";

const ROLES: [Role, string][] = [
  ["admin", "Admin: settings and people"],
  ["approver", "Approver: approves fixes"],
  ["responder", "Responder: investigates and runs fixes"],
  ["viewer", "Viewer: can only look"],
];

function temporaryPassword() {
  const bytes = new Uint8Array(12);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"[b % 55]).join("");
}

function RolePicker({ value, onChange }: { value: Role[]; onChange: (r: Role[]) => void }) {
  return (
    <div className="checks">
      {ROLES.map(([role, label]) => (
        <label key={role} className="check">
          <input type="checkbox" checked={value.includes(role)}
            onChange={(e) => onChange(e.target.checked ? [...value, role] : value.filter((r) => r !== role))} />
          {label}
        </label>
      ))}
    </div>
  );
}

export default function TeamTab({ meId }: { meId: string }) {
  const [users, setUsers] = useState<User[] | null>(null);
  const [teams, setTeams] = useState<Team[]>([]);
  const [email, setEmail] = useState("");
  const [roles, setRoles] = useState<Role[]>(["responder"]);
  const [added, setAdded] = useState<{ email: string; password: string } | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [editRoles, setEditRoles] = useState<Role[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function load() {
    const [u, t] = await Promise.all([api.users(), api.teams()]);
    setUsers(u);
    setTeams(t);
  }

  useEffect(() => {
    load().catch((e) => setError((e as Error).message));
  }, []);

  async function run(fn: () => Promise<unknown>) {
    setBusy(true);
    setError("");
    try {
      await fn();
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function add(ev: FormEvent) {
    ev.preventDefault();
    const password = temporaryPassword();
    run(async () => {
      await api.addUser(email, password, roles, teams[0] ? [teams[0].id] : []);
      setAdded({ email: email.trim().toLowerCase(), password });
      setEmail("");
    });
  }

  return (
    <div className="detail">
      {added && (
        <div className="panel card">
          <div className="row"><h3>{added.email} can now log in</h3><Badge text="Shown only once" tone="warn" /></div>
          <p className="small">Send them this address and temporary password in a private message.</p>
          <CopyBox label="Log-in details"
            text={`${typeof window === "undefined" ? "" : window.location.origin}\nEmail: ${added.email}\nPassword: ${added.password}`} />
        </div>
      )}
      <form className="panel card" onSubmit={add}>
        <h3>Add a person</h3>
        <label className="field">
          Work email
          <input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
        </label>
        <RolePicker value={roles} onChange={setRoles} />
        <div className="row">
          <button className="primary" type="submit" disabled={busy || roles.length === 0}>Add</button>
          {busy && <Spinner />}
        </div>
        <p className="small muted">A temporary password is made for them. Nobody can approve their own fix.</p>
        <ErrorText text={error} />
      </form>
      <div className="panel card">
        <h3>People</h3>
        {!users ? <p className="muted"><Spinner /> Loading…</p> : (
          <table>
            <thead><tr><th>Email</th><th>Roles</th><th>Status</th><th /></tr></thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.id}>
                  <td>{u.email}{u.id === meId && <span className="muted"> (you)</span>}</td>
                  <td>
                    {editing === u.id ? (
                      <>
                        <RolePicker value={editRoles} onChange={setEditRoles} />
                        <div className="row">
                          <button className="primary" disabled={busy || editRoles.length === 0}
                            onClick={() => run(async () => { await api.setRoles(u.id, editRoles); setEditing(null); })}>
                            Save
                          </button>
                          <button onClick={() => setEditing(null)}>Cancel</button>
                        </div>
                      </>
                    ) : (
                      <div className="row">{u.roles.map((r) => <Badge key={r} text={r} />)}</div>
                    )}
                  </td>
                  <td>{u.is_active ? <Badge text="Active" tone="good" /> : <Badge text="Removed" />}</td>
                  <td>
                    <div className="row">
                      {editing !== u.id && (
                        <button onClick={() => { setEditing(u.id); setEditRoles(u.roles); }}>Change roles</button>
                      )}
                      {u.id !== meId && (u.is_active ? (
                        <button className="danger" disabled={busy}
                          onClick={() => window.confirm(`Remove access for ${u.email}?`) && run(() => api.deactivate(u.id))}>
                          Remove access
                        </button>
                      ) : (
                        <button disabled={busy} onClick={() => run(() => api.activate(u.id))}>Give access back</button>
                      ))}
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
