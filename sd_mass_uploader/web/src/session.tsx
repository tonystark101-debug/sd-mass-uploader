// Start an upload. Normally one question: the vendor code (the vendor platform knows the rest).
// Without a code: three things typed by hand. Device, dates and project come from the cards.
import {
  CalendarDays, ChevronDown, Cpu, FolderTree, HardDrive, CircleHelp, KeyRound, PencilLine, Play,
} from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";
import { api } from "./api";
import { VendorCodeStart } from "./packet";
import type { Destination, StationState } from "./types";
import { Button, useAction } from "./ui";

const PHONE = /(?:\+?\d[\s\-().]*){10,}/;
const today = () => new Date().toISOString().slice(0, 10);
/** "Motor Repair " -> "motor-repair": how folder names are written. */
const slug = (text: string) => text.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");

interface Field {
  name: string; label: string; required?: boolean; hint?: string; type?: string;
  options?: string[]; placeholder?: string; slug?: boolean; more?: boolean;
}

function fieldsFor(destination: Destination): Field[] {
  if (destination.profile === "peakai") {
    return [
      { name: "vendor_name", label: "Vendor name", required: true, placeholder: "e.g. Narayan",
        hint: "Who supplied this footage. Ask; never guess. Two vendors: \"Vendor 1: …, Vendor 2: …\"" },
      { name: "activity", label: "Activity", required: true, options: destination.pack?.activities,
        placeholder: "e.g. motor-repair", slug: true,
        hint: "Pick from the list, or type new work in a few words." },
      { name: "city", label: "Place of recording", required: true, placeholder: "e.g. bijapur",
        slug: true, hint: "The town or city where it was recorded." },
      { name: "site", label: "Site", placeholder: "Shop or site name", more: true },
      { name: "batch", label: "Batch", placeholder: "batch-9", more: true },
      { name: "recorded_from", label: "First recording day", type: "date", more: true,
        hint: "Only if the cards can't tell (encrypted or .mcap footage)." },
      { name: "recorded_to", label: "Last recording day", type: "date", more: true },
      { name: "video_hours", label: "Total video hours", type: "number", placeholder: "26.4",
        more: true },
    ];
  }
  return [
    { name: "activity_name", label: "Activity", required: true },
    { name: "place_of_recording", label: "Place of recording", required: true },
    { name: "recording_date", label: "Recording date", required: true, type: "date" },
    { name: "operator_name", label: "Operator", required: true },
    { name: "project_name", label: "Project", required: true },
    { name: "description", label: "Description", more: true },
  ];
}

export function StartSession({ state }: { state: StationState }) {
  const codeAvailable = Boolean(state.destination.vendor_platform);
  const [mode, setMode] = useState<"code" | "manual">(codeAvailable ? "code" : "manual");
  const [auto, setAuto] = useState(true);
  const detected = state.waiting_cards.length;

  return (
    <div className="start-page">
      <section className="hero">
        <div>
          <h1>New upload</h1>
          <p className="muted">Put all the cards in, then tell the station whose they are.
            Everything else is read from the cards.</p>
        </div>
        <div className={`plugged${detected ? " has" : ""}`} aria-live="polite">
          <HardDrive size={26} aria-hidden="true" />
          <div>
            <strong>{detected}</strong>
            <span>{detected === 1 ? "card" : "cards"} plugged in</span>
          </div>
        </div>
      </section>

      <div className="mode-switch" role="tablist" aria-label="How to fill in the details">
        <button type="button" role="tab" aria-selected={mode === "code"}
                className={`mode${mode === "code" ? " active" : ""}`} disabled={!codeAvailable}
                onClick={() => setMode("code")}>
          <span className="mode-icon"><KeyRound size={22} /></span>
          <span className="mode-text">
            <strong>Enter vendor code {codeAvailable && <em className="badge tone-blue">Recommended</em>}</strong>
            <span>{codeAvailable ? "Vendor, activity and place come from the vendor platform"
                                 : "Not set up on this station (vendor_platform in config.yaml)"}</span>
          </span>
        </button>
        <button type="button" role="tab" aria-selected={mode === "manual"}
                className={`mode${mode === "manual" ? " active" : ""}`}
                onClick={() => setMode("manual")}>
          <span className="mode-icon"><PencilLine size={22} /></span>
          <span className="mode-text">
            <strong>Enter manually</strong>
            <span>No code? Type the vendor name, activity and place</span>
          </span>
        </button>
      </div>

      <section className="panel">
        {mode === "code" && codeAvailable
          ? <VendorCodeStart state={state} auto={auto} />
          : <ManualStart state={state} auto={auto} />}
        <label className="switch">
          <input type="checkbox" checked={auto} onChange={(event) => setAuto(event.target.checked)} />
          <span className="switch-track" aria-hidden="true" />
          Upload each card as soon as it is scanned
        </label>
      </section>

      <AutoDetected />
    </div>
  );
}

function AutoDetected() {
  return (
    <section className="auto-strip" aria-label="Read from the cards">
      <h2>Read from the cards automatically</h2>
      <div className="auto-items">
        <div><Cpu size={20} aria-hidden="true" /><strong>Device</strong>
          <span>From each card's files: GI Lab, Mono, Stereo and more</span></div>
        <div><CalendarDays size={20} aria-hidden="true" /><strong>Recording dates</strong>
          <span>From the footage itself</span></div>
        <div><FolderTree size={20} aria-hidden="true" /><strong>Project folder</strong>
          <span>Chosen from the device, for the right client</span></div>
        <div><CircleHelp size={20} aria-hidden="true" /><strong>Not sure?</strong>
          <span>The station asks you which device recorded the card</span></div>
      </div>
    </section>
  );
}

function ManualStart({ state, auto }: { state: StationState; auto: boolean }) {
  const destination = state.destination;
  const fields = useMemo(() => fieldsFor(destination), [destination]);
  const [values, setValues] = useState<Record<string, string>>(
    destination.profile === "peakai" ? {} : { recording_date: today() });
  const [unknownCity, setUnknownCity] = useState(false);
  const { run, busy } = useAction();

  const valueOf = (field: Field) => {
    const raw = (values[field.name] ?? "").trim();
    return field.slug ? slug(raw) : raw;
  };
  const problems = fields.flatMap((field) => {
    const value = valueOf(field);
    if (field.required && !value) return [`${field.label} is required`];
    if (value && PHONE.test(values[field.name] ?? "")) return [`${field.label} looks like a phone number`];
    return [];
  });
  const cityIsUnknown = values.city !== undefined && slug(values.city) === "unknown";
  const ready = problems.length === 0 && (!cityIsUnknown || unknownCity);
  const main = fields.filter((field) => !field.more);
  const more = fields.filter((field) => field.more);

  const submit = () => {
    const metadata: Record<string, string | boolean> = {};
    for (const field of fields) {
      const value = valueOf(field);
      if (value) metadata[field.name] = value;
    }
    if (cityIsUnknown) metadata.city_unknown_confirmed = true;
    void run("open", () => api("POST", "/api/v1/session", { metadata, auto_upload: auto }),
             "Upload started. Cards join it as they are scanned.");
  };
  const input = (field: Field) => (
    <Labelled key={field.name} field={field}
              shown={field.slug && values[field.name] && slug(values[field.name]) !== values[field.name]
                ? `Saved as: ${slug(values[field.name])}` : undefined}>
      <input type={field.type ?? "text"} value={values[field.name] ?? ""}
             list={field.options ? `list-${field.name}` : undefined} placeholder={field.placeholder}
             onChange={(event) => setValues({ ...values, [field.name]: event.target.value })} />
      {field.options && (
        <datalist id={`list-${field.name}`}>
          {field.options.map((option) => <option key={option} value={option} />)}
        </datalist>
      )}
    </Labelled>
  );

  return (
    <div className="mode-panel">
      <div className="form-grid three">{main.map(input)}</div>
      {cityIsUnknown && (
        <label className="check">
          <input type="checkbox" checked={unknownCity}
                 onChange={(event) => setUnknownCity(event.target.checked)} />
          Nobody knows where it was recorded (the place will be "unknown")
        </label>
      )}
      {more.length > 0 && (
        <details className="more">
          <summary><ChevronDown size={16} aria-hidden="true" /> More details (optional)</summary>
          <div className="form-grid">{more.map(input)}</div>
        </details>
      )}
      {destination.profile === "peakai" && destination.pack && (
        <p className="muted small">Uploads go to <code>gs://{destination.pack.bucket}</code>, into
          each card's project folder for today (India time).</p>
      )}
      <div className="result-actions">
        {problems.length > 0 && <span className="muted small">{problems.join(". ")}.</span>}
        <span className="spacer" />
        <Button kind="primary" size="lg" icon={<Play size={18} />} disabled={!ready}
                busy={busy === "open"} onClick={submit}>Start upload</Button>
      </div>
    </div>
  );
}

function Labelled({ field, children, shown }: { field: Field; children: ReactNode; shown?: string }) {
  return (
    <label className="field">
      <span>{field.label}{field.required ? <b className="req" aria-label="required"> *</b> : ""}</span>
      {children}
      {shown ? <small className="tone-blue">{shown}</small>
             : field.hint && <small className="muted">{field.hint}</small>}
    </label>
  );
}
