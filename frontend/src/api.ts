import type {
  ChatResponse,
  Email,
  EmailStatus,
  Lead,
  StrategyDoc,
  SyncResult,
} from "./types";

const BASE = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000/api";

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status}: ${text}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  // Leads
  listLeads: () => fetch(`${BASE}/leads`).then(json<Lead[]>),
  syncLeads: () =>
    fetch(`${BASE}/leads/sync`, { method: "POST" }).then(json<SyncResult>),
  uploadLeadsCsv: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return fetch(`${BASE}/leads/upload-csv`, { method: "POST", body: fd }).then(
      json<Lead[]>,
    );
  },

  // Documents
  listDocuments: () => fetch(`${BASE}/documents`).then(json<StrategyDoc[]>),
  uploadDocument: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return fetch(`${BASE}/documents/upload`, { method: "POST", body: fd }).then(
      json<StrategyDoc>,
    );
  },
  deleteDocument: (docId: string) =>
    fetch(`${BASE}/documents/${encodeURIComponent(docId)}`, {
      method: "DELETE",
    }).then(json<{ deleted: boolean; s3_key: string }>),

  // Emails
  listEmails: () => fetch(`${BASE}/emails`).then(json<Email[]>),
  generateEmail: (leadId: string) =>
    fetch(`${BASE}/emails/generate/${leadId}`, { method: "POST" }).then(
      json<Email>,
    ),
  editEmail: (id: string, body: { subject?: string; body?: string }) =>
    fetch(`${BASE}/emails/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(json<Email>),
  setEmailStatus: (id: string, status: EmailStatus) =>
    fetch(`${BASE}/emails/${id}/status`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status }),
    }).then(json<Email>),

  // Chat
  chat: (message: string, sessionId: string | null) =>
    fetch(`${BASE}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, session_id: sessionId }),
    }).then(json<ChatResponse>),
};
