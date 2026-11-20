// Start from a vendor platform packet: put the cards in, say how many, type the code. The
// station reads everything else from the vendor platform; nobody types activity, city or vendor.
import { useState } from "react";
import { api, ApiError } from "./api";
import type { PacketPreview, StationState } from "./types";
import { Button, Pill, useAction } from "./ui";

const CODE = /^[A-Za-z]{5}\d{4}[A-Za-z]/;

export function PacketStart({ state, onTypeByHand }: {
  state: StationState; onTypeByHand: () => void;
}) {
  const detected = state.waiting_cards.length;
  const [counted, setCounted] = useState("");
  const [code, setCode] = useState("");
  const [preview, setPreview] = useState<PacketPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [checking, setChecking] = useState(false);
  const { run, busy } = useAction();

  const count = counted.trim() === "" ? null : Number(counted);
  const countOk = count !== null && Number.isInteger(count) && count >= 0;
  const cleanCode = code.toUpperCase().replace(/[^A-Z0-9-]/g, "");
  const codeOk = CODE.test(cleanCode.replace(/-/g, ""));
  // A changed answer makes the shown packet out of date.
  const reset = () => { setPreview(null); setError(null); };

  const check = async () => {
    setChecking(true);
    setError(null);
    try {
      setPreview(await api<PacketPreview>("POST", "/api/v1/packet/preview",
                                          { code: cleanCode, cards_counted: count }));
    } catch (err) {
      setPreview(null);
      setError(err instanceof ApiError ? err.message : "Something went wrong.");
    } finally {
      setChecking(false);
    }
  };

  const needsReason = (preview?.problems.length ?? 0) > 0;
  const canStart = !!preview && (!needsReason || note.trim().length >= 5);
  const start = () => void run("packet", () => api("POST", "/api/v1/packet/open", {
    code: preview!.packet.ref, cards_counted: count, note: note.trim(),
  }), `Packet ${preview!.packet.ref} started. Cards upload by themselves.`);

  const p = preview?.packet;
  return (
    <section className="start packet-start">
      <h2>New packet</h2>

      <ol className="steps">
        <li className={detected > 0 ? "done" : "now"}>
          <strong>Put all the packet's cards in.</strong>
          <span className="big-count" aria-live="polite">{detected}</span>
          <span className="muted">{detected === 1 ? "card" : "cards"} plugged in now</span>
        </li>

        <li className={countOk && count === detected ? "done" : detected > 0 ? "now" : ""}>
          <label className="field">
            <strong>How many cards did you put in?</strong>
            <input inputMode="numeric" value={counted} placeholder="10" className="count-input"
                   onChange={(event) => { setCounted(event.target.value.replace(/\D/g, ""));
                                          reset(); }} />
          </label>
          {countOk && count !== detected && (
            <p className="tone-red small">
              {count! > detected
                ? `The station sees only ${detected}. ${count! - detected} card(s) are not showing: check each reader's light and push the card in again.`
                : `The station sees ${detected}: ${detected - count!} more than you counted. Is a USB stick or another packet's card in?`}
            </p>
          )}
          {countOk && count === detected && <p className="tone-green small">All {detected} seen.</p>}
        </li>

        <li className={preview ? "done" : countOk ? "now" : ""}>
          <label className="field">
            <strong>Type the code written on the packet</strong>
            <input value={code} placeholder="FVGPA3451K" autoCapitalize="characters"
                   className="code-input" spellCheck={false}
                   onChange={(event) => { setCode(event.target.value); reset(); }}
                   onKeyDown={(event) => { if (event.key === "Enter" && codeOk && countOk) void check(); }} />
          </label>
          <Button kind="primary" disabled={!codeOk || !countOk} busy={checking}
                  onClick={() => void check()}>Check packet</Button>
          {error && <p className="check-report bad">{error}</p>}
        </li>
      </ol>

      {p && preview && (
        <div className={`check-report ${needsReason ? "bad" : "ok"}`}>
          <h3>{p.ref}</h3>
          <dl className="packet-facts">
            <dt>From</dt><dd>{p.vendor}{p.manager ? ` (manager ${p.manager})` : ""}</dd>
            <dt>Where</dt><dd>{[p.environment, p.site, p.city].filter(Boolean).join(" · ")}</dd>
            <dt>Device</dt><dd>{p.device_kind}</dd>
            <dt>Cards</dt><dd>
              vendor sent {p.cards_sent}
              {p.cards_received !== null && <>, manager counted {p.cards_received}</>}
              {", "}you counted {preview.cards.counted}, station sees {preview.cards.detected}
            </dd>
            <dt>Recorded</dt><dd>
              {p.recorded_from ?? "?"}{p.recorded_to && p.recorded_to !== p.recorded_from ? ` to ${p.recorded_to}` : ""}
              {" · "}{p.hours_declared} h declared over {p.days.length} day(s)
            </dd>
            <dt>Goes to</dt><dd>
              <Pill tone="blue">{preview.details.activity}</Pill>{" "}
              <Pill tone="blue">{preview.details.city}</Pill>{" "}
              <span className="muted small">each card into its device's folder</span>
            </dd>
          </dl>
          {needsReason && (
            <>
              <ul className="problems-list">
                {preview.problems.map((text) => <li key={text}>{text}</li>)}
              </ul>
              <label className="field">
                <span>Fix it above, or say why to go on anyway (sent to the vendor platform)</span>
                <input value={note} onChange={(event) => setNote(event.target.value)}
                       placeholder="e.g. card 7 is cracked, kept aside" />
              </label>
            </>
          )}
          <Button kind="primary" disabled={!canStart} busy={busy === "packet"} onClick={start}>
            Start upload
          </Button>
        </div>
      )}

      <p className="muted small">
        No code on the packet?{" "}
        <button type="button" className="link small" onClick={onTypeByHand}>
          Type the details by hand</button>
      </p>
    </section>
  );
}
