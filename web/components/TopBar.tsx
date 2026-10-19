import { OPEN_STATUSES } from "@/lib/labels";
import type { Me, Stats } from "@/lib/types";

export type View = "incidents" | "map" | "settings";

export default function TopBar({ me, stats, view, onView, onLogout }: {
  me: Me;
  stats: Stats | null;
  view: View;
  onView: (v: View) => void;
  onLogout: () => void;
}) {
  const inc = stats?.incidents_by_status ?? {};
  const fixes = stats?.fixes_by_result ?? {};
  const open = OPEN_STATUSES.reduce((n, k) => n + (inc[k] ?? 0), 0);
  const undone = (fixes.rolled_back ?? 0) + (fixes.apply_failed ?? 0);
  const notProven = (fixes.unverified ?? 0) + (fixes.partially_verified ?? 0);
  return (
    <header className="top">
      <div className="brand">Rootvanta</div>
      <nav className="row">
        <button className={view === "incidents" ? "tab active" : "tab"} onClick={() => onView("incidents")}>
          Incidents
        </button>
        <button className={view === "map" ? "tab active" : "tab"} onClick={() => onView("map")}>
          Service map
        </button>
        {me.roles.includes("admin") && (
          <button className={view === "settings" ? "tab active" : "tab"} onClick={() => onView("settings")}>
            Settings
          </button>
        )}
      </nav>
      <div className="stats">
        <span className="stat" title="Incidents that are not resolved yet"><b>{open}</b> open</span>
        <span className="stat" title="Fixes whose checks passed on real metrics"><b>{fixes.verified ?? 0}</b> fixes proven</span>
        <span className="stat" title="Fixes that made things worse and were undone by the agent"><b>{undone}</b> undone automatically</span>
        <span className="stat" title="Fixes without enough data to prove them"><b>{notProven}</b> not fully proven</span>
        {stats?.median_investigation_seconds != null && (
          <span className="stat" title={`Typical time to find a cause, over ${stats.investigations_done} investigations ` +
            `(95% finish within ${stats.p95_investigation_seconds?.toFixed(1)} s)`}>
            <b>{stats.median_investigation_seconds.toFixed(1)} s</b> to investigate
          </span>
        )}
        {(stats?.ai_investigations_this_month ?? 0) > 0 && (
          <span className="stat" title={`AI cost this month, over ${stats!.ai_investigations_this_month} AI investigations`}>
            <b>${stats!.ai_cost_usd_this_month.toFixed(2)}</b> AI this month
          </span>
        )}
      </div>
      <div className="spacer" />
      <span className="small muted">{me.email} · {me.roles.join(", ")}</span>
      <button onClick={onLogout}>Log out</button>
    </header>
  );
}
