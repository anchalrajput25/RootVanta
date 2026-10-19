// Small pieces shared by the cards.

import type { Tone } from "@/lib/labels";

export function Badge({ text, tone = "" }: { text: string; tone?: Tone }) {
  return <span className={`badge ${tone}`}>{text}</span>;
}

export function StatusBadge({ map, value }: { map: Record<string, [string, Tone]>; value: string }) {
  const [text, tone] = map[value] ?? [value, ""];
  return <Badge text={text} tone={tone} />;
}

export function Spinner() {
  return <span className="spinner" />;
}

export function ErrorText({ text }: { text?: string }) {
  return text ? <div className="error">{text}</div> : null;
}
