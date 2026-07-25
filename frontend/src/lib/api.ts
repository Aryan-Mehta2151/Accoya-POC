import type {
  ChatMessage,
  ChatResponse,
  CsvUploadResult,
  EarlyBidSyncStatus,
  Email,
  EmailGenerationJob,
  EmailStatus,
  Lead,
  LeadWorkspace,
  StrategyDocument,
  SyncResult,
} from '../types';

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000/api').replace(
  /\/$/,
  '',
);

type ErrorPayload = {
  code?: unknown;
  message?: unknown;
  warnings?: unknown;
  detail?: unknown;
};

export class ApiError extends Error {
  readonly status: number;
  readonly code?: string;
  readonly warnings: string[];
  readonly detail: unknown;

  constructor(options: {
    status: number;
    message: string;
    code?: string;
    warnings?: string[];
    detail?: unknown;
  }) {
    super(options.message);
    this.name = 'ApiError';
    this.status = options.status;
    this.code = options.code;
    this.warnings = options.warnings ?? [];
    this.detail = options.detail;
  }
}

function readableDetail(detail: unknown): string | null {
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => {
        if (typeof item === 'string') return item;
        if (item && typeof item === 'object' && 'msg' in item) {
          return String((item as { msg: unknown }).msg);
        }
        return null;
      })
      .filter((item): item is string => Boolean(item));
    return messages.length > 0 ? messages.join(' ') : null;
  }
  if (detail && typeof detail === 'object') {
    if ('message' in detail && typeof (detail as { message: unknown }).message === 'string') {
      return (detail as { message: string }).message;
    }
    if ('issues' in detail && Array.isArray((detail as { issues: unknown }).issues)) {
      return `${(detail as { issues: unknown[] }).issues.length} feed row issue(s) need attention.`;
    }
  }
  return null;
}

async function parseBody(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, init);
  } catch (error) {
    throw new ApiError({
      status: 0,
      message: 'The service could not be reached. Check that the backend is running and try again.',
      detail: error,
    });
  }

  const body = await parseBody(response);
  if (!response.ok) {
    const payload = body && typeof body === 'object' ? (body as ErrorPayload) : {};
    const message =
      (typeof payload.message === 'string' && payload.message) ||
      readableDetail(payload.detail) ||
      readableDetail(body) ||
      `The request failed with status ${response.status}.`;
    const warnings = Array.isArray(payload.warnings)
      ? payload.warnings.map(String)
      : [];
    throw new ApiError({
      status: response.status,
      message,
      code: typeof payload.code === 'string' ? payload.code : undefined,
      warnings,
      detail: payload.detail ?? body,
    });
  }

  return body as T;
}

function toUiChatRole(role: unknown): ChatMessage['role'] {
  if (role === 'human' || role === 'user') return 'user';
  return 'assistant';
}

function normalizeChatMessage(raw: unknown): ChatMessage {
  const row = (raw ?? {}) as {
    role?: unknown;
    content?: unknown;
    created_at?: unknown;
  };
  return {
    role: toUiChatRole(row.role),
    content: typeof row.content === 'string' ? row.content : String(row.content ?? ''),
    created_at: typeof row.created_at === 'string' ? row.created_at : undefined,
  };
}

export const api = {
  listLeads: () => request<Lead[]>('/leads'),
  getLeadSyncStatus: () => request<EarlyBidSyncStatus>('/leads/sync-status'),
  syncLeads: () => request<SyncResult>('/leads/sync', { method: 'POST' }),
  uploadLeadsCsv: (file: File) => {
    const data = new FormData();
    data.append('file', file);
    return request<CsvUploadResult>('/leads/upload-csv', { method: 'POST', body: data });
  },
  getLeadWorkspace: (leadId: string) =>
    request<LeadWorkspace>(`/leads/${encodeURIComponent(leadId)}/workspace`),
  queueEmailGeneration: (leadId: string, idempotencyKey: string) =>
    request<EmailGenerationJob>(`/leads/${encodeURIComponent(leadId)}/email-generations`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ idempotency_key: idempotencyKey }),
    }),

  listEmails: () => request<Email[]>('/emails'),
  getEmail: (emailId: string) =>
    request<Email>(`/emails/${encodeURIComponent(emailId)}`),
  editEmail: (emailId: string, payload: { subject?: string; body?: string }) =>
    request<Email>(`/emails/${encodeURIComponent(emailId)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  setEmailStatus: (emailId: string, status: EmailStatus) =>
    request<Email>(`/emails/${encodeURIComponent(emailId)}/status`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status }),
    }),

  listDocuments: () => request<StrategyDocument[]>('/documents'),
  uploadDocument: (file: File) => {
    const data = new FormData();
    data.append('file', file);
    return request<StrategyDocument>('/documents/upload', { method: 'POST', body: data });
  },
  deleteDocument: (documentId: string) =>
    request<{ deleted: boolean; s3_key: string }>(
      `/documents/${encodeURIComponent(documentId)}`,
      { method: 'DELETE' },
    ),

  chat: (message: string, sessionId: string | null) =>
    request<ChatResponse>('/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message, session_id: sessionId }),
    }),
  getChatHistory: async (sessionId: string) => {
    const rows = await request<unknown[]>(`/chat/${encodeURIComponent(sessionId)}`);
    return rows.map(normalizeChatMessage);
  },
  listChatSessions: () => request<import('../types').ChatSession[]>('/chat/sessions'),
  createChatSession: () => request<{ session_id: string }>('/chat/session', { method: 'POST' }),
  deleteChat: (sessionId: string) =>
    request<{ deleted: boolean; session_id: string }>(
      `/chat/${encodeURIComponent(sessionId)}`,
      { method: 'DELETE' },
    ),
};
