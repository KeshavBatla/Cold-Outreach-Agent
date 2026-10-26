export interface BatchItem {
  id: string;
  status: string;
  mode: "open_role" | "speculative";
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
  mode?: "open_role" | "speculative";
  is_archived?: boolean;
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
  mode: "open_role" | "speculative";
  company_name: string | null;
  company_domain: string | null;
  contact_name: string | null;
  contact_email: string | null;
  role: string | null;
  job_description: string | null;
  role_fit: string | null;
  evaluator_feedback?: string | null;
  eligibility_reason?: string | null;
  summary: string | null;
  failure_code: string | null;
  search_attempt: number;
  search_budget: number;
  candidates_discovered?: number | null;
  candidates_evaluated?: number | null;
  candidates_accepted?: number | null;
  rejected_candidates: Array<{
    status: "rejected";
    company_name: string;
    company_domain: string;
    job_role: string;
    reason: string;
  }>;
}

export interface DraftDocument {
  artifact_type: "cover_letter" | "email";
  version: number;
  content: string;
  subject: string | null;
  approval: string | null;
  delivery_status: string | null;
  delivery_version: number | null;
  delivery_id: string | null;
  delivery_queued_at: string | null;
}

export interface GmailConnection {
  connected: boolean;
  status: "unlinked" | "connected" | "revoked";
  test_mode: boolean;
  live_enabled: boolean;
  account_email: string | null;
}

export interface DeliveryResult {
  id: string;
  status: "queued" | "sending" | "sent" | "failed" | "unknown" | "cancelled" | "simulated";
  draft_version: number;
  provider_message_id: string | null;
  safe_error_code: string | null;
  queued_at: string;
  completed_at: string | null;
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

export interface AuthSession {
  authenticated: boolean;
  user: { login: string; email: string | null } | null;
}

const apiBase = import.meta.env.VITE_API_BASE_URL ?? "";
const testAuthEnabled = import.meta.env.VITE_ENABLE_TEST_AUTH === "true";

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  const method = (init.method ?? "GET").toUpperCase();
  const csrfToken = readCookie("outreach_csrf");
  if (csrfToken && !["GET", "HEAD", "OPTIONS"].includes(method)) {
    headers.set("X-CSRF-Token", csrfToken);
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
    if (response.status === 401 && !path.startsWith("/api/v1/auth/")) {
      window.dispatchEvent(new Event("webapp-auth-required"));
    }
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

function readCookie(name: string): string | undefined {
  const prefix = `${name}=`;
  return document.cookie
    .split(";")
    .map((cookie) => cookie.trim())
    .find((cookie) => cookie.startsWith(prefix))
    ?.slice(prefix.length);
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
