"use client";

// Settings for admins: everything a new company needs to start using the agent.

import { useState } from "react";
import AlertsTab from "./AlertsTab";
import MonitoringTab from "./MonitoringTab";
import NotificationsTab from "./NotificationsTab";
import SignInTab from "./SignInTab";
import TeamTab from "./TeamTab";

const TABS = [
  ["monitoring", "Monitoring"],
  ["alerts", "Alerts"],
  ["team", "Team"],
  ["notifications", "Notifications"],
  ["signin", "Sign-in"],
] as const;

export type SettingsTab = (typeof TABS)[number][0];

export default function Settings({ initialTab = "monitoring", meId }: { initialTab?: SettingsTab; meId: string }) {
  const [tab, setTab] = useState<SettingsTab>(initialTab);
  return (
    <main className="settings">
      <nav className="tabs" aria-label="Settings sections">
        {TABS.map(([key, label]) => (
          <button key={key} className={tab === key ? "tab active" : "tab"} onClick={() => setTab(key)}
            aria-current={tab === key ? "page" : undefined}>
            {label}
          </button>
        ))}
      </nav>
      {tab === "monitoring" && <MonitoringTab />}
      {tab === "alerts" && <AlertsTab />}
      {tab === "team" && <TeamTab meId={meId} />}
      {tab === "notifications" && <NotificationsTab />}
      {tab === "signin" && <SignInTab />}
    </main>
  );
}
