"use client";

import { useState } from "react";

export default function CopyBox({ label, text }: { label: string; text: string }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // clipboard blocked: the text is selectable
    }
  }
  return (
    <div className="copy">
      <div className="row small muted">{label}<div className="spacer" />
        <button type="button" onClick={copy}>{copied ? "Copied" : "Copy"}</button>
      </div>
      <pre>{text}</pre>
    </div>
  );
}
