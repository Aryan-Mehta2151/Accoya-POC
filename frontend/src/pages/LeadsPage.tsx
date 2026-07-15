import { useEffect, useState } from "react";
import { api } from "../api";
import type { Email, Lead, StrategyDoc } from "../types";

export function LeadsPage() {
  const [leads, setLeads] = useState<Lead[]>([]);
  const [docs, setDocs] = useState<StrategyDoc[]>([]);
  const [generating, setGenerating] = useState<string | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [uploadingDoc, setUploadingDoc] = useState(false);
  const [deletingDoc, setDeletingDoc] = useState<string | null>(null);
  const [lastEmail, setLastEmail] = useState<Record<string, Email>>({});
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = () => api.listLeads().then(setLeads).catch((e) => setError(String(e)));
  const loadDocs = () => api.listDocuments().then(setDocs).catch((e) => setError(String(e)));
  useEffect(() => {
    load();
    loadDocs();
  }, []);

  const onSync = async () => {
    setSyncing(true);
    setError(null);
    setNotice(null);
    try {
      const res = await api.syncLeads();
      setNotice(
        `Synced ${res.feed}: ${res.created} new, ${res.updated} updated (${res.total} total).`,
      );
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setSyncing(false);
    }
  };

  const onUpload = async (file: File) => {
    setError(null);
    try {
      await api.uploadLeadsCsv(file);
      await load();
    } catch (e) {
      setError(String(e));
    }
  };

  const onGenerate = async (leadId: string) => {
    setGenerating(leadId);
    setError(null);
    try {
      const email = await api.generateEmail(leadId);
      setLastEmail((prev) => ({ ...prev, [leadId]: email }));
    } catch (e) {
      setError(String(e));
    } finally {
      setGenerating(null);
    }
  };

  const onUploadDoc = async (file: File) => {
    setUploadingDoc(true);
    setError(null);
    setNotice(null);
    try {
      await api.uploadDocument(file);
      await loadDocs();
      setNotice("Strategy document uploaded to S3 bucket.");
    } catch (e) {
      setError(String(e));
    } finally {
      setUploadingDoc(false);
    }
  };

  const onDeleteDoc = async (docId: string) => {
    setDeletingDoc(docId);
    setError(null);
    setNotice(null);
    try {
      await api.deleteDocument(docId);
      await loadDocs();
      setNotice("Strategy document deleted from S3 bucket.");
    } catch (e) {
      setError(String(e));
    } finally {
      setDeletingDoc(null);
    }
  };

  return (
    <div>
      <section className="card docs-panel">
        <div className="card-head">
          <h3>Strategy Docs (S3 Bucket)</h3>
        </div>
        <div className="toolbar">
          <input
            type="file"
            disabled={uploadingDoc}
            onChange={(e) => e.target.files?.[0] && onUploadDoc(e.target.files[0])}
          />
          {uploadingDoc && <span className="muted small">Uploading to S3…</span>}
        </div>
        <ul className="list">
          {docs.map((doc) => (
            <li className="doc-item" key={doc.id}>
              <div>
                <strong>{doc.filename}</strong>
                <div className="muted small">{doc.s3_key}</div>
              </div>
              <div className="doc-actions">
                {doc.url && (
                  <a className="muted small" href={doc.url} target="_blank" rel="noreferrer">
                    Open
                  </a>
                )}
                <button
                  disabled={deletingDoc === doc.id}
                  onClick={() => onDeleteDoc(doc.id)}
                >
                  {deletingDoc === doc.id ? "Deleting…" : "Delete"}
                </button>
              </div>
            </li>
          ))}
          {docs.length === 0 && (
            <p className="muted">No docs in bucket yet. Upload one to get started.</p>
          )}
        </ul>
      </section>

      <div className="toolbar">
        <button onClick={onSync} disabled={syncing}>
          {syncing ? "Syncing…" : "Sync EarlyBid feed"}
        </button>
        <span className="muted small">or upload a feed CSV:</span>
        <input
          type="file"
          accept=".csv"
          onChange={(e) => e.target.files?.[0] && onUpload(e.target.files[0])}
        />
      </div>
      {notice && <p className="muted">{notice}</p>}
      {error && <p className="error">{error}</p>}
      <div className="cards">
        {leads.map((lead) => (
          <div className="card" key={lead.id}>
            <div className="card-head">
              <h3>{lead.project ?? "Untitled opportunity"}</h3>
              {lead.score != null && <span className="badge">Score {lead.score}</span>}
            </div>
            <p className="muted small">
              {[lead.location, lead.state].filter(Boolean).join(", ") || "—"}
            </p>
            <p className="muted small">
              {[lead.section, lead.signal, lead.intelligence].filter(Boolean).join(" · ")}
            </p>
            {lead.summary && <p className="summary">{lead.summary}</p>}
            <p className="muted small">
              Contact: {lead.contact_email ?? lead.contacts ?? "—"}
            </p>
            {lead.url && (
              <a className="muted small" href={lead.url} target="_blank" rel="noreferrer">
                Source document
              </a>
            )}
            <button
              disabled={generating === lead.id}
              onClick={() => onGenerate(lead.id)}
            >
              {generating === lead.id ? "Generating…" : "Generate outreach email"}
            </button>
            {lastEmail[lead.id] && (
              <div className="preview">
                <strong>{lastEmail[lead.id].subject}</strong>
                <pre>{lastEmail[lead.id].body}</pre>
              </div>
            )}
          </div>
        ))}
        {leads.length === 0 && (
          <p className="muted">No leads yet. Sync the EarlyBid feed or upload a CSV.</p>
        )}
      </div>
    </div>
  );
}
