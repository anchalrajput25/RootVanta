import { dateTime, INCIDENT_STATUS } from "@/lib/labels";
import type { Incident } from "@/lib/types";
import { Badge, StatusBadge } from "./ui";

export default function IncidentList({ incidents, selected, onSelect }: {
  incidents: Incident[];
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <section className="panel">
      <div className="list-head">Incidents</div>
      {incidents.length === 0 && (
        <p className="muted pad small">No incidents yet. When monitoring sends an alert, an incident opens here by itself.</p>
      )}
      {incidents.map((i) => (
        <button key={i.id} className={`incident ${i.id === selected ? "selected" : ""}`} onClick={() => onSelect(i.id)}>
          <div className="row"><span className="title">{i.title}</span></div>
          <div className="row small muted">
            <StatusBadge map={INCIDENT_STATUS} value={i.status} />
            <Badge text={i.severity} />
            {i.service} · {dateTime(i.started_at)}
          </div>
        </button>
      ))}
    </section>
  );
}
