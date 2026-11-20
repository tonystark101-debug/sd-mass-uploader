// Internet lines: every line the station uploads over, its speed now, and the total.
import { Globe } from "lucide-react";
import { bytes, type Tone } from "./format";
import type { LineView, NetworkView } from "./types";
import { Pill } from "./ui";

const STATES: Record<string, { label: string; tone: Tone }> = {
  active: { label: "Working", tone: "green" },
  checking: { label: "Checking", tone: "blue" },
  no_internet: { label: "No internet", tone: "red" },
  resting: { label: "Resting", tone: "amber" },
  spare: { label: "Spare", tone: "gray" },
  gone: { label: "Unplugged", tone: "purple" },
};

function headline(network: NetworkView): string {
  switch (network.mode) {
    case "multi": return `Uploading on ${network.working} lines at once`;
    case "single": return "Uploading on 1 line. Plug in another to go faster";
    case "default": return "No working line found yet: using this computer's default connection";
    default: return "Several lines switched off in settings";
  }
}

export function speedText(mbit: number): string {
  return mbit >= 1000 ? `${(mbit / 1000).toFixed(2)} Gbit/s` : `${mbit.toFixed(1)} Mbit/s`;
}

function LineRow({ line, top }: { line: LineView; top: number }) {
  const look = STATES[line.state] ?? { label: line.state, tone: "gray" as Tone };
  const width = top > 0 ? Math.round((100 * line.mbit_per_s) / top) : 0;
  const note = [line.shares_with && `Same internet as ${line.shares_with}: adds no speed.`,
                line.note].filter(Boolean).join(" ");
  return (
    <li className="line-row">
      <div className="line-head">
        <strong>{line.name}</strong>
        <span className="muted small">{line.kind} · {line.ip}</span>
        <Pill tone={look.tone}>{look.label}</Pill>
      </div>
      <div className="line-speed">
        <div className="bar" aria-hidden="true">
          <div className={`bar-fill fill-${line.state === "active" ? "green" : "gray"}`}
               style={{ width: `${width}%` }} />
        </div>
        <span className="line-mbit">{speedText(line.mbit_per_s)}</span>
      </div>
      <div className="muted small">
        {line.slots ? `${line.uploads} of ${line.slots} uploads` : `${line.uploads} uploads`}
        {" · "}{bytes(line.sent_bytes)} sent · {line.files_done} files
        {line.public_ip ? ` · public ${line.public_ip}` : ""}
      </div>
      {note && <div className={`small ${line.state === "no_internet" ? "tone-red" : "muted"}`}>
        {note}</div>}
    </li>
  );
}

export function LinesPanel({ network }: { network: NetworkView | undefined }) {
  if (!network || !network.enabled) return null;
  const top = Math.max(1, ...network.lines.map((line) => line.mbit_per_s));
  return (
    <section className="lines" aria-label="Internet lines">
      <div className="section-head">
        <h2><Globe size={18} aria-hidden="true" /> Internet lines</h2>
        <span className="lines-total" title="Upload speed over every line together">
          {speedText(network.total_mbit_per_s)}
        </span>
      </div>
      <p className="muted small">{headline(network)} · {network.capacity} files at once</p>
      {network.error && <p className="small tone-red">Could not read the connections:
        {" "}{network.error}</p>}
      <ul className="line-list">
        {network.lines.map((line) => <LineRow key={line.key} line={line} top={top} />)}
      </ul>
    </section>
  );
}
