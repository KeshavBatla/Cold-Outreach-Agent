import { useCallback, useEffect, useMemo, useState } from "react";
import type { FormEvent } from "react";
import {
  api,
  type Batch,
  type BatchList,
  type DraftDocument,
  type ResearchCard,
  type RunEvent,
} from "./api";

function App() {
  const [batches, setBatches] = useState<Batch[]>([]);
  const [selectedBatchId, setSelectedBatchId] = useState(
    new URLSearchParams(window.location.search).get("batch") ?? "",
  );
  const [batch, setBatch] = useState<Batch | null>(null);
  const [research, setResearch] = useState<ResearchCard[]>([]);
  const [documents, setDocuments] = useState<DraftDocument[]>([]);
  const [selectedItemId, setSelectedItemId] = useState("");
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [eventCursor, setEventCursor] = useState(0);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(false);
  const [companyName, setCompanyName] = useState("");
  const [role, setRole] = useState("");
  const [jobDescription, setJobDescription] = useState("");
  const [batchLabel, setBatchLabel] = useState("");
  const [candidateName, setCandidateName] = useState("");
  const [cvFile, setCvFile] = useState<File | null>(null);
  const [exampleFile, setExampleFile] = useState<File | null>(null);

  const selectedResearch = useMemo(
    () => research.find((item) => item.item_id === selectedItemId) ?? research[0] ?? null,
    [research, selectedItemId],
  );

  const loadBatches = useCallback(async () => {
    const result = await api<BatchList>("/api/v1/batches?limit=50");
    setBatches(result.items);
    if (!selectedBatchId && result.items.length) {
      setSelectedBatchId(result.items[0].id);
    }
  }, [selectedBatchId]);

  const loadBatch = useCallback(async (batchId: string, cursor: number) => {
    const [summary, cards, newEvents] = await Promise.all([
      api<Batch>(`/api/v1/batches/${batchId}`),
      api<ResearchCard[]>(`/api/v1/batches/${batchId}/research-results`),
      api<RunEvent[]>(`/api/v1/batches/${batchId}/events?after=${cursor}`),
    ]);
    setBatch(summary);
    setResearch(cards);
    setSelectedItemId((current) =>
      cards.some((item) => item.item_id === current) ? current : cards[0]?.item_id ?? "",
    );
    if (newEvents.length) {
      setEvents((current) => [...current, ...newEvents]);
      setEventCursor(newEvents[newEvents.length - 1].sequence);
    }
  }, []);

  useEffect(() => {
    setEvents([]);
    setEventCursor(0);
  }, [selectedBatchId]);

  const loadDrafts = useCallback(async (batchId: string, itemId: string) => {
    const result = await api<{ item_id: string; documents: DraftDocument[] }>(
      `/api/v1/batches/${batchId}/items/${itemId}/drafts`,
    );
    setDocuments(result.documents);
  }, []);

  useEffect(() => {
    let active = true;
    api<BatchList>("/api/v1/batches?limit=50")
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
  }, []);

  useEffect(() => {
    if (!selectedBatchId) {
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
        await loadBatch(selectedBatchId, eventCursor);
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
  }, [selectedBatchId, selectedItemId, eventCursor, loadBatch, loadBatches, loadDrafts]);

  useEffect(() => {
    if (!selectedBatchId || !selectedItemId) {
      setDocuments([]);
      return;
    }
    void loadDrafts(selectedBatchId, selectedItemId).catch((cause: unknown) => {
      setDocuments([]);
      if (!isNotFoundOrEmpty(cause)) setError(errorMessage(cause));
    });
  }, [selectedBatchId, selectedItemId, loadDrafts]);

  async function createBatch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!cvFile) {
      setError("Upload a CV before creating a batch.");
      return;
    }
    const targetCompany = companyName.trim();
    if (!targetCompany) {
      setError("Enter a company name before creating a batch.");
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
          label: batchLabel || `${companyName || "Research"} · ${new Date().toLocaleDateString()}`,
          candidate_name: candidateName || undefined,
          cv_artifact_id: cv.id,
          example_letter_artifact_id: exampleLetterId,
          items: [
            {
              company_name: targetCompany,
              role: role.trim() || undefined,
              job_description: jobDescription.trim() || undefined,
            },
          ],
        }),
      });
      setSelectedBatchId(created.id);
      setCompanyName("");
      setRole("");
      setJobDescription("");
      setBatchLabel("");
      setCandidateName("");
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
      setNotice(`${title(document.artifact_type)} ${decision}. This does not authorize email delivery.`);
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
        </div>
      </header>

      <section className="hero">
        <div>
          <p className="eyebrow">WORKSPACE</p>
          <h1>Thoughtful outreach, under your control.</h1>
          <p className="hero-copy">
            Run research in batches, review every result, then refine and approve each draft.
            Nothing is sent from this workspace.
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
                Batch name <span className="optional">optional</span>
                <input value={batchLabel} onChange={(event) => setBatchLabel(event.target.value)} placeholder="e.g. Product roles — October" />
              </label>
              <label>
                Company <span className="required">required</span>
                <input required value={companyName} onChange={(event) => setCompanyName(event.target.value)} placeholder="Company name" />
              </label>
              <label>
                Role
                <input value={role} onChange={(event) => setRole(event.target.value)} placeholder="Target role" />
              </label>
              <label>
                Job description <span className="optional">optional</span>
                <textarea rows={3} value={jobDescription} onChange={(event) => setJobDescription(event.target.value)} placeholder="Paste role context if available" />
              </label>
              <label>
                CV <span className="required">required</span>
                <input type="file" accept=".pdf,.docx,.txt" onChange={(event) => setCvFile(event.target.files?.[0] ?? null)} />
              </label>
              <label>
                Example cover letter <span className="optional">optional</span>
                <input type="file" accept=".pdf,.docx,.txt" onChange={(event) => setExampleFile(event.target.files?.[0] ?? null)} />
              </label>
              <label>
                Candidate name <span className="optional">optional</span>
                <input value={candidateName} onChange={(event) => setCandidateName(event.target.value)} placeholder="Name for personalized drafts" />
              </label>
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
              <span className="count-pill">{batches.length}</span>
            </div>
            {batches.length === 0 ? (
              <p className="muted empty-history">New batches will appear here.</p>
            ) : (
              <div className="batch-list">
                {batches.map((entry) => (
                  <button
                    className={`batch-row ${selectedBatchId === entry.id ? "selected" : ""}`}
                    key={entry.id}
                    onClick={() => setSelectedBatchId(entry.id)}
                  >
                    <span className="batch-icon">{entry.item_count === 1 ? "↗" : "▦"}</span>
                    <span className="batch-row-copy">
                      <strong>{entry.label || "Outreach batch"}</strong>
                      <small>{entry.item_count} item{entry.item_count === 1 ? "" : "s"} · {humanize(entry.status)}</small>
                    </span>
                    <span className={`mini-status ${entry.status}`} />
                  </button>
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
                    <h2>{batch.label || "Outreach batch"}</h2>
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
                            <h3>{card.company_name || "Company research"}</h3>
                            <span>{card.company_domain || "Domain pending"}</span>
                          </div>
                          <span className={`badge small ${card.status}`}>{humanize(card.status)}</span>
                        </div>
                        <div className="role-row">
                          <span className="role-icon">⌘</span>
                          <div><small>ROLE</small><strong>{card.role || "Role discovery in progress"}</strong></div>
                          <span className={`fit-pill ${card.role_fit || "pending"}`}>{humanize(card.role_fit || "pending")}</span>
                        </div>
                        <div className="contact-block">
                          <small>CONTACT</small>
                          <strong>{card.contact_name || "Contact not available yet"}</strong>
                          {card.contact_email && <span>{card.contact_email}</span>}
                        </div>
                        <p className="summary-text">{card.summary || card.failure_code || "Waiting for persisted research results."}</p>
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
                            onSave={(content, subject) => void saveDocument(document, content, subject)}
                            onDecision={(decision) => void decideDraft(document, decision)}
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
                    <div className="delivery-placeholder">
                      <span className="lock-icon">⌑</span>
                      <div><strong>Email delivery is not available yet</strong><p>Draft approval records review only. There is no Send One or Send All action in this milestone.</p></div>
                    </div>
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
      <footer className="footer-note">Research is resumable · Decisions are versioned · Sending is disabled</footer>
    </main>
  );
}

function DraftEditor({
  document,
  disabled,
  onSave,
  onDecision,
}: {
  document: DraftDocument;
  disabled: boolean;
  onSave: (content: string, subject: string) => void;
  onDecision: (decision: "approved" | "rejected") => void;
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
          <button className="text-button" disabled={disabled} onClick={() => onDecision("rejected")}>Reject</button>
          <button className="approve-button" disabled={disabled} onClick={() => onDecision("approved")}>Approve</button>
        </div>
      </div>
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

function errorMessage(value: unknown) {
  return value instanceof Error ? value.message : "The request could not be completed.";
}

function isNotFoundOrEmpty(value: unknown) {
  return value instanceof Error && value.message === "Batch item not found.";
}

export default App;
