"use client";

// A checklist for a new company's admin, shown until the basics are set up.

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { SettingsTab } from "./settings/Settings";

interface Step {
  done: boolean;
  title: string;
  why: string;
  tab: SettingsTab;
}

export default function GettingStarted({ onOpen }: { onOpen: (tab: SettingsTab) => void }) {
  const [steps, setSteps] = useState<Step[] | null>(null);

  useEffect(() => {
    Promise.all([api.integrations(), api.alertSources(), api.users(), api.notificationTargets()])
      .then(([integrations, sources, users, targets]) => {
        const ok = (kind: string) => integrations.some((i) => i.kind === kind && i.connected);
        setSteps([
          { done: ok("prometheus"), title: "Connect your metrics (Prometheus)", tab: "monitoring",
            why: "The agent needs them to find causes and to prove fixes." },
          { done: sources.length > 0, title: "Send alerts here", tab: "alerts",
            why: "Incidents then open and get investigated by themselves." },
          { done: ok("service_admin"), title: "Connect a change system", tab: "monitoring",
            why: "Needed to apply approved fixes." },
          { done: users.length > 1, title: "Add your team", tab: "team",
            why: "A second person is needed to approve risky fixes." },
          { done: targets.length > 0, title: "Get notified", tab: "notifications",
            why: "A message when a fix needs approval or is undone." },
        ]);
      })
      .catch(() => setSteps(null)); // not an admin, or offline: just skip the checklist
  }, []);

  if (!steps || steps.every((s) => s.done)) return null;
  const left = steps.filter((s) => !s.done).length;
  return (
    <div className="panel card">
      <div className="row"><h3>Get started</h3><div className="spacer" />
        <span className="small muted">{steps.length - left} of {steps.length} done</span></div>
      <ul className="steps">
        {steps.map((s) => (
          <li key={s.title}>
            <span>{s.done ? "✓" : "○"}</span>
            <span className="grow">
              <b>{s.title}</b> <span className="muted small">{s.why}</span>
            </span>
            {!s.done && <button onClick={() => onOpen(s.tab)}>Set up</button>}
          </li>
        ))}
      </ul>
    </div>
  );
}
