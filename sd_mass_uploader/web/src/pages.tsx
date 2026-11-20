// History, Hardware and Setup.
import { KeyRound, Upload } from "lucide-react";
import { useEffect, useRef, useState, type ReactElement } from "react";
import { api, ApiError } from "./api";
import { since, speed } from "./format";
import type { PackCheck, StationState } from "./types";
import { Button, Empty, Pill, useAction } from "./ui";

// ------------------------------------------------------------------ history

interface SessionRow { session_id: string; status: string; created_at: string;
  upload_seconds: number; last_error: string | null }

export function HistoryPage() {
  const [rows, setRows] = useState<SessionRow[] | null>(null);
  const [report, setReport] = useState<Record<string, unknown> | null>(null);
  useEffect(() => {
    api<SessionRow[]>("GET", "/api/v1/sessions?limit=100").then(setRows).catch(() => setRows([]));
  }, []);
  if (!rows) return <p className="muted">Loading…</p>;
  if (!rows.length) return <Empty title="No sessions yet" />;
  return (
    <div className="split">
      <table className="table">
        <thead><tr><th>Session</th><th>Status</th><th>Started</th><th /></tr></thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.session_id}>
              <td><code>{row.session_id}</code></td>
              <td><Pill tone={row.status === "SUCCESS" ? "green" : row.status === "UPLOADING"
                ? "blue" : row.status === "FAILED" ? "red" : "gray"}>{row.status}</Pill></td>
              <td>{since(row.created_at)}</td>
              <td>
                <button type="button" className="link" onClick={() =>
                  api<Record<string, unknown>>("GET",
                    `/api/v1/sessions/${row.session_id}/report`).then(setReport)}>
                  Report
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {report && (
        <section className="report">
          <h3>Report</h3>
          <pre>{JSON.stringify(report.summary ?? report, null, 2)}</pre>
        </section>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ hardware

interface UsbDevice { topology_id: string; parent_topology_id: string | null;
  product_name: string | null; is_hub: boolean; negotiated_speed_mbps: number | null;
  speed_source: string }
interface Inventory { captured_at: string; usb_devices: UsbDevice[];
  readers: { reader_id: string; model: string | null; status: string; topology_id: string | null }[] }

export function HardwarePage() {
  const [inventory, setInventory] = useState<Inventory | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api<Inventory>("GET", "/api/v1/hardware").then(setInventory)
      .catch((reason: Error) => setError(reason.message));
  }, []);
  if (error) {
    return <Empty title="No hardware inventory yet">{error}. It appears after the station
      sees real readers.</Empty>;
  }
  if (!inventory) return <p className="muted">Loading…</p>;
  const children = (parent: string | null) =>
    inventory.usb_devices.filter((device) => device.parent_topology_id === parent);
  const tree = (parent: string | null, depth: number): ReactElement[] =>
    children(parent).flatMap((device) => [
      <li key={device.topology_id} style={{ paddingLeft: `${depth * 20}px` }}>
        <strong>{device.product_name ?? "USB device"}</strong>{" "}
        {device.is_hub && <Pill tone="gray">hub</Pill>}{" "}
        <Pill tone={device.negotiated_speed_mbps && device.negotiated_speed_mbps < 5000
          ? "orange" : "gray"}>{speed(device.negotiated_speed_mbps)}</Pill>
      </li>,
      ...tree(device.topology_id, depth + 1),
    ]);
  return (
    <section>
      <p className="muted">Saved {since(inventory.captured_at)}. Readers below 5 Gbps are
        marked orange: move them to a USB 3 port.</p>
      <ul className="tree">{tree(null, 0)}</ul>
    </section>
  );
}

// ------------------------------------------------------------------ setup

export function SetupPage({ state }: { state: StationState }) {
  const [settings, setSettings] = useState<Record<string, Record<string, unknown>> | null>(null);
  const [check, setCheck] = useState<PackCheck | null>(null);
  const [applied, setApplied] = useState<string | null>(null);
  const file = useRef<HTMLInputElement>(null);
  const { run, busy } = useAction();
  const destination = state.destination;

  useEffect(() => {
    api<Record<string, Record<string, unknown>>>("GET", "/api/v1/settings")
      .then(setSettings).catch(() => setSettings(null));
  }, []);

  const upload = async () => {
    const chosen = file.current?.files?.[0];
    if (!chosen) return;
    setApplied(null);
    await run("check", async () => {
      try {
        setCheck(await api<PackCheck>("POST", "/api/v1/pack/check", undefined, chosen));
      } catch (error) {
        setCheck(null);
        throw error instanceof ApiError ? error : new ApiError(0, "Check failed");
      }
    });
  };

  return (
    <div className="setup">
      <section className="card-block">
        <h2>Upload destination</h2>
        {destination.profile === "peakai" && destination.pack ? (
          <>
            <p>PeakAI pack <strong>{destination.pack.name}</strong> (v{destination.pack.version},
              {" "}{destination.pack.dated}), imported {since(destination.pack.imported_at)}.
              Uploads go to <code>gs://{destination.pack.bucket}</code>.</p>
            <h3>Passes on this laptop</h3>
            <table className="table">
              <thead><tr><th>Pass</th><th>Folder</th><th>For</th><th>Token</th></tr></thead>
              <tbody>
                {destination.keeper?.passes.map((item) => (
                  <tr key={item.name}>
                    <td><strong>{item.name}</strong></td>
                    <td><code>{item.folder}</code></td>
                    <td>{item.description}</td>
                    <td>{item.has_token
                      ? <Pill tone="green">{item.minutes_left} min left</Pill>
                      : <Pill tone={item.last_error ? "red" : "gray"}>
                          {item.last_error ? "No token" : "Not fetched yet"}</Pill>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {destination.keeper?.allowed == null && (
              <p className="muted small">The station has not asked PeakAI for passes yet.</p>
            )}
            {destination.reports && destination.reports.length > 0 && (
              <>
                <h3>PeakAI dashboard entries</h3>
                <ul className="plain">
                  {destination.reports.map((report) => (
                    <li key={report.pass_name}>
                      <strong>{report.pass_name}</strong>:{" "}
                      {report.last_error
                        ? <span className="error-text">{report.last_error}</span>
                        : `${report.status} (sent ${since(report.sent_at)})`}
                    </li>
                  ))}
                </ul>
              </>
            )}
          </>
        ) : (
          <p>No PeakAI pack is imported. Uploads use the bucket in config.yaml
            {destination.bucket ? <> (<code>{destination.bucket}</code>)</> : ""}.</p>
        )}
      </section>

      <section className="card-block">
        <h2 className="with-icon"><KeyRound size={20} /> Import a PeakAI pack</h2>
        <p className="muted">Choose PeakAI's zip. The station checks it and shows what it will
          change. Nothing happens until you apply it. The laptop key inside is stored privately
          and never shown.</p>
        <div className="actions-row">
          <input ref={file} type="file" accept=".zip,application/zip" aria-label="Pack zip" />
          <Button icon={<Upload size={16} />} busy={busy === "check"} onClick={upload}>
            Check pack
          </Button>
        </div>
        {check && (
          <div className={`check-report ${check.ok ? "ok" : "bad"}`}>
            <pre>{check.lines.join("\n")}</pre>
            {check.ok ? (
              <div className="actions-row">
                <Button kind="primary" busy={busy === "apply"} onClick={() => run("apply",
                  async () => {
                    const result = await api<{ applied: string }>("POST", "/api/v1/pack/apply");
                    setApplied(result.applied);
                    setCheck(null);
                  })}>Apply this pack</Button>
                <Button kind="ghost" onClick={() => run("cancel", async () => {
                  await api("POST", "/api/v1/pack/cancel");
                  setCheck(null);
                })}>Cancel</Button>
              </div>
            ) : <p className="error-text">This pack has errors. Nothing was changed.</p>}
          </div>
        )}
        {applied && (
          <p className="callout">Pack {applied} applied. Restart the station service to use
            it: <code>sudo systemctl restart sdu-daemon</code>. The zip still holds the key:
            keep it private or delete it.</p>
        )}
      </section>

      {settings && (
        <section className="card-block">
          <h2>Settings</h2>
          <p className="muted small">Change these in config.yaml, then restart the station.</p>
          <div className="settings-grid">
            {Object.entries(settings).map(([group, values]) => (
              <dl key={group} className="facts">
                <dt className="group">{group}</dt><dd />
                {Object.entries(values).map(([key, value]) => (
                  <FragmentRow key={key} name={key} value={value} />
                ))}
              </dl>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

function FragmentRow({ name, value }: { name: string; value: unknown }) {
  return (
    <>
      <dt>{name.replaceAll("_", " ")}</dt>
      <dd>{value === null || value === "" ? "not set" : String(value)}</dd>
    </>
  );
}
