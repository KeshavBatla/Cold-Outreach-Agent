import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";
import {
  api,
  type AuthSession,
  type Batch,
  type BatchList,
  type DeliveryResult,
  type DraftDocument,
  type GmailConnection,
  type ResearchCard,
  type RunEvent,
} from "./api";

function App() {
  const [authLoading, setAuthLoading] = useState(true);
  const [authUser, setAuthUser] = useState<AuthSession["user"]>(null);
  const [gmailConnection, setGmailConnection] = useState<GmailConnection | null>(null);
  const [sendConfirmation, setSendConfirmation] = useState<{
    document: DraftDocument;
    batchId: string;
    itemId: string;
    recipient: string;
    attachments: string[];
    connectedEmail?: string;
  } | null>(null);
  const [loginName, setLoginName] = useState("");
  const [loginPassword, setLoginPassword] = useState("");
  const [batches, setBatches] = useState<Batch[]>([]);
  const [showArchived, setShowArchived] = useState(false);
  const [selectedBatchId, setSelectedBatchId] = useState(
    new URLSearchParams(window.location.search).get("batch") ?? "",
  );
  const [batch, setBatch] = useState<Batch | null>(null);
  const [research, setResearch] = useState<ResearchCard[]>([]);
  const [documents, setDocuments] = useState<DraftDocument[]>([]);
  const [attachments, setAttachments] = useState<string[]>([]);
  const [selectedItemId, setSelectedItemId] = useState("");
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [eventCursor, setEventCursor] = useState(0);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(false);
  const [discoveryMode, setDiscoveryMode] = useState<"open_role" | "speculative">("open_role");
  const [companyName, setCompanyName] = useState("");
  const [companyDomain, setCompanyDomain] = useState("");
  const [role, setRole] = useState("");
  const [jobDescription, setJobDescription] = useState("");
  const [candidateProfile, setCandidateProfile] = useState("");
  const [targetSeniority, setTargetSeniority] = useState("");
  const [targetDomainKeywords, setTargetDomainKeywords] = useState("");
  const [locationPreference, setLocationPreference] = useState("");
  const [batchLabel, setBatchLabel] = useState("");
  const [cvFile, setCvFile] = useState<File | null>(null);
  const [exampleFile, setExampleFile] = useState<File | null>(null);

  const selectedBatchIdRef = useRef(selectedBatchId);
  useEffect(() => {
    selectedBatchIdRef.current = selectedBatchId;
  }, [selectedBatchId]);
  const eventCursorRef = useRef(0);

  const selectedResearch = useMemo(
    () => research.find((item) => item.item_id === selectedItemId) ?? research[0] ?? null,
    [research, selectedItemId],
  );

  const loadBatches = useCallback(async () => {
    const result = await api<BatchList>(`/api/v1/batches?limit=50&include_archived=${showArchived}`);
    setBatches(result.items);
    if (!selectedBatchId && result.items.length) {
      setSelectedBatchId(result.items[0].id);
    }
  }, [selectedBatchId, showArchived]);

  const loadBatch = useCallback(async (batchId: string, cursor: number) => {
    const [summary, cards, newEvents] = await Promise.all([
      api<Batch>(`/api/v1/batches/${batchId}`),
      api<ResearchCard[]>(`/api/v1/batches/${batchId}/research-results`),
      api<RunEvent[]>(`/api/v1/batches/${batchId}/events?after=${cursor}`),
    ]);
    if (selectedBatchIdRef.current !== batchId) return;
    setBatch(summary);
    setResearch(cards);
    setSelectedItemId((current) =>
      cards.some((item) => item.item_id === current) ? current : cards[0]?.item_id ?? "",
    );
    if (newEvents.length) {
      setEvents((current) => {
        const seen = new Set(current.map((e) => e.id));
        const added = newEvents.filter((e) => !seen.has(e.id));
        const combined = [...current, ...added];
        combined.sort((a, b) => a.sequence - b.sequence);
        return combined;
      });
      const maxSeq = Math.max(...newEvents.map((e) => e.sequence));
      setEventCursor(maxSeq);
      eventCursorRef.current = maxSeq;
    }
  }, []);

  useEffect(() => {
    setEvents([]);
    setEventCursor(0);
    eventCursorRef.current = 0;
  }, [selectedBatchId]);

  const loadDrafts = useCallback(async (batchId: string, itemId: string) => {
    const result = await api<{ item_id: string; documents: DraftDocument[]; attachments?: string[] }>(
      `/api/v1/batches/${batchId}/items/${itemId}/drafts`,
    );
    setDocuments(result.documents);
    setAttachments(result.attachments ?? []);
  }, []);

  useEffect(() => {
    let active = true;
    api<AuthSession>("/api/v1/auth/session")
      .then((result) => {
        if (active) setAuthUser(result.authenticated ? result.user : null);
      })
      .catch((cause: unknown) => {
        if (active) setError(errorMessage(cause));
      })
      .finally(() => {
        if (active) setAuthLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    const requireLogin = () => {
      setAuthUser(null);
      setError("Your session expired. Sign in again to continue.");
    };
    window.addEventListener("webapp-auth-required", requireLogin);
    return () => window.removeEventListener("webapp-auth-required", requireLogin);
  }, []);

  useEffect(() => {
    if (!authUser) return;
    let active = true;
    api<BatchList>(`/api/v1/batches?limit=50&include_archived=${showArchived}`)
      .then((result) => {
        if (!active) return;
        setBatches(result.items);
        const desired = selectedBatchId || result.items[0]?.id || "";
        if (desired) setSelectedBatchId(desired);
      })
      .catch((cause: unknown) => {
        if (active) setError(errorMessage(cause));
      });
    return () => {
      active = false;
    };
  }, [authUser, showArchived]);

  useEffect(() => {
    if (!authUser) {
      setGmailConnection(null);
      return;
    }
    let active = true;
    api<GmailConnection>("/api/v1/gmail/connection")
      .then((result) => {
        if (active) setGmailConnection(result);
      })
      .catch((cause: unknown) => {
        if (active) setError(errorMessage(cause));
      });
    return () => {
      active = false;
    };
  }, [authUser]);

  useEffect(() => {
    const callbackState = new URLSearchParams(window.location.search).get("gmail");
    if (!callbackState) return;
    if (callbackState === "connected") setNotice("Gmail is connected. Test mode and live-send settings are shown in the connection panel.");
    else if (callbackState === "authorization-cancelled") setNotice("Gmail connection was cancelled.");
    else if (callbackState === "authorization-failed") setError("Gmail authorization could not be completed. Review the backend configuration and try again.");
    const url = new URL(window.location.href);
    url.searchParams.delete("gmail");
    window.history.replaceState({}, "", url);
  }, []);

  useEffect(() => {
    if (!authUser || !selectedBatchId) {
      setBatch(null);
      setResearch([]);
      setDocuments([]);
      setEvents([]);
      setEventCursor(0);
      return;
    }
    const url = new URL(window.location.href);
    url.searchParams.set("batch", selectedBatchId);
    window.history.replaceState({}, "", url);
    let active = true;
    const refresh = async () => {
      try {
        await loadBatch(selectedBatchId, eventCursorRef.current);
        if (selectedItemId) await loadDrafts(selectedBatchId, selectedItemId);
        if (active) {
          await loadBatches();
          setError("");
        }
      } catch (cause) {
        if (active) setError(errorMessage(cause));
      }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 4000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [authUser, selectedBatchId, selectedItemId, loadBatch, loadBatches, loadDrafts]);

  useEffect(() => {
    if (!authUser || !selectedBatchId || !selectedItemId) {
      setDocuments([]);
      return;
    }
    void loadDrafts(selectedBatchId, selectedItemId).catch((cause: unknown) => {
      setDocuments([]);
      if (!isNotFoundOrEmpty(cause)) setError(errorMessage(cause));
    });
  }, [authUser, selectedBatchId, selectedItemId, loadDrafts]);

  async function signIn(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setLoading(true);
    setError("");
    try {
      const result = await api<AuthSession>("/api/v1/auth/login", {
        method: "POST",
        body: JSON.stringify({ login: loginName, password: loginPassword }),
      });
      setAuthUser(result.user);
      setLoginPassword("");
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setLoading(false);
    }
  }

  async function signOut() {
    setLoading(true);
    try {
      await api<void>("/api/v1/auth/logout", { method: "POST" });
      setAuthUser(null);
      setGmailConnection(null);
      setSendConfirmation(null);
      setBatches([]);
      setSelectedBatchId("");
      setBatch(null);
      setResearch([]);
      setDocuments([]);
      setEvents([]);
      setError("");
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setLoading(false);
    }
  }

  async function connectGmail() {
    setLoading(true);
    setError("");
    try {
      const result = await api<{ authorization_url: string }>("/api/v1/gmail/oauth/start", {
        method: "POST",
      });
      window.location.assign(result.authorization_url);
    } catch (cause) {
      setError(errorMessage(cause));
      setLoading(false);
    }
  }

  async function disconnectGmail() {
    await runAction(async () => {
      await api<void>("/api/v1/gmail/disconnect", { method: "POST" });
      await loadGmailConnection();
      setNotice("Gmail was disconnected and its stored token was removed.");
    });
  }

  async function loadGmailConnection() {
    const result = await api<GmailConnection>("/api/v1/gmail/connection");
    setGmailConnection(result);
  }

  async function confirmEmailSend() {
    if (!sendConfirmation) return;
    const target = sendConfirmation;
    const draft = target.document;
    await runAction(async () => {
      const result = await api<DeliveryResult>(
        `/api/v1/gmail/batches/${target.batchId}/items/${target.itemId}/send`,
        {
          method: "POST",
          body: JSON.stringify({ draft_version: draft.version, confirmed: true }),
        },
      );
      await loadDrafts(target.batchId, target.itemId);
      await loadGmailConnection();
      setSendConfirmation(null);
      setNotice(deliveryNotice(result));
    });
  }

  async function resolveInterruptedDelivery(document: DraftDocument) {
    if (!document.delivery_id) return;
    const confirmed = window.confirm(
      "Check the connected Gmail account's Sent folder first. If you cannot confirm delivery, mark this attempt unknown? It will not be retried.",
    );
    if (!confirmed) return;
    await runAction(async () => {
      await api(
        `/api/v1/gmail/deliveries/${document.delivery_id}/resolve-unknown`,
        {
          method: "POST",
          body: JSON.stringify({ checked_sent_folder: true }),
        },
      );
      if (selectedBatchId && selectedItemId) {
        await loadDrafts(selectedBatchId, selectedItemId);
      }
      setNotice("Delivery marked unknown. It cannot be retried; keep checking Gmail Sent if the outcome remains unclear.");
    });
  }

  async function toggleArchiveBatch(batchId: string, isCurrentlyArchived: boolean) {
    await runAction(async () => {
      const endpoint = isCurrentlyArchived ? "unarchive" : "archive";
      await api(`/api/v1/batches/${batchId}/${endpoint}`, { method: "POST" });
      await loadBatches();
      setNotice(isCurrentlyArchived ? "Batch restored from archive." : "Batch moved to archive.");
    });
  }

  async function createBatch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!cvFile) {
      setError("Upload a CV before creating a batch.");
      return;
    }
    const targetCompany = companyName.trim();
    const targetDomain = companyDomain.trim();
    if (discoveryMode === "open_role" && !targetCompany && !targetDomain) {
      setError("For named-company research, enter a company name or domain.");
      return;
    }
    setLoading(true);
    setError("");
    setNotice("");
    try {
      const cvForm = new FormData();
      cvForm.set("file", cvFile);
      cvForm.set("artifact_type", "cv");
      const cv = await api<{ id: string }>("/api/v1/artifacts?artifact_type=cv", { method: "POST", body: cvForm });
      let exampleLetterId: string | undefined;
      if (exampleFile) {
        const exampleForm = new FormData();
        exampleForm.set("file", exampleFile);
        exampleForm.set("artifact_type", "example_letter");
        const example = await api<{ id: string }>("/api/v1/artifacts?artifact_type=example_letter", {
          method: "POST",
          body: exampleForm,
        });
        exampleLetterId = example.id;
      }
      const created = await api<Batch>("/api/v1/batches", {
        method: "POST",
        body: JSON.stringify({
          label: batchLabel.trim() || `${targetCompany || targetDomain || "Opportunity discovery"} · ${new Date().toLocaleDateString()}`,
          candidate_profile: candidateProfile.trim() || undefined,
          mode: discoveryMode,
          target_seniority: targetSeniority.trim() || undefined,
          target_domain_keywords: targetDomainKeywords.trim() || undefined,
          location_preference: locationPreference.trim() || undefined,
          cv_artifact_id: cv.id,
          example_letter_artifact_id: exampleLetterId,
          items: [
            {
              company_name: targetCompany || undefined,
              domain: targetDomain || undefined,
              role: role.trim() || undefined,
              job_description: jobDescription.trim() || undefined,
            },
          ],
        }),
      });
      setSelectedBatchId(created.id);
      setDiscoveryMode("open_role");
      setCompanyName("");
      setCompanyDomain("");
      setRole("");
      setJobDescription("");
      setCandidateProfile("");
      setTargetSeniority("");
      setTargetDomainKeywords("");
      setLocationPreference("");
      setBatchLabel("");
      setCvFile(null);
      setExampleFile(null);
      setNotice("Batch queued. Progress and review state are loaded from the server.");
      await loadBatches();
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setLoading(false);
    }
  }

  async function researchDecision(item: ResearchCard, decision: "approve" | "reject") {
    await runAction(async () => {
      await api(`/api/v1/batches/${selectedBatchId}/items/${item.item_id}/research-decision`, {
        method: "POST",
        body: JSON.stringify({ decision }),
      });
      await loadBatch(selectedBatchId, eventCursor);
      if (decision === "approve") {
        await loadDrafts(selectedBatchId, item.item_id).catch(() => {});
      }
      setNotice(decision === "approve" ? "Research approved. Draft generation will follow." : "Research rejected.");
    });
  }

  async function retryItem(itemId: string) {
    await runAction(async () => {
      await api(`/api/v1/batches/${selectedBatchId}/items/${itemId}/retry`, {
        method: "POST",
      });
      await loadBatch(selectedBatchId, eventCursor);
      setNotice("An explicit retry was queued. The saved run state is authoritative.");
    });
  }

  async function regenerateCurrentDrafts() {
    if (!selectedBatchId || !selectedItemId) return;
    await runAction(async () => {
      await api(`/api/v1/batches/${selectedBatchId}/drafts/generate`, {
        method: "POST",
        body: JSON.stringify({ regenerate_item_ids: [selectedItemId] }),
      });
      await loadBatch(selectedBatchId, eventCursor);
      await loadDrafts(selectedBatchId, selectedItemId);
      setNotice("Regeneration queued. Approval for the previous versions is no longer current.");
    });
  }

  async function saveDocument(document: DraftDocument, content: string, subject: string) {
    await runAction(async () => {
      await api(
        `/api/v1/batches/${selectedBatchId}/items/${selectedItemId}/drafts/${document.version}?artifact_type=${document.artifact_type}`,
        {
          method: "PUT",
          body: JSON.stringify({
            content,
            subject: document.artifact_type === "email" ? subject : null,
            expected_current_version: document.version,
          }),
        },
      );
      await loadDrafts(selectedBatchId, selectedItemId);
      await loadBatch(selectedBatchId, eventCursor);
      setNotice("Saved as a new version. Approval for the previous version no longer applies.");
    });
  }

  async function decideDraft(document: DraftDocument, decision: "approved" | "rejected") {
    await runAction(async () => {
      await api(`/api/v1/batches/${selectedBatchId}/items/${selectedItemId}/approvals`, {
        method: "POST",
        body: JSON.stringify({
          artifact_type: document.artifact_type,
          draft_version: document.version,
          decision,
        }),
      });
      await loadDrafts(selectedBatchId, selectedItemId);
      await loadBatch(selectedBatchId, eventCursor);
      setNotice(
        decision === "approved"
          ? `${title(document.artifact_type)} approved. Both drafts must be approved before email sending.`
          : `${title(document.artifact_type)} rejected. Replacement draft generation queued.`
      );
    });
  }

  async function approveBoth() {
    const letter = documents.find((document) => document.artifact_type === "cover_letter");
    const email = documents.find((document) => document.artifact_type === "email");
    if (!letter || !email) {
      setError("Both current draft artifacts must be present before combined approval.");
      return;
    }
    await runAction(async () => {
      await api(`/api/v1/batches/${selectedBatchId}/items/${selectedItemId}/approvals/combined`, {
        method: "POST",
        body: JSON.stringify({
          cover_letter_version: letter.version,
          email_version: email.version,
        }),
      });
      await loadDrafts(selectedBatchId, selectedItemId);
      await loadBatch(selectedBatchId, eventCursor);
      setNotice("Both current drafts approved. Sending is not available in this milestone.");
    });
  }

  async function runAction(action: () => Promise<void>) {
    setLoading(true);
    setError("");
    setNotice("");
    try {
      await action();
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setLoading(false);
    }
  }

  if (authLoading) {
    return <main className="shell auth-shell"><p role="status">Checking your session…</p></main>;
  }

  if (!authUser) {
    return (
      <main className="shell auth-shell">
        <header className="topbar">
          <div className="brand">
            <span className="brand-mark">O</span>
            <div><strong>Outreach Studio</strong><span>Private workspace</span></div>
          </div>
        </header>
        <section className="panel auth-panel">
          <p className="eyebrow">SIGN IN</p>
          <h1>Access your outreach workspace.</h1>
          {error && <div className="notice error" role="alert">{error}</div>}
          <form onSubmit={(event) => void signIn(event)} className="create-form">
            <label>
              Login
              <input
                autoComplete="username"
                required
                value={loginName}
                onChange={(event) => setLoginName(event.target.value)}
              />
            </label>
            <label>
              Password
              <input
                type="password"
                autoComplete="current-password"
                required
                value={loginPassword}
                onChange={(event) => setLoginPassword(event.target.value)}
              />
            </label>
            <button className="primary-button" type="submit" disabled={loading}>
              {loading ? "Signing in…" : "Sign in"}
            </button>
          </form>
        </section>
      </main>
    );
  }

  return (
    <main className="shell">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark">O</span>
          <div>
            <strong>Outreach Studio</strong>
            <span>Research · review · draft</span>
          </div>
        </div>
        <div className="topbar-status">
          <span className="status-dot" />
          <span>Human approval required</span>
          <span>{authUser.login}</span>
          <button className="text-button" onClick={() => void signOut()} disabled={loading}>Sign out</button>
        </div>
      </header>

      <section className="hero">
        <div>
          <p className="eyebrow">WORKSPACE</p>
          <h1>Thoughtful outreach, under your control.</h1>
              <p className="hero-copy">
                Research opportunities, review persisted findings, edit drafts, and send an individually confirmed email.
          </p>
        </div>
        {batch && (
          <div className="hero-stat">
            <span className="stat-label">Current batch</span>
            <strong>{batch.item_count}</strong>
            <span>contact{batch.item_count === 1 ? "" : "s"} · {humanize(batch.status)}</span>
          </div>
        )}
      </section>

      {error && <div className="notice error" role="alert">{error}</div>}
      {notice && <div className="notice success" role="status">{notice}</div>}

      <div className="workspace">
        <aside className="sidebar">
          <section className="panel gmail-panel">
            <div className="section-heading">
              <div><p className="eyebrow">EMAIL DELIVERY</p><h2>Gmail connection</h2></div>
              <span className={`badge small ${gmailConnection?.connected ? "completed" : "queued"}`}>
                {gmailConnection?.connected ? "Connected" : humanize(gmailConnection?.status ?? "loading")}
              </span>
            </div>
            <p className="muted">
              {gmailConnection?.test_mode
                ? "Test mode is on: confirmed sends are recorded as simulations and never contact Gmail."
                : gmailConnection?.live_enabled
                  ? "Live delivery is enabled. Each email still requires a separate confirmation."
                  : "Live delivery is disabled by server configuration."}
            </p>
            {gmailConnection?.connected ? (
              <button className="secondary-button" disabled={loading} onClick={() => void disconnectGmail()}>
                Disconnect and revoke
              </button>
            ) : (
              <button className="secondary-button" disabled={loading} onClick={() => void connectGmail()}>
                Connect Gmail
              </button>
            )}
            <p className="privacy-note">
              Requires a Google OAuth web client and backend settings: GOOGLE_OAUTH_CLIENT_ID,
              GOOGLE_OAUTH_CLIENT_SECRET, GOOGLE_OAUTH_REDIRECT_URI, and LANGGRAPH_AES_KEY.
              Only the Gmail send scope is requested; tokens stay encrypted on the server.
            </p>
          </section>
          <section className="panel create-panel">
            <div className="section-heading">
              <div>
                <p className="eyebrow">NEW BATCH</p>
                <h2>Start with one contact</h2>
              </div>
              <span className="step-number">01</span>
            </div>
            <form onSubmit={(event) => void createBatch(event)} className="create-form">
              <label>
                Discovery mode
                <select
                  value={discoveryMode}
                  onChange={(event) => setDiscoveryMode(event.target.value as "open_role" | "speculative")}
                >
                  <option value="open_role">Named company / open role</option>
                  <option value="speculative">Speculative opportunity discovery</option>
                </select>
              </label>
              <p className="mode-help">
                {discoveryMode === "open_role"
                  ? "Research a specific company or domain. Provide at least one target."
                  : "Discover suitable opportunities without a company or domain. Your CV and optional profile, seniority, keywords, and location guide research."}
              </p>
              <p className="mode-help">
                Research runs require the Celery worker, Redis, and PostgreSQL. SQLite can save batches but cannot process research jobs.
              </p>
              <label>
                Batch name <span className="optional">optional</span>
                <input value={batchLabel} onChange={(event) => setBatchLabel(event.target.value)} placeholder="e.g. Product roles — October" />
              </label>
              {discoveryMode === "open_role" && (
                <>
                  <label>
                    Company name <span className="optional">optional</span>
                    <input value={companyName} onChange={(event) => setCompanyName(event.target.value)} placeholder="Company name" />
                  </label>
                  <label>
                    Company domain <span className="optional">optional</span>
                    <input value={companyDomain} onChange={(event) => setCompanyDomain(event.target.value)} placeholder="example.com" />
                  </label>
                  <label>
                    Role <span className="optional">optional</span>
                    <input value={role} onChange={(event) => setRole(event.target.value)} placeholder="Target role" />
                  </label>
                  <label>
                    Job description <span className="optional">optional</span>
                    <textarea rows={3} value={jobDescription} onChange={(event) => setJobDescription(event.target.value)} placeholder="Paste role context if available" />
                  </label>
                </>
              )}
              <label>
                CV <span className="required">required</span>
                <input type="file" accept=".pdf,.docx,.txt" onChange={(event) => setCvFile(event.target.files?.[0] ?? null)} />
              </label>
              {discoveryMode === "speculative" && (
                <>
                  <label>
                    Candidate profile <span className="optional">optional</span>
                    <textarea rows={4} value={candidateProfile} onChange={(event) => setCandidateProfile(event.target.value)} placeholder="Add candidate skills and experience, or leave blank to derive a profile from the CV." />
                  </label>
                  <label>
                    Target seniority <span className="optional">optional</span>
                    <input value={targetSeniority} onChange={(event) => setTargetSeniority(event.target.value)} placeholder="Intern, junior, fresher" />
                  </label>
                  <label>
                    Target domain keywords <span className="optional">optional</span>
                    <input value={targetDomainKeywords} onChange={(event) => setTargetDomainKeywords(event.target.value)} placeholder="Agentic AI, LLM systems, machine learning" />
                  </label>
                  <label>
                    Location preference <span className="optional">optional</span>
                    <input value={locationPreference} onChange={(event) => setLocationPreference(event.target.value)} placeholder="Remote, India, US" />
                  </label>
                </>
              )}
              <label>
                Example cover letter <span className="optional">optional</span>
                <input type="file" accept=".pdf,.docx,.txt" onChange={(event) => setExampleFile(event.target.files?.[0] ?? null)} />
              </label>
              {discoveryMode === "open_role" && (
                <>
                  <label>
                    Target seniority <span className="optional">optional</span>
                    <input value={targetSeniority} onChange={(event) => setTargetSeniority(event.target.value)} placeholder="Intern, junior, fresher" />
                  </label>
                  <label>
                    Target domain keywords <span className="optional">optional</span>
                    <input value={targetDomainKeywords} onChange={(event) => setTargetDomainKeywords(event.target.value)} placeholder="Agentic AI, LLM systems, machine learning" />
                  </label>
                  <label>
                    Location preference <span className="optional">optional</span>
                    <input value={locationPreference} onChange={(event) => setLocationPreference(event.target.value)} placeholder="Remote, India, US" />
                  </label>
                </>
              )}
              <p className="privacy-note muted">Candidate name is extracted automatically from the uploaded CV.</p>
              <button className="primary-button" type="submit" disabled={loading}>
                {loading ? "Working…" : "Create research batch"}
                <span aria-hidden="true">↗</span>
              </button>
            </form>
            <p className="privacy-note">Files are stored privately on the backend. Only artifact references are returned to the browser.</p>
          </section>

          <section className="panel history-panel">
            <div className="section-heading">
              <div>
                <p className="eyebrow">HISTORY</p>
                <h2>Your batches</h2>
              </div>
              <div className="batch-list-controls">
                <span className="count-pill">{batches.length}</span>
                <button
                  className="text-button compact"
                  onClick={() => setShowArchived((v) => !v)}
                  title={showArchived ? "Hide archived batches" : "Show archived batches"}
                >
                  {showArchived ? "Hide archived" : "Show archived"}
                </button>
              </div>
            </div>
            {batches.length === 0 ? (
              <p className="muted empty-history">New batches will appear here.</p>
            ) : (
              <div className="batch-list">
                {batches.map((entry) => (
                  <div
                    className={`batch-row-wrapper ${entry.is_archived ? "archived" : ""}`}
                    key={entry.id}
                  >
                    <button
                      className={`batch-row ${selectedBatchId === entry.id ? "selected" : ""}`}
                      onClick={() => setSelectedBatchId(entry.id)}
                    >
                      <span className="batch-icon">{entry.item_count === 1 ? "↗" : "▦"}</span>
                      <span className="batch-row-copy">
                        <strong>{entry.label || "Outreach batch"}</strong>
                        <small>
                          <span className={`mode-tag ${entry.mode || "open_role"}`}>
                            {entry.mode === "speculative" ? "Speculative" : "Named company"}
                          </span>
                          {" · "}
                          {entry.item_count} item{entry.item_count === 1 ? "" : "s"} · {humanize(entry.status)}{entry.is_archived ? " · archived" : ""}
                        </small>
                      </span>
                      <span className={`mini-status ${entry.status}`} />
                    </button>
                    <button
                      className="text-button compact archive-btn"
                      title={entry.is_archived ? "Restore batch" : "Archive batch"}
                      onClick={(e) => { e.stopPropagation(); void toggleArchiveBatch(entry.id, Boolean(entry.is_archived)); }}
                    >
                      {entry.is_archived ? "↩ Restore" : "Archive"}
                    </button>
                  </div>
                ))}
              </div>
            )}
          </section>
        </aside>

        <div className="main-column">
          {!batch ? (
            <section className="panel empty-state">
              <span className="empty-icon">✳</span>
              <p className="eyebrow">READY WHEN YOU ARE</p>
              <h2>Create your first research batch</h2>
              <p>Start with one contact. The same review flow will scale to larger batches.</p>
            </section>
          ) : (
            <>
              <section className="panel overview-panel">
                <div className="overview-top">
                  <div>
                    <p className="eyebrow">BATCH OVERVIEW</p>
                    <div className="title-row">
                      <h2>{batch.label || "Outreach batch"}</h2>
                      <span className={`mode-badge ${batch.mode || "open_role"}`}>
                        {batch.mode === "speculative" ? "Speculative discovery" : "Named company research"}
                      </span>
                    </div>
                    <p className="muted">{new Date(batch.created_at).toLocaleString()}</p>
                  </div>
                  <span className={`badge ${batch.status}`}>{humanize(batch.status)}</span>
                </div>
                <div className="progress-track" aria-label={`${batch.completed_count} of ${batch.item_count} complete`}>
                  <span style={{ width: `${batch.item_count ? Math.round((batch.completed_count / batch.item_count) * 100) : 0}%` }} />
                </div>
                <div className="metric-row">
                  <Metric label="Contacts" value={batch.item_count} />
                  <Metric label="Completed" value={batch.completed_count} />
                  <Metric label="Failed" value={batch.failed_count} />
                  <Metric label="Current phase" value={humanize(batch.status)} />
                </div>
              </section>

              <section className="panel research-panel">
                <div className="section-heading">
                  <div>
                    <p className="eyebrow">01 / RESEARCH REVIEW</p>
                    <h2>Research results</h2>
                  </div>
                  <span className="count-pill">{research.length} result{research.length === 1 ? "" : "s"}</span>
                </div>
                {research.length === 0 ? (
                  <div className="inline-empty">
                    <span className="loader" />
                    <div><strong>Research is queued</strong><p>Results and progress will appear here as the worker persists them.</p></div>
                  </div>
                ) : (
                  <div className="research-grid" data-count={research.length}>
                    {research.map((card) => (
                      <article className="research-card" key={card.item_id}>
                        <div className="card-heading">
                          <div className="company-avatar">{(card.company_name || "?").slice(0, 1).toUpperCase()}</div>
                          <div className="card-title">
                            <div className="card-title-row">
                              <h3>{card.company_name || "Company research"}</h3>
                              <span className={`mode-pill-small ${card.mode || "open_role"}`}>
                                {card.mode === "speculative" ? "Speculative" : "Named company"}
                              </span>
                            </div>
                            <span>{card.company_domain || "Domain pending"}</span>
                          </div>
                          <span className={`badge small ${card.status}`}>{humanize(card.status)}</span>
                        </div>
                        <div className="role-row">
                          <span className="role-icon">⌘</span>
                          <div><small>ROLE</small><strong>{card.role || "Role discovery in progress"}</strong></div>
                          <span className={`fit-pill ${card.role_fit || "pending"}`}>{humanize(card.role_fit || "pending")}</span>
                        </div>
                        {(card.eligibility_reason || card.evaluator_feedback) && (
                          <div className="eligibility-feedback-block">
                            <small>ELIGIBILITY EVALUATION</small>
                            <p>{card.eligibility_reason || card.evaluator_feedback}</p>
                          </div>
                        )}
                        <div className="contact-block">
                          <small>CONTACT</small>
                          <strong>{card.contact_name || "Contact not available yet"}</strong>
                          {card.contact_email && <span>{card.contact_email}</span>}
                        </div>
                        {card.job_description && <p className="summary-text">{card.job_description}</p>}
                        <p className="summary-text">
                          {card.summary || (
                            card.failure_code
                              ? researchFailureMessage(card.failure_code, card.search_attempt, card.search_budget)
                              : researchStatusMessage(card.status)
                          )}
                        </p>
                        {card.status === "researching" && card.mode === "speculative" && card.search_budget > 0 && (
                          <p className="mode-help">
                            Searching opportunity {Math.max(1, card.search_attempt)} of {card.search_budget}.
                          </p>
                        )}
                        {card.rejected_candidates.length > 0 && (
                          <div className="contact-block">
                            <small>SKIPPED OPPORTUNITIES</small>
                            {card.rejected_candidates.map((candidate, index) => (
                              <span key={`${candidate.company_name}-${candidate.job_role}-${index}`}>
                                Rejected: {candidate.company_name || candidate.company_domain || "Company"} — {candidate.job_role || "Role not identified"}: {candidate.reason}
                              </span>
                            ))}
                          </div>
                        )}
                        {card.status === "research_review" ? (
                          <div className="button-row">
                            <button className="secondary-button" disabled={loading} onClick={() => void researchDecision(card, "reject")}>Reject</button>
                            <button className="primary-button compact" disabled={loading} onClick={() => void researchDecision(card, "approve")}>Approve research <span>→</span></button>
                          </div>
                        ) : card.decision && <p className="decision-note">Research {card.decision}. Decisions are saved per contact.</p>}
                        {card.status === "failed" && (
                          <button
                            className="secondary-button"
                            disabled={loading}
                            onClick={() => void retryItem(card.item_id)}
                          >
                            Retry this item
                          </button>
                        )}
                      </article>
                    ))}
                  </div>
                )}
                {research.some((card) => card.decision === "approved") && (
                  <div className="draft-start-row">
                    <div>
                      <strong>Research-approved items</strong>
                      <span>Draft generation starts after the saved research review resumes. Cover letters are generated before their dependent emails.</span>
                    </div>
                  </div>
                )}
              </section>

              <section className="panel draft-panel">
                <div className="section-heading">
                  <div>
                    <p className="eyebrow">02 / COMBINED DRAFT REVIEW</p>
                    <h2>Cover letter & email</h2>
                  </div>
                  {selectedResearch && <span className="count-pill">{selectedResearch.company_name || selectedResearch.role || "Selected contact"}</span>}
                </div>
                {research.filter((card) => card.decision === "approved").length > 1 && (
                  <nav className="item-switcher" aria-label="Approved contacts">
                    {research.filter((card) => card.decision === "approved").map((card, index) => (
                      <button
                        key={card.item_id}
                        className={selectedItemId === card.item_id ? "active" : ""}
                        onClick={() => setSelectedItemId(card.item_id)}
                      >
                        {index + 1}. {card.contact_name || card.company_name || "Contact"}
                      </button>
                    ))}
                  </nav>
                )}
                {documents.length === 0 ? (
                  <div className="inline-empty">
                    <span className="document-icon">▤</span>
                    <div><strong>No drafts loaded for this contact yet</strong><p>Approve the research and start draft generation. Drafting runs cover letter first, then the dependent email.</p></div>
                  </div>
                ) : (
                  <>
                    <div className="document-grid">
                      {(["cover_letter", "email"] as const).map((kind) => {
                        const document = documents.find((entry) => entry.artifact_type === kind);
                        return document ? (
                          <DraftEditor
                            key={`${kind}-${document.version}`}
                            document={document}
                            disabled={loading}
                            recipient={selectedResearch?.contact_email ?? null}
                            gmailConnected={Boolean(gmailConnection?.connected)}
                            deliveryStatus={document.delivery_status}
                            deliveryQueuedAt={document.delivery_queued_at}
                            canSend={
                              document.artifact_type === "email"
                              && selectedResearch?.decision === "approved"
                              && gmailConnection?.connected === true
                              && (gmailConnection.test_mode || gmailConnection.live_enabled)
                              && isValidEmail(selectedResearch?.contact_email)
                              && !document.delivery_status
                            }
                            onSave={(content, subject) => void saveDocument(document, content, subject)}
                            onDecision={(decision) => void decideDraft(document, decision)}
                            onSend={() => selectedBatchId && selectedItemId && setSendConfirmation({
                              document,
                              batchId: selectedBatchId,
                              itemId: selectedItemId,
                              recipient: selectedResearch?.contact_email ?? "",
                              attachments: attachments,
                              connectedEmail: gmailConnection?.account_email ?? undefined,
                            })}
                            onResolveUnknown={() => void resolveInterruptedDelivery(document)}
                          />
                        ) : (
                          <div className="document-card pending-document" key={kind}>
                            <h3>{title(kind)}</h3><p>Waiting for this artifact to be generated.</p>
                          </div>
                        );
                      })}
                    </div>
                    <div className="draft-footer">
                      <p>Approvals apply to exact saved versions. Editing or regeneration requires review again.</p>
                      <div className="button-row">
                        {selectedResearch?.decision === "approved" && (
                          <button className="secondary-button" disabled={loading} onClick={() => void regenerateCurrentDrafts()}>Regenerate current</button>
                        )}
                        <button className="primary-button compact" disabled={loading || documents.length !== 2} onClick={() => void approveBoth()}>Approve both current versions</button>
                      </div>
                    </div>
                    <p className="delivery-note">Cover-letter approval is separate. Email approval alone never sends; use the explicit Send Email action on the saved email draft.</p>
                  </>
                )}
              </section>

              <section className="panel activity-panel">
                <div className="section-heading">
                  <div><p className="eyebrow">LIVE ACTIVITY</p><h2>Run progress</h2></div>
                  <span className="live-indicator">Persisted</span>
                </div>
                {events.length === 0 ? <p className="muted">No progress events recorded yet.</p> : (
                  <ol className="event-list">
                    {events.slice(-8).reverse().map((event) => (
                      <li key={event.id}>
                        <span className="event-dot" />
                        <div><strong>{event.detail || humanize(event.event_type)}</strong><small>{humanize(event.phase || "run")} · {new Date(event.created_at).toLocaleTimeString()}</small></div>
                        {event.progress !== null && <span className="event-progress">{event.progress}%</span>}
                      </li>
                    ))}
                  </ol>
                )}
              </section>
            </>
          )}
        </div>
      </div>
      {sendConfirmation && (
        <div className="confirmation-backdrop">
          <section className="send-confirmation panel" role="dialog" aria-modal="true" aria-labelledby="send-confirmation-title">
            <p className="eyebrow">FINAL DELIVERY REVIEW</p>
            <h2 id="send-confirmation-title">Confirm this email</h2>
            <p><strong>To:</strong> {sendConfirmation.recipient}</p>
            <p><strong>Subject:</strong> {sendConfirmation.document.subject}</p>
            <pre>{sendConfirmation.document.content}</pre>
            {sendConfirmation.attachments.length > 0 && (
              <div className="attachment-list">
                <strong>Attachments:</strong>
                <ul>
                  {sendConfirmation.attachments.map((name) => (
                    <li key={name}>📎 {name}</li>
                  ))}
                </ul>
              </div>
            )}
            {sendConfirmation.connectedEmail && (
              <p className="muted"><strong>Sending via:</strong> {sendConfirmation.connectedEmail}</p>
            )}
            <p className="muted">
              {gmailConnection?.test_mode
                ? "Test mode is active. Confirming records a simulation; Gmail will not be contacted."
                : "This sends one email through the connected Gmail account. Delivery cannot be automatically undone."}
            </p>
            <div className="button-row">
              <button className="secondary-button" disabled={loading} onClick={() => setSendConfirmation(null)}>Cancel</button>
              <button className="primary-button compact" disabled={loading} onClick={() => void confirmEmailSend()}>
                {loading ? "Processing…" : gmailConnection?.test_mode ? "Confirm simulation" : "Confirm and send"}
              </button>
            </div>
          </section>
        </div>
      )}
      <footer className="footer-note">Research is resumable · Draft approvals are versioned · Each send is explicitly confirmed</footer>
    </main>
  );
}

function DraftEditor({
  document,
  disabled,
  recipient,
  gmailConnected,
  deliveryStatus,
  deliveryQueuedAt,
  canSend,
  onSave,
  onDecision,
  onSend,
  onResolveUnknown,
}: {
  document: DraftDocument;
  disabled: boolean;
  recipient: string | null;
  gmailConnected: boolean;
  deliveryStatus: string | null;
  deliveryQueuedAt: string | null;
  canSend: boolean;
  onSave: (content: string, subject: string) => void;
  onDecision: (decision: "approved" | "rejected") => void;
  onSend: () => void;
  onResolveUnknown: () => void;
}) {
  const [content, setContent] = useState(document.content);
  const [subject, setSubject] = useState(document.subject ?? "");
  const changed = content !== document.content || subject !== (document.subject ?? "");
  return (
    <article className="document-card">
      <div className="document-header">
        <div><p className="eyebrow">{document.artifact_type === "email" ? "OUTREACH" : "ATTACHMENT"}</p><h3>{title(document.artifact_type)}</h3></div>
        <div className="version-state">
          <span>v{document.version}</span>
          <span className={`approval-state ${document.approval || "pending"}`}>{humanize(document.approval || "pending review")}</span>
        </div>
      </div>
      {document.artifact_type === "email" && (
        <label className="subject-field">Subject<input value={subject} onChange={(event) => setSubject(event.target.value)} /></label>
      )}
      <label className="editor-label">
        <span className="sr-only">{title(document.artifact_type)} content</span>
        <textarea value={content} onChange={(event) => setContent(event.target.value)} rows={document.artifact_type === "email" ? 12 : 16} />
      </label>
      <div className="document-actions">
        <button className="secondary-button" disabled={disabled || !changed} onClick={() => onSave(content, subject)}>Save new version</button>
        <div className="button-row">
          <button
            className={`text-button ${document.approval === "rejected" ? "active-decision" : ""}`}
            disabled={disabled || document.approval === "rejected"}
            onClick={() => onDecision("rejected")}
          >
            {document.approval === "rejected" ? "Rejected" : "Reject"}
          </button>
          <button
            className={`approve-button ${document.approval === "approved" ? "active-decision" : ""}`}
            disabled={disabled || document.approval === "approved"}
            onClick={() => onDecision("approved")}
          >
            {document.approval === "approved" ? "Approved" : "Approve"}
          </button>
        </div>
      </div>
      {document.artifact_type === "email" && (
        <div className="send-email-row">
          <span>{deliveryStatus ? deliveryStatusMessage(deliveryStatus, document.delivery_version) : `Recipient: ${recipient || "not available"}`}</span>
          <div className="button-row">
            {(deliveryStatus === "queued" || deliveryStatus === "sending")
              && isInterruptedDelivery(deliveryQueuedAt) && (
                <button className="secondary-button" disabled={disabled} onClick={onResolveUnknown}>
                  Check Sent and mark unknown
                </button>
              )}
            {!deliveryStatus && (
              <button
                className="primary-button compact"
                disabled={disabled || changed || document.approval !== "approved" || !gmailConnected || !isValidEmail(recipient) || !canSend}
                onClick={onSend}
              >
                Send Email
              </button>
            )}
          </div>
        </div>
      )}
      {changed && <p className="edit-warning">Unsaved edit · approval will be cleared after saving.</p>}
    </article>
  );
}

function Metric({ label, value }: { label: string; value: string | number }) {
  return <div className="metric"><span>{label}</span><strong>{value}</strong></div>;
}

function title(value: string) {
  return value === "cover_letter" ? "Cover letter" : "Email";
}

function humanize(value: string) {
  return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function isValidEmail(value: string | null | undefined) {
  return typeof value === "string" && /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(value.trim());
}

function isInterruptedDelivery(queuedAt: string | null) {
  return Boolean(queuedAt && Date.now() - new Date(queuedAt).getTime() >= 5 * 60 * 1000);
}

function deliveryStatusMessage(status: string, version: number | null) {
  const versionLabel = version ? ` for email v${version}` : "";
  switch (status) {
    case "sent":
      return `Sent${versionLabel}.`;
    case "simulated":
      return `Test simulation recorded${versionLabel}; no email was sent.`;
    case "unknown":
      return `Delivery is unknown${versionLabel}. Check Gmail Sent before taking any further action; automatic retry is disabled.`;
    case "sending":
      return `Sending${versionLabel}. If interrupted, verify Gmail Sent before taking any further action.`;
    case "queued":
      return `Delivery reserved${versionLabel}. If this remains queued for five minutes, check Gmail Sent before marking it unknown.`;
    case "failed":
      return `Delivery failed${versionLabel}. Automatic retry is disabled.`;
    default:
      return `${humanize(status)}${versionLabel}.`;
  }
}

function deliveryNotice(result: DeliveryResult) {
  switch (result.status) {
    case "sent":
      return `Gmail accepted the email (message ID ${result.provider_message_id ?? "not returned"}).`;
    case "simulated":
      return "Test simulation recorded. No email was sent.";
    case "unknown":
      return "Gmail delivery is ambiguous. Check the connected account's Sent folder before taking any further action; this draft will not be retried automatically.";
    case "failed":
      return `Gmail did not confirm delivery (${result.safe_error_code ?? "delivery failed"}). This item cannot be sent again automatically.`;
    default:
      return `Delivery status: ${humanize(result.status)}.`;
  }
}

function researchStatusMessage(status: string) {
  switch (status) {
    case "queued":
      return "Queued; waiting for the worker to claim this item.";
    case "researching":
      return "Research is in progress.";
    case "research_review":
      return "Research is ready and awaiting your approval.";
    case "drafting":
      return "Research is approved; cover letter and email drafts are being generated.";
    case "draft_review":
      return "Both drafts are ready for review.";
    case "rejected":
      return "Research was rejected; no drafts were generated.";
    case "ineligible":
      return "Research did not find a matching opportunity or usable contact.";
    case "completed":
      return "Review is complete.";
    case "failed":
      return "This item failed. Review the error code and retry explicitly when ready.";
    default:
      return "No research summary has been persisted yet.";
  }
}

function researchFailureMessage(code: string, attempts: number, budget: number) {
  if (code === "search_exhausted") {
    return `Search exhausted after ${attempts} of ${budget} opportunities; no suitable role was found.`;
  }
  if (code === "no_matching_role") {
    return "No suitable role was found at the specified company.";
  }
  if (code === "no_usable_contact") {
    return "No suitable contact was found for this opportunity.";
  }
  return humanize(code);
}

function errorMessage(value: unknown) {
  return value instanceof Error ? value.message : "The request could not be completed.";
}

function isNotFoundOrEmpty(value: unknown) {
  return value instanceof Error && value.message === "Batch item not found.";
}

export default App;
