export interface BatchItem {
  id: string;
  status: string;
  company_name: string | null;
  domain: string | null;
  role: string | null;
  research_decision: string | null;
  failure_code: string | null;
}

export interface Batch {
  id: string;
  label: string | null;
  status: string;
  created_at: string;
  item_count: number;
  completed_count: number;
  failed_count: number;
  items: BatchItem[];
}

export interface BatchList {
  items: Batch[];
  limit: number;
  offset: number;
}

export interface ResearchCard {
  item_id: string;
  status: string;
  decision: string | null;
  company_name: string | null;
  company_domain: string | null;
  contact_name: string | null;
  contact_email: string | null;
  role: string | null;
  role_fit: string | null;
  summary: string | null;
  failure_code: string | null;
}

export interface DraftDocument {
  artifact_type: "cover_letter" | "email";
  version: number;
  content: string;
  subject: string | null;
  approval: string | null;
}

export interface RunEvent {
  id: string;
  sequence: number;
  event_type: string;
  phase: string | null;
  status: string | null;
  progress: number | null;
  item_id: string | null;
  detail: string | null;
  created_at: string;
}

const apiBase = import.meta.env.VITE_API_BASE_URL ?? "";
const testAuthEnabled = import.meta.env.VITE_ENABLE_TEST_AUTH === "true";

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  if (testAuthEnabled) {
    headers.set("X-Webapp-Test-Auth", "enabled");
  }
  const response = await fetch(`${apiBase}${path}`, {
    ...init,
    headers,
    credentials: "include",
  });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const body: unknown = await response.json();
      if (isRecord(body) && body.detail !== undefined) {
        message = formatErrorDetail(body.detail) ?? message;
      }
    } catch {
      // Preserve the status-based message when a response is not JSON.
    }
    throw new Error(message);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function formatErrorDetail(detail: unknown): string | undefined {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const messages = detail.map((entry) => {
      if (!isRecord(entry)) return typeof entry === "string" ? entry : undefined;
      const message = typeof entry.msg === "string" ? entry.msg : undefined;
      const location = Array.isArray(entry.loc)
        ? entry.loc.filter((part): part is string | number =>
          typeof part === "string" || typeof part === "number",
        ).filter((part) => part !== "body" && part !== "query" && part !== "path").join(".")
        : "";
      if (!message) return undefined;
      return location ? `${location}: ${message}` : message;
    }).filter((message): message is string => Boolean(message));
    if (messages.length) return messages.join("; ");
  }
  if (isRecord(detail)) {
    if (typeof detail.message === "string") return detail.message;
    if (typeof detail.msg === "string") return detail.msg;
    try {
      return JSON.stringify(detail);
    } catch {
      return undefined;
    }
  }
  return undefined;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
