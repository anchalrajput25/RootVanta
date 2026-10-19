"use client";

// The company's dependency map, learned from traces. Callers are on the left and the
// services they call on the right, so an outage usually reads left to right towards its cause.

import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { dateTime } from "@/lib/labels";
import type { ServiceMap } from "@/lib/types";
import { Badge, ErrorText, Spinner } from "./ui";

const NODE_W = 150;
const NODE_H = 34;
const COL_GAP = 90;
const ROW_GAP = 22;
const PAD = 16;
const POLL_MS = 3000;

type Layout = { pos: Record<string, { x: number; y: number }>; width: number; height: number };

// Each service goes one column right of its furthest caller (cycles are cut).
function layout(map: ServiceMap): Layout {
  const names = map.services.map((s) => s.name);
  const callers: Record<string, string[]> = Object.fromEntries(names.map((n) => [n, []]));
  for (const e of map.edges) if (e.caller !== e.callee) callers[e.callee]?.push(e.caller);
  const level: Record<string, number> = {};
  const visiting = new Set<string>();
  const depth = (n: string): number => {
    if (n in level) return level[n];
    if (visiting.has(n)) return 0; // a cycle: stop here
    visiting.add(n);
    const d = Math.max(-1, ...callers[n].map(depth)) + 1;
    visiting.delete(n);
    level[n] = d;
    return d;
  };
  names.forEach(depth);
  const columns: string[][] = [];
  for (const n of [...names].sort()) (columns[level[n]] ??= []).push(n);
  const pos: Layout["pos"] = {};
  columns.forEach((col, c) => col.forEach((n, r) => {
    pos[n] = { x: PAD + c * (NODE_W + COL_GAP), y: PAD + r * (NODE_H + ROW_GAP) };
  }));
  const rows = Math.max(1, ...columns.map((c) => c?.length ?? 0));
  return {
    pos,
    width: PAD * 2 + columns.length * NODE_W + Math.max(0, columns.length - 1) * COL_GAP,
    height: PAD * 2 + rows * NODE_H + (rows - 1) * ROW_GAP,
  };
}

function edgePath(a: { x: number; y: number }, b: { x: number; y: number }): string {
  const x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2, x2 = b.x, y2 = b.y + NODE_H / 2;
  if (x2 <= x1) { // same column or backwards (a cycle): loop around underneath
    const dip = Math.max(y1, y2) + NODE_H;
    return `M${x1},${y1} C${x1 + 40},${dip} ${x2 - 40},${dip} ${x2},${y2}`;
  }
  const mid = (x1 + x2) / 2;
  return `M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}`;
}

export default function ServiceMapView({ canRefresh, onOpenIncident }: {
  canRefresh: boolean;
  onOpenIncident: (id: string) => void;
}) {
  const [map, setMap] = useState<ServiceMap | null>(null);
  const [error, setError] = useState("");
  const [hover, setHover] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setMap(await api.serviceMap());
      setError("");
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  const updating = map?.last_refresh?.status === "queued" || map?.last_refresh?.status === "running";
  useEffect(() => {
    load();
    if (!updating) return;
    const timer = setInterval(load, POLL_MS);
    return () => clearInterval(timer);
  }, [load, updating]);

  const shape = useMemo(() => (map ? layout(map) : null), [map]);

  async function refresh() {
    setError("");
    try {
      await api.refreshServiceMap();
    } catch (e) {
      setError((e as Error).message);
    }
    load();
  }

  if (!map || !shape) {
    return <div className="settings wide-page"><div className="panel card muted">{error || <><Spinner /> Loading…</>}</div></div>;
  }
  const near = new Set<string>();
  if (hover) {
    near.add(hover);
    for (const e of map.edges) if (e.caller === hover || e.callee === hover) { near.add(e.caller); near.add(e.callee); }
  }
  const maxCalls = Math.max(1, ...map.edges.map((e) => e.calls));
  const last = map.last_refresh;

  return (
    <div className="settings wide-page">
      <div className="panel card">
        <div className="row">
          <h3>Service map</h3>
          <div className="spacer" />
          {updating && <span className="small muted"><Spinner /> Reading traces…</span>}
          {canRefresh && <button disabled={updating} onClick={refresh}>Update from traces</button>}
        </div>
        <p className="muted small intro">
          Which service calls which, learned from your traces. Every investigation adds what it sees, and
          &ldquo;Update from traces&rdquo; reads the last hour for every service. Alerts from services next to each
          other on this map, a few minutes apart, are grouped into one incident.
          {map.updated_at && <> Last seen: {dateTime(map.updated_at)}.</>}
        </p>
        {last?.status === "failed" && <ErrorText text={`The last update failed: ${last.error ?? "unknown error"}`} />}
        <ErrorText text={error} />
        {map.services.length === 0 ? (
          <p className="muted">
            Nothing learned yet. Connect Jaeger in Settings &gt; Monitoring, then press &ldquo;Update from traces&rdquo;.
          </p>
        ) : (
          <>
            <div className="legend small muted" style={{ marginTop: 10 }}>
              <span><span className="key" />calls (thicker = more)</span>
              <span><span className="key errors" />some calls failed</span>
              <span><span className="key box" />open incident (click to open)</span>
            </div>
            <div className="map-wrap">
              <svg width={shape.width} height={shape.height} role="img" aria-label="Service dependency map">
                <defs>
                  <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="9" markerHeight="9"
                    markerUnits="userSpaceOnUse" orient="auto-start-reverse">
                    <path d="M0,0 L10,5 L0,10 z" fill="context-stroke" />
                  </marker>
                </defs>
                {map.edges.map((e) => {
                  const a = shape.pos[e.caller], b = shape.pos[e.callee];
                  if (!a || !b) return null;
                  const faded = hover && !(e.caller === hover || e.callee === hover);
                  return (
                    <path key={`${e.caller}>${e.callee}`} d={edgePath(a, b)} markerEnd="url(#arrow)"
                      className={`map-edge${e.error_calls > 0 ? " errors" : ""}${faded ? " faded" : ""}`}
                      strokeWidth={1.2 + 2.8 * (e.calls / maxCalls)}>
                      <title>{`${e.caller} calls ${e.callee}: ${e.calls} calls in the last sample` +
                        (e.error_calls ? `, ${e.error_calls} failed` : "")}</title>
                    </path>
                  );
                })}
                {map.services.map((s) => {
                  const p = shape.pos[s.name];
                  if (!p) return null;
                  const cls = ["map-node", s.open_incident_id ? "incident clickable" : "",
                    hover && !near.has(s.name) ? "faded" : ""].join(" ");
                  return (
                    <g key={s.name} className={cls} transform={`translate(${p.x},${p.y})`}
                      onMouseEnter={() => setHover(s.name)} onMouseLeave={() => setHover(null)}
                      onClick={() => s.open_incident_id && onOpenIncident(s.open_incident_id)}>
                      <rect width={NODE_W} height={NODE_H} rx={6} />
                      <text x={10} y={NODE_H / 2 + 4}>
                        {s.name.length > 16 ? `${s.name.slice(0, 15)}…` : s.name}
                      </text>
                      {s.open_incident_severity && (
                        <text x={NODE_W - 10} y={NODE_H / 2 + 4} textAnchor="end" className="small">
                          {s.open_incident_severity}
                        </text>
                      )}
                      <title>{s.open_incident_id ? `${s.name}: open incident (${s.open_incident_severity})` : s.name}</title>
                    </g>
                  );
                })}
              </svg>
            </div>
            <p className="small muted">
              {map.services.length} services, {map.edges.length} connections.{" "}
              {map.services.some((s) => s.open_incident_id) &&
                <Badge tone="bad" text={`${map.services.filter((s) => s.open_incident_id).length} with open incidents`} />}
            </p>
          </>
        )}
      </div>
    </div>
  );
}
