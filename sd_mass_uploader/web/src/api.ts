// Talking to the station: REST for commands and details, one WebSocket for live state.
import { useEffect, useRef, useState } from "react";
import type { StationEvent, StationState } from "./types";

const TOKEN_KEY = "sdu.token";

/** `main.py console` opens /#token=...; keep it for this tab and remove it from the address. */
export function captureToken(): void {
  const match = /(?:^|&)token=([^&]+)/.exec(window.location.hash.slice(1));
  if (match) {
    try {
      sessionStorage.setItem(TOKEN_KEY, decodeURIComponent(match[1]));
    } catch {
      memoryToken = decodeURIComponent(match[1]);
    }
    history.replaceState(null, "", window.location.pathname + window.location.search);
  }
}

let memoryToken: string | null = null;

export function getToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY) ?? memoryToken;
  } catch {
    return memoryToken;
  }
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function api<T = unknown>(method: string, path: string, body?: unknown,
                                       raw?: Blob): Promise<T> {
  const headers: Record<string, string> = { Authorization: `Bearer ${getToken() ?? ""}` };
  let payload: BodyInit | undefined;
  if (raw) {
    headers["Content-Type"] = "application/zip";
    payload = raw;
  } else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  let response: Response;
  try {
    response = await fetch(path, { method, headers, body: payload });
  } catch {
    throw new ApiError(0, "The station is not answering.");
  }
  const text = await response.text();
  const data = text ? safeJson(text) : null;
  if (!response.ok) {
    const detail = data && typeof data === "object" && "detail" in data
      ? describeDetail((data as { detail: unknown }).detail) : response.statusText;
    throw new ApiError(response.status, detail);
  }
  return data as T;
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

function describeDetail(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((item) => (item && typeof item === "object" && "msg" in item
      ? String((item as { msg: unknown }).msg) : String(item))).join("; ");
  }
  return "The station refused this.";
}

export type Connection = "connecting" | "live" | "offline" | "signed-out";

/** Live station state from the WebSocket, reconnecting by itself. */
export function useStation(): { state: StationState | null; events: StationEvent[];
                                 connection: Connection; lastUpdate: number } {
  const [state, setState] = useState<StationState | null>(null);
  const [events, setEvents] = useState<StationEvent[]>([]);
  const [connection, setConnection] = useState<Connection>("connecting");
  const [lastUpdate, setLastUpdate] = useState(0);
  const retry = useRef(1000);

  useEffect(() => {
    let socket: WebSocket | null = null;
    let timer: number | undefined;
    let closed = false;

    const connect = () => {
      if (!getToken()) {
        setConnection("signed-out");
        return;
      }
      const scheme = window.location.protocol === "https:" ? "wss" : "ws";
      socket = new WebSocket(`${scheme}://${window.location.host}/api/v1/ws`);
      socket.onopen = () => socket?.send(JSON.stringify({ type: "auth", token: getToken() }));
      socket.onmessage = (message) => {
        const data = JSON.parse(message.data as string);
        if (data.type === "state") {
          setState(data.state as StationState);
          setLastUpdate(Date.now());
          setConnection("live");
          retry.current = 1000;
        } else if (data.type === "event") {
          setEvents((list) => [data.event as StationEvent, ...list].slice(0, 200));
        }
      };
      socket.onclose = (closing) => {
        if (closed) return;
        if (closing.code === 4401) {
          setConnection("signed-out");
          return;
        }
        setConnection("offline");
        timer = window.setTimeout(connect, retry.current);
        retry.current = Math.min(retry.current * 2, 10000);
      };
    };
    connect();
    return () => {
      closed = true;
      window.clearTimeout(timer);
      socket?.close();
    };
  }, []);

  return { state, events, connection, lastUpdate };
}
