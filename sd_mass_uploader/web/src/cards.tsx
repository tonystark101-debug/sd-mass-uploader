// The card grid and the card detail panel.
import {
  CircleCheck, Cpu, Pause, Play, RefreshCw, SkipForward,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { bytes, cardName, look, percent, speed } from "./format";
import type { CardDetail, CardView, Destination } from "./types";
import { Button, Panel, StatusBadge, useAction } from "./ui";

export function CardTile({ card, onOpen, destination }: {
  card: CardView; onOpen: () => void; destination: Destination;
}) {
  const share = percent(card.bytes_done, card.bytes_total);
  const tone = look(card.state).tone;
  const placement = destination.cards?.[card.card_id];
  const where = placement?.groups.map((group) => group.pass).filter(Boolean).join(" + ");
  return (
    <button type="button" className={`tile tile-${tone}`} onClick={onOpen}
            aria-label={`${cardName(card)}: ${look(card.state).word}`}>
      <div className="tile-top">
        <strong className="tile-name">{cardName(card)}</strong>
        <StatusBadge state={card.state}
                     extra={card.state === "UPLOADING" ? `${share}%` : undefined} />
      </div>
      <div className="muted small tile-sub">
        {card.label ? card.card_id : card.reader_model ?? ""}
        {where ? ` · ${where}` : ""}
      </div>
      <div className="bar" aria-hidden="true">
        <div className={`bar-fill fill-${tone}`} style={{ width: `${share}%` }} />
      </div>
      <div className="tile-foot small">
        <span>{card.files_done}/{card.files_total} files</span>
        <span>{bytes(card.bytes_done)} of {bytes(card.bytes_total)}</span>
      </div>
      {card.state === "UPLOADED" && <div className="safe small">Safe to remove</div>}
      {card.reason && card.state !== "UPLOADED" && (
        <div className="tile-reason small">{card.reason}</div>
      )}
    </button>
  );
}

const STATUS_FILTERS = ["", "FAILED", "CONFLICT", "FAILED_PERMANENT", "UNCERTAIN", "PENDING",
                        "ACTIVE", "VERIFIED"];

export function CardDrawer({ cardId, destination, onClose }: {
  cardId: string; destination: Destination; onClose: () => void;
}) {
  const [detail, setDetail] = useState<CardDetail | null>(null);
  const [status, setStatus] = useState("");
  const [error, setError] = useState<string | null>(null);
  const { run, busy } = useAction();

  const load = useCallback(() => {
    const query = status ? `?status=${status}&limit=200` : "?limit=200";
    api<CardDetail>("GET", `/api/v1/cards/${encodeURIComponent(cardId)}${query}`)
      .then((data) => { setDetail(data); setError(null); })
      .catch((reason: Error) => setError(reason.message));
  }, [cardId, status]);

  useEffect(() => {
    load();
    const timer = window.setInterval(load, 3000);
    return () => window.clearInterval(timer);
  }, [load]);

  const card = detail?.card;
  const path = `/api/v1/cards/${encodeURIComponent(cardId)}`;
  const command = (name: string, label: string, body?: unknown) =>
    run(name, () => api("POST", `${path}/${name}`, body), label).then(load);
  const placement = destination.cards?.[cardId];

  return (
    <Panel title={card ? cardName(card) : cardId} onClose={onClose} wide>
      {error && <p className="error-text">{error}</p>}
      {card && (
        <>
          <div className="drawer-status">
            <StatusBadge state={card.state} />
            {card.reason && <p>{card.reason}</p>}
          </div>
          <div className="actions-row">
            {card.paused ? (
              <Button icon={<Play size={16} />} busy={busy === "resume"}
                      onClick={() => command("resume", "Resumed.")}>Resume</Button>
            ) : (
              <Button icon={<Pause size={16} />} busy={busy === "pause"}
                      onClick={() => command("pause", "Paused. Files in progress finish first.")}>
                Pause
              </Button>
            )}
            {card.state === "READY" && (
              <Button icon={<Play size={16} />} kind="primary" busy={busy === "start"}
                      onClick={() => command("start", "Started.")}>Start upload</Button>
            )}
            {card.state === "NEEDS_CONFIRMATION" && (
              <Button icon={<CircleCheck size={16} />} kind="primary" busy={busy === "confirm"}
                      onClick={() => command("confirm", "Confirmed.")}>It is a card</Button>
            )}
            {["FAILED", "UPLOADED_WITH_ISSUES", "NEEDS_ATTENTION"].includes(card.state) && (
              <Button icon={<RefreshCw size={16} />} busy={busy === "retry"}
                      onClick={() => command("retry", "Queued again.")}>Retry failed files</Button>
            )}
          </div>
          {card.state === "NEEDS_DECISION" && destination.devices && (
            <PlaceCard devices={Object.keys(destination.devices)} busy={busy}
                       onPlace={(device) => command("place", "Placed.", { device })}
                       onSkip={() => command("place", "Unplaced files skipped.", { skip: true })} />
          )}
          <dl className="facts">
            <dt>Card ID</dt><dd>{card.card_id}</dd>
            {placement?.serial && (<><dt>Folder name</dt><dd>card-{placement.serial}</dd></>)}
            <dt>Files</dt><dd>{card.files_done} of {card.files_total} uploaded and verified</dd>
            <dt>Size</dt><dd>{bytes(card.bytes_done)} of {bytes(card.bytes_total)}</dd>
            <dt>Reader</dt><dd>{card.reader_model ?? "unknown"}</dd>
            <dt>USB link</dt><dd>{speed(card.link_speed_mbps)}</dd>
            <dt>File system</dt><dd>{card.filesystem ?? "unknown"}</dd>
            <dt>Inserted</dt><dd>{card.present ? (card.mount_point ?? "yes") : "no (removed)"}</dd>
          </dl>
          {placement && placement.groups.length > 0 && (
            <section>
              <h3>Where it goes</h3>
              <table className="table">
                <thead><tr><th>Device</th><th>Pass</th><th>Files</th><th>Hours</th></tr></thead>
                <tbody>
                  {placement.groups.map((group) => (
                    <tr key={`${group.device}-${group.pass}`}>
                      <td>{group.device}</td>
                      <td>{group.folder ?? group.pass ?? "not set"}</td>
                      <td>{group.files} ({bytes(group.bytes)})</td>
                      <td>{group.hours ?? "unknown"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {placement.excluded > 0 && (
                <p className="muted small">{placement.excluded} item(s) were not project
                  footage and are not uploaded.</p>
              )}
            </section>
          )}
          <section>
            <div className="section-head">
              <h3>Files</h3>
              <label className="small">
                Show{" "}
                <select value={status} onChange={(event) => setStatus(event.target.value)}>
                  {STATUS_FILTERS.map((value) => (
                    <option key={value} value={value}>
                      {value ? value.toLowerCase().replace("_", " ") : "problems first"}
                      {value && detail?.file_status_counts[value] != null
                        ? ` (${detail.file_status_counts[value]})` : ""}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <table className="table files">
              <thead><tr><th>File</th><th>Status</th><th>Size</th><th>Last error</th></tr></thead>
              <tbody>
                {detail?.files.map((file) => (
                  <tr key={file.task_id}>
                    <td className="path">{file.relative_path}</td>
                    <td>{file.status.toLowerCase().replace("_", " ")}</td>
                    <td>{bytes(file.size_bytes)}</td>
                    <td className="small">{file.last_error ?? ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {detail && detail.files.length === 0 && <p className="muted">No files to show.</p>}
          </section>
          {detail && detail.scan_errors.length > 0 && (
            <section>
              <h3>Scan notes</h3>
              <ul className="plain">
                {detail.scan_errors.map((item) => (
                  <li key={`${item.path}-${item.kind}`}>
                    <code>{item.path}</code> <span className="muted">({item.kind})</span>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </>
      )}
    </Panel>
  );
}

function PlaceCard({ devices, busy, onPlace, onSkip }: {
  devices: string[]; busy: string | null; onPlace: (device: string) => void; onSkip: () => void;
}) {
  const [device, setDevice] = useState("");
  return (
    <section className="decision">
      <h3>Which device recorded the files the station could not place?</h3>
      <p className="muted small">Ask the person. Never guess: the folder is chosen from the
        device. If they are not project footage, skip them.</p>
      <div className="actions-row">
        <select value={device} onChange={(event) => setDevice(event.target.value)}
                aria-label="Device">
          <option value="">Choose the device…</option>
          {devices.map((name) => <option key={name} value={name}>{name}</option>)}
        </select>
        <Button icon={<Cpu size={16} />} kind="primary" disabled={!device} busy={busy === "place"}
                onClick={() => onPlace(device)}>Use this device</Button>
        <Button icon={<SkipForward size={16} />} kind="ghost" onClick={onSkip}>Skip those files</Button>
      </div>
    </section>
  );
}
