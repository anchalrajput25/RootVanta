"use client";

// The logged-in screen. It polls the API every few seconds so a running
// investigation or fix shows its progress without a reload.

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { describeAction } from "@/lib/labels";
import type { IncidentDetail, Incident, Me, Plan, Preview, Stats } from "@/lib/types";
import { FixCard, IncidentCard, InvestigationCard, ProofCard, SeenBeforeCard } from "./Cards";
import GettingStarted from "./GettingStarted";
import IncidentList from "./IncidentList";
import ServiceMapView from "./ServiceMap";
import Settings, { type SettingsTab } from "./settings/Settings";
import TopBar, { type View } from "./TopBar";
import { Spinner } from "./ui";

const POLL_MS = 4000;

export default function Dashboard({ me, onLogout }: { me: Me; onLogout: () => void }) {
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  // A link from a chat message (?incident=...) opens that incident.
  const [selected, setSelected] = useState<string | null>(() =>
    typeof window === "undefined" ? null : new URLSearchParams(window.location.search).get("incident"));
  const [detail, setDetail] = useState<IncidentDetail | null>(null);
  const [previews, setPreviews] = useState<Record<string, Preview>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<Record<string, boolean>>({});
  const [loadError, setLoadError] = useState("");
  const [view, setView] = useState<View>("incidents");
  const [settingsTab, setSettingsTab] = useState<SettingsTab>("monitoring");

  // The incident on screen right now, so a slow answer for an old selection is ignored.
  const selectedRef = useRef<string | null>(null);
  selectedRef.current = selected;

  const refresh = useCallback(async () => {
    try {
      const [list, s] = await Promise.all([api.incidents(), api.stats()]);
      setIncidents(list);
      setStats(s);
      const id = selectedRef.current ?? list[0]?.id ?? null;
      if (id !== selectedRef.current) {
        selectedRef.current = id;
        setSelected(id);
      }
      if (id) {
        const d = await api.detail(id);
        if (selectedRef.current === id) setDetail(d);
      }
      setLoadError("");
    } catch (e) {
      setLoadError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    if (view !== "incidents") return;
    refresh();
    const timer = setInterval(refresh, POLL_MS);
    return () => clearInterval(timer);
  }, [refresh, view]);

  function openSettings(tab: SettingsTab) {
    setSettingsTab(tab);
    setView("settings");
  }

  function select(id: string) {
    selectedRef.current = id;
    setSelected(id);
    setDetail(null);
    refresh();
  }

  async function act(key: string, fn: () => Promise<unknown>) {
    setBusy((b) => ({ ...b, [key]: true }));
    setErrors(({ [key]: _, ...rest }) => rest);
    try {
      await fn();
    } catch (e) {
      setErrors((errs) => ({ ...errs, [key]: (e as Error).message }));
    } finally {
      setBusy(({ [key]: _, ...rest }) => rest);
      await refresh();
    }
  }

  const can = (...roles: string[]) => me.roles.some((r) => roles.includes(r));

  const actions = {
    investigate: (incidentId: string) => act(incidentId, () => api.investigate(incidentId)),
    practiceRun: (planId: string) =>
      act(planId, async () => {
        const preview = await api.dryRun(planId);
        setPreviews((p) => ({ ...p, [planId]: preview }));
      }),
    decide: (planId: string, decision: "approved" | "rejected") => act(planId, () => api.decide(planId, decision)),
    runFix: (plan: Plan) => {
      const ok = window.confirm(
        `Run this fix now?\n\n${describeAction(plan.action)}\n\n` +
          "Each stage is checked with real metrics, and the fix is undone automatically if a check fails.",
      );
      if (ok) act(plan.id, () => api.execute(plan.id));
    },
  };

  return (
    <>
      <TopBar me={me} stats={stats} view={view} onView={(v) => { setSettingsTab("monitoring"); setView(v); }}
        onLogout={onLogout} />
      {loadError && <div className="pad error">{loadError}</div>}
      {view === "settings" ? <Settings key={settingsTab} initialTab={settingsTab} meId={me.id} /> :
       view === "map" ? (
        <ServiceMapView canRefresh={can("responder", "approver", "admin")}
          onOpenIncident={(id) => { setView("incidents"); select(id); }} />
      ) : (
      <main className="layout">
        <IncidentList incidents={incidents} selected={selected} onSelect={select} />
        <section className="detail">
          {can("admin") && <GettingStarted onOpen={openSettings} />}
          {!selected ? (
            incidents.length > 0 && <div className="panel card muted">Pick an incident on the left.</div>
          ) : !detail ? (
            <div className="panel card muted"><Spinner /> Loading…</div>
          ) : (
            <div className="detail" key={detail.incident.id}>
              <IncidentCard incident={detail.incident} alerts={detail.alerts} />
              <InvestigationCard detail={detail} canStart={can("responder", "approver")}
                busy={!!busy[detail.incident.id]} error={errors[detail.incident.id]}
                onInvestigate={actions.investigate} />
              <SeenBeforeCard similar={detail.similar} onOpen={select} />
              <FixCard detail={detail} canAct={can("responder", "approver")} canApprove={can("approver")}
                busy={detail.plan ? !!busy[detail.plan.id] : false}
                error={detail.plan ? errors[detail.plan.id] : undefined}
                preview={detail.plan ? previews[detail.plan.id] : undefined}
                onPracticeRun={actions.practiceRun} onDecide={actions.decide} onRunFix={actions.runFix} />
              <ProofCard detail={detail} />
            </div>
          )}
        </section>
      </main>
      )}
    </>
  );
}
