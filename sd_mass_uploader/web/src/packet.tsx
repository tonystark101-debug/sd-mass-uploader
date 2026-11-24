// Start from a vendor code: the operator types the code written on the packet, and the station
// reads the vendor, activity and place (and the packet, if there is one) from the vendor platform.
import {
  AlertTriangle, ArrowRight, Briefcase, CalendarDays, CircleCheck, Cpu, KeyRound, MapPin,
  Package, Play, RotateCcw, Search, User,
} from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { api, ApiError } from "./api";
import type { PacketPreview, StationState } from "./types";
import { Button, useAction } from "./ui";

const CODE = /^[A-Za-z]{5}\d{4}[A-Za-z]/;
const VENDOR_ONLY = "__vendor__";

export function VendorCodeStart({ state, auto }: { state: StationState; auto: boolean }) {
  const detected = state.waiting_cards.length;
  const [code, setCode] = useState("");
  const [preview, setPreview] = useState<PacketPreview | null>(null);
  const [choice, setChoice] = useState<string | null>(null);  // a packet ref, or VENDOR_ONLY
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [checking, setChecking] = useState(false);
  const { run, busy } = useAction();

  const cleanCode = code.toUpperCase().replace(/[^A-Z0-9-]/g, "");
  const codeOk = CODE.test(cleanCode.replace(/-/g, ""));

  const fetchFor = async (ref: string, quiet = false) => {
    if (!quiet) { setChecking(true); setError(null); }
    try {
      const answer = await api<PacketPreview>("POST", "/api/v1/packet/preview", { code: ref });
      setPreview(answer);
      return answer;
    } catch (err) {
      if (!quiet) {
        setPreview(null);
        setError(err instanceof ApiError ? err.message : "Something went wrong.");
      }
      return null;
    } finally {
      if (!quiet) setChecking(false);
    }
  };

  const lookUp = async () => {
    setChoice(null);
    setNote("");
    const answer = await fetchFor(cleanCode);
    if (answer?.packet) setChoice(answer.packet.ref);
  };

  // A card plugged in or pulled out after the check: compare the counts again.
  const shownRef = preview?.packet?.ref;
  const lastCount = useRef(detected);
  useEffect(() => {
    if (lastCount.current === detected) return;
    lastCount.current = detected;
    if (shownRef) void fetchFor(shownRef, true);
  }, [detected, shownRef]);

  const pick = async (ref: string) => {
    setChoice(ref);
    setNote("");
    if (ref !== VENDOR_ONLY) await fetchFor(ref);
  };

  const reset = () => { setPreview(null); setChoice(null); setError(null); setNote(""); };

  const vendorOnly = choice === VENDOR_ONLY;
  const packet = !vendorOnly ? preview?.packet ?? null : null;
  const needsReason = !!packet && (preview?.problems.length ?? 0) > 0;
  const canStart = !!preview && (vendorOnly || (!!packet && (!needsReason || note.trim().length >= 5)));

  const start = () => void run("packet", () => api("POST", "/api/v1/packet/open", vendorOnly
    ? { code: cleanCode, vendor_only: true, auto_upload: auto }
    : { code: packet!.ref, note: note.trim(), auto_upload: auto }),
    vendorOnly ? "Upload started with the vendor's details."
               : `Packet ${packet?.ref} started. Cards upload by themselves.`);

  const who = preview?.vendor;
  return (
    <div className="mode-panel">
      <div className="code-row">
        <label className="field grow">
          <span>Vendor code</span>
          <div className="input-icon">
            <KeyRound size={20} aria-hidden="true" />
            <input value={code} placeholder="FVGPA3451K" autoCapitalize="characters" autoFocus
                   className="code-input" spellCheck={false} aria-label="Vendor code"
                   onChange={(event) => { setCode(event.target.value); reset(); }}
                   onKeyDown={(event) => { if (event.key === "Enter" && codeOk) void lookUp(); }} />
          </div>
          <small className="muted">The code written on the packet: 5 letters, 4 digits and a
            letter. The date after it is optional.</small>
        </label>
        <Button kind="primary" size="lg" icon={<Search size={18} />} disabled={!codeOk}
                busy={checking} onClick={() => void lookUp()}>Fetch details</Button>
      </div>

      {error && (
        <div className="alert alert-bad" role="alert">
          <AlertTriangle size={20} aria-hidden="true" />
          <div><strong>{error}</strong>
            <p className="small">Check the code, or use <em>Enter manually</em> above.</p></div>
        </div>
      )}

      {who && (
        <div className="result-card">
          <div className="result-head">
            <span className="avatar" aria-hidden="true">{initials(who.vendor)}</span>
            <div>
              <h3>{who.vendor}</h3>
              <span className="muted small">Vendor code <code>{who.code}</code> · from the vendor
                platform</span>
            </div>
            <span className="badge tone-green"><CircleCheck size={14} /> Found</span>
          </div>
          <div className="fact-grid">
            <Fact icon={<User size={18} />} label="Vendor name" value={who.vendor} />
            <Fact icon={<Briefcase size={18} />} label="Activity"
                  value={who.environment || "—"} sub={String(preview!.details.activity ?? "")} />
            <Fact icon={<MapPin size={18} />} label="Place of recording"
                  value={[who.site, who.city].filter(Boolean).join(", ") || "—"}
                  sub={String(preview!.details.city ?? "")} />
            <Fact icon={<Cpu size={18} />} label="Device (vendor said)"
                  value={who.device_kind || "—"} sub="each card is still checked" />
          </div>

          {preview!.choices.length > 0 && (
            <div className="choices">
              <h4>Which packet are these cards from?</h4>
              {preview!.choices.map((item) => (
                <button key={item.ref} type="button"
                        className={`choice${choice === item.ref ? " selected" : ""}`}
                        onClick={() => void pick(item.ref)} aria-pressed={choice === item.ref}>
                  <Package size={20} aria-hidden="true" />
                  <span className="choice-main">
                    <strong>{item.ref}</strong>
                    <span className="muted small">
                      {item.cards_received ?? item.cards_sent} card(s) · recorded {days(item.recorded_from, item.recorded_to)}
                      {" · "}{item.hours_declared} h declared
                    </span>
                  </span>
                  {item.uploaded
                    ? <span className="badge tone-amber">Uploaded {item.uploaded.at.slice(0, 10)}</span>
                    : <span className="badge tone-blue">Waiting</span>}
                </button>
              ))}
              <button type="button" className={`choice${vendorOnly ? " selected" : ""}`}
                      onClick={() => void pick(VENDOR_ONLY)} aria-pressed={vendorOnly}>
                <User size={20} aria-hidden="true" />
                <span className="choice-main">
                  <strong>None of these</strong>
                  <span className="muted small">Use only the vendor, activity and place. No
                    packet is marked or reported.</span>
                </span>
              </button>
            </div>
          )}

          {packet && preview && (
            <div className="packet-box">
              <div className="packet-title"><Package size={18} aria-hidden="true" />
                Packet <code>{packet.ref}</code></div>
              <div className="fact-grid small-facts">
                <Fact icon={<Package size={16} />} label="Cards"
                      value={`packet ${preview.cards.expected ?? "?"} · station sees ${preview.cards.detected}`}
                      sub={packet.cards_received !== null ? `manager counted ${packet.cards_received}`
                                                          : `vendor sent ${packet.cards_sent}`} />
                <Fact icon={<CalendarDays size={16} />} label="Recorded"
                      value={days(packet.recorded_from, packet.recorded_to)}
                      sub={`${packet.hours_declared} h declared over ${packet.days.length} day(s)`} />
              </div>
              {needsReason ? (
                <div className="alert alert-warn">
                  <AlertTriangle size={20} aria-hidden="true" />
                  <div className="grow">
                    <ul className="problems-list">
                      {preview.problems.map((text) => <li key={text}>{text}</li>)}
                    </ul>
                    <label className="field">
                      <span>Fix it, or say why to go on anyway (sent to the vendor platform)</span>
                      <input value={note} onChange={(event) => setNote(event.target.value)}
                             placeholder="e.g. card 7 is cracked, kept aside" />
                    </label>
                  </div>
                </div>
              ) : (
                <p className="tone-green small with-icon"><CircleCheck size={16} /> All {preview.cards.detected} card(s) of the packet are plugged in.</p>
              )}
            </div>
          )}

          <div className="result-actions">
            <Button kind="ghost" icon={<RotateCcw size={16} />} onClick={() => { setCode(""); reset(); }}>
              Different code</Button>
            <span className="spacer" />
            <Button kind="primary" size="lg" icon={<Play size={18} />} disabled={!canStart || detected === 0}
                    busy={busy === "packet"} onClick={start}>
              Start upload {detected > 0 && <>· {detected} card{detected === 1 ? "" : "s"}</>}
              <ArrowRight size={18} aria-hidden="true" />
            </Button>
          </div>
          {detected === 0 && <p className="muted small right">Plug the cards in to start.</p>}
          {!canStart && preview.choices.length > 0 && !choice && (
            <p className="muted small right">Choose a packet, or "None of these".</p>
          )}
        </div>
      )}
    </div>
  );
}

function Fact({ icon, label, value, sub }: {
  icon: ReactNode; label: string; value: string; sub?: string;
}) {
  return (
    <div className="fact">
      <span className="fact-icon" aria-hidden="true">{icon}</span>
      <div>
        <span className="fact-label">{label}</span>
        <strong className="fact-value">{value}</strong>
        {sub && <span className="fact-sub">{sub}</span>}
      </div>
    </div>
  );
}

function initials(name: string): string {
  return name.split(/\s+/).filter(Boolean).slice(0, 2).map((part) => part[0]).join("").toUpperCase() || "?";
}

function days(from: string | null, to: string | null): string {
  if (!from) return "dates unknown";
  return to && to !== from ? `${from} to ${to}` : from;
}
