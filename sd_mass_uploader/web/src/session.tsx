// Start a session: PeakAI's fields when a pack is active, the generic fields otherwise.
import { useMemo, useState, type ReactNode } from "react";
import { api } from "./api";
import type { Destination, StationState } from "./types";
import { Button, useAction } from "./ui";

const PHONE = /(?:\+?\d[\s\-().]*){10,}/;
const today = () => new Date().toISOString().slice(0, 10);

interface Field {
  name: string; label: string; required?: boolean; hint?: string; type?: string;
  options?: string[]; placeholder?: string;
}

function fieldsFor(destination: Destination): Field[] {
  if (destination.profile === "peakai") {
    return [
      { name: "activity", label: "Activity", required: true, options: destination.pack?.activities,
        hint: "From PeakAI's list. A new kind of work: lowercase words joined by '-'." },
      { name: "city", label: "City", required: true, placeholder: "bijapur",
        hint: "Lowercase town name. Ask the person where it was recorded." },
      { name: "vendor_name", label: "Vendor", required: true, placeholder: "Narayan",
        hint: "Who supplied this footage. Ask; never guess. Two vendors: \"Vendor 1: …, Vendor 2: …\"" },
      { name: "site", label: "Site", placeholder: "Shop or site name, if known" },
      { name: "batch", label: "Batch", placeholder: "batch-9" },
      { name: "recorded_from", label: "First recording day", type: "date",
        hint: "Only if the cards can't tell (encrypted or .mcap footage)." },
      { name: "recorded_to", label: "Last recording day", type: "date" },
      { name: "video_hours", label: "Total video hours", type: "number", placeholder: "26.4" },
    ];
  }
  return [
    { name: "activity_name", label: "Activity", required: true },
    { name: "place_of_recording", label: "Place of recording", required: true },
    { name: "recording_date", label: "Recording date", required: true, type: "date" },
    { name: "operator_name", label: "Operator", required: true },
    { name: "project_name", label: "Project", required: true },
    { name: "description", label: "Description" },
  ];
}

export function StartSession({ state }: { state: StationState }) {
  const destination = state.destination;
  const fields = useMemo(() => fieldsFor(destination), [destination]);
  const [values, setValues] = useState<Record<string, string>>(
    destination.profile === "peakai" ? {} : { recording_date: today() });
  const [auto, setAuto] = useState(true);
  const [unknownCity, setUnknownCity] = useState(false);
  const { run, busy } = useAction();

  const problems = fields.flatMap((field) => {
    const value = (values[field.name] ?? "").trim();
    if (field.required && !value) return [`${field.label} is required`];
    if (value && PHONE.test(value)) return [`${field.label} looks like a phone number`];
    return [];
  });
  const cityIsUnknown = values.city?.trim().toLowerCase() === "unknown";
  const ready = problems.length === 0 && (!cityIsUnknown || unknownCity);

  const submit = () => {
    const metadata: Record<string, string | boolean> = {};
    for (const field of fields) {
      const value = (values[field.name] ?? "").trim();
      if (value) metadata[field.name] = value;
    }
    if (cityIsUnknown) metadata.city_unknown_confirmed = true;
    void run("open", () => api("POST", "/api/v1/session", { metadata, auto_upload: auto }),
             "Session opened. Insert cards.");
  };

  return (
    <section className="start">
      <h2>Start a session</h2>
      <p className="muted">These details go with every card inserted until you end the session.
        {destination.profile === "peakai" && destination.pack &&
          <> Uploads go to <code>gs://{destination.pack.bucket}</code>, into each card's pass
            folder for today (India time).</>}
      </p>
      {state.waiting_cards.length > 0 && (
        <p className="callout">{state.waiting_cards.length} card(s) are already inserted. They
          join the session when it opens.</p>
      )}
      <div className="form-grid">
        {fields.map((field) => (
          <Labelled key={field.name} field={field}>
            {field.options ? (
              <input list={`list-${field.name}`} value={values[field.name] ?? ""}
                     onChange={(event) => setValues({ ...values, [field.name]: event.target.value })} />
            ) : (
              <input type={field.type ?? "text"} value={values[field.name] ?? ""}
                     placeholder={field.placeholder}
                     onChange={(event) => setValues({ ...values, [field.name]: event.target.value })} />
            )}
            {field.options && (
              <datalist id={`list-${field.name}`}>
                {field.options.map((option) => <option key={option} value={option} />)}
              </datalist>
            )}
          </Labelled>
        ))}
      </div>
      {cityIsUnknown && (
        <label className="check">
          <input type="checkbox" checked={unknownCity}
                 onChange={(event) => setUnknownCity(event.target.checked)} />
          Nobody knows where it was recorded (the city will be "unknown")
        </label>
      )}
      <label className="check">
        <input type="checkbox" checked={auto} onChange={(event) => setAuto(event.target.checked)} />
        Upload each card as soon as it is scanned
      </label>
      {problems.length > 0 && <p className="muted small">{problems.join(". ")}.</p>}
      <Button kind="primary" disabled={!ready} busy={busy === "open"} onClick={submit}>
        Start session
      </Button>
    </section>
  );
}

function Labelled({ field, children }: { field: Field; children: ReactNode }) {
  return (
    <label className="field">
      <span>{field.label}{field.required ? " *" : ""}</span>
      {children}
      {field.hint && <small className="muted">{field.hint}</small>}
    </label>
  );
}
