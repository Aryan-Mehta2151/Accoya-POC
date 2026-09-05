import type {
  ChatMessage,
  ChatResponse,
  CsvUploadResult,
  EarlyBidSyncStatus,
  Email,
  EmailDeliveryJob,
  EmailGenerationJob,
  EmailReplySummary,
  EmailStatus,
  Lead,
  LeadWorkspace,
  StrategyDocument,
  SyncResult,
} from '../types';

const DEFAULT_API_BASE = import.meta.env.DEV
  ? typeof window === 'undefined'
    ? 'http://localhost:8000/api'
    : `${window.location.protocol}//${window.location.hostname}:8000/api`
  : '/api';

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? DEFAULT_API_BASE).replace(
  /\/$/,
  '',
);

export interface AuthUser {
  id: string;
  email: string;
  name: string | null;
  session_expires_at: string;
}

interface CsrfResponse {
  csrf_token: string;
}

interface LoginResponse extends CsrfResponse {
  user: AuthUser;
}

type ErrorPayload = {
  code?: unknown;
  message?: unknown;
  warnings?: unknown;
  detail?: unknown;
};

type UnauthorizedListener = () => void;

const unsafeMethods = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);
const unauthorizedListeners = new Set<UnauthorizedListener>();
let csrfToken: string | null = null;
let csrfRequest: Promise<string> | null = null;
let csrfRequestEpoch: number | null = null;
let authStateEpoch = 0;
let sessionGeneration = 0;

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

function apiErrorFromResponse(response: Response, body: unknown): ApiError {
  const payload = body && typeof body === 'object' ? (body as ErrorPayload) : {};
  const nestedDetail =
    payload.detail && typeof payload.detail === 'object'
      ? (payload.detail as ErrorPayload)
      : {};
  const message =
    (typeof payload.message === 'string' && payload.message) ||
    (typeof nestedDetail.message === 'string' && nestedDetail.message) ||
    readableDetail(payload.detail) ||
    readableDetail(body) ||
    `The request failed with status ${response.status}.`;
  const warnings = Array.isArray(payload.warnings)
    ? payload.warnings.map(String)
    : [];

  return new ApiError({
    status: response.status,
    message,
    code:
      (typeof payload.code === 'string' && payload.code) ||
      (typeof nestedDetail.code === 'string' && nestedDetail.code) ||
      undefined,
    warnings,
    detail: payload.detail ?? body,
  });
}

async function fetchApi(path: string, init?: RequestInit): Promise<Response> {
  try {
    return await fetch(`${API_BASE}${path}`, {
      ...init,
      cache: 'no-store',
      credentials: 'include',
    });
  } catch (error) {
    throw new ApiError({
      status: 0,
      message: 'The service could not be reached. Check that the backend is running and try again.',
      detail: error,
    });
  }
}

function authStateChangedError(): ApiError {
  return new ApiError({
    status: 0,
    code: 'auth_state_changed',
    message: 'The sign-in state changed while the request was in progress. Try again.',
  });
}

async function loadCsrfToken(force = false): Promise<string> {
  if (force) {
    authStateEpoch += 1;
    csrfToken = null;
  }

  // Never overlap CSRF fetches. An older response can carry a Set-Cookie
  // header, so merely ignoring its JSON token would still desynchronize the
  // HttpOnly seed from the in-memory header token.
  if (csrfRequest) {
    const pending = csrfRequest;
    if (!force && csrfRequestEpoch === authStateEpoch) return pending;
    try {
      await pending;
    } catch {
      // A superseded request is expected to fail its epoch check. Its response
      // must still settle before a replacement request may start.
    }
    return loadCsrfToken(false);
  }
  if (!force && csrfToken) return csrfToken;

  const requestEpoch = authStateEpoch;
  const pending = (async () => {
    const response = await fetchApi('/auth/csrf');
    const body = await parseBody(response);
    if (requestEpoch !== authStateEpoch) throw authStateChangedError();
    if (!response.ok) throw apiErrorFromResponse(response, body);
    const token =
      body && typeof body === 'object' && 'csrf_token' in body
        ? (body as { csrf_token: unknown }).csrf_token
        : null;
    if (typeof token !== 'string' || !token) {
      throw new ApiError({
        status: 500,
        message: 'The service returned an invalid security token.',
        detail: body,
      });
    }
    csrfToken = token;
    return token;
  })();
  csrfRequest = pending;
  csrfRequestEpoch = requestEpoch;

  try {
    return await pending;
  } finally {
    if (csrfRequest === pending) {
      csrfRequest = null;
      csrfRequestEpoch = null;
    }
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const sessionBound = !path.startsWith('/auth/');
  const requestGeneration = sessionGeneration;
  const method = (init?.method ?? 'GET').toUpperCase();
  const headers = new Headers(init?.headers);
  if (unsafeMethods.has(method)) {
    headers.set('X-CSRF-Token', await loadCsrfToken());
  }
  if (sessionBound && requestGeneration !== sessionGeneration) {
    throw authStateChangedError();
  }

  const response = await fetchApi(path, { ...init, method, headers });
  const body = await parseBody(response);
  if (sessionBound && requestGeneration !== sessionGeneration) {
    throw authStateChangedError();
  }
  if (!response.ok) {
    const error = apiErrorFromResponse(response, body);
    if (response.status === 401 && !path.startsWith('/auth/')) {
      unauthorizedListeners.forEach((listener) => listener());
    }
    if (response.status === 403 && error.code === 'csrf_failed') {
      // Do not replay the rejected mutation. Forget only the bad CSRF token so
      // the user's next explicit action can obtain current cookie-bound data.
      authStateEpoch += 1;
      csrfToken = null;
    }
    throw error;
  }

  return body as T;
}

export function subscribeToUnauthorized(listener: UnauthorizedListener): () => void {
  unauthorizedListeners.add(listener);
  return () => unauthorizedListeners.delete(listener);
}

export function clearApiAuthState(): void {
  sessionGeneration += 1;
  authStateEpoch += 1;
  csrfToken = null;
}

export const authApi = {
  prepareCsrf: (force = false) => loadCsrfToken(force),
  getCurrentUser: () => request<AuthUser>('/auth/me'),
  login: async (email: string, password: string) => {
    const response = await request<LoginResponse>('/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
    });
    // Login rotates both cookies. Invalidate every CSRF fetch that began under
    // the previous anonymous cookie pair before publishing the new token.
    sessionGeneration += 1;
    authStateEpoch += 1;
    csrfToken = response.csrf_token;
    return response.user;
  },
  logout: async () => {
    try {
      await request<unknown>('/auth/logout', { method: 'POST' });
    } catch (error) {
      // An expired/revoked cookie already means logout succeeded from the
      // browser's perspective. Network and server failures remain retryable.
      if (!(error instanceof ApiError) || error.status !== 401) throw error;
    }
    clearApiAuthState();
  },
  forgotPassword: (email: string) =>
    request<{ message: string }>('/auth/forgot-password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email }),
    }),
  requestAccess: (email: string, name?: string) =>
    request<{ message: string }>('/auth/request-access', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, name: name?.trim() || undefined }),
    }),
  resetPassword: (token: string, password: string) =>
    request<{ message: string }>('/auth/reset-password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token, password }),
    }),
  googleStartUrl: () => `${API_BASE}/auth/google/start`,
};

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
  listLeads: (view: 'active' | 'dismissed' = 'active') =>
    request<Lead[]>(`/leads?view=${view}`),
  getLeadSyncStatus: () => request<EarlyBidSyncStatus>('/leads/sync-status'),
  syncLeads: () => request<SyncResult>('/leads/sync', { method: 'POST' }),
  uploadLeadsCsv: (file: File) => {
    const data = new FormData();
    data.append('file', file);
    return request<CsvUploadResult>('/leads/upload-csv', { method: 'POST', body: data });
  },
  getLeadWorkspace: (leadId: string) =>
    request<LeadWorkspace>(`/leads/${encodeURIComponent(leadId)}/workspace`),
  updateLeadContact: (
    leadId: string,
    payload: { contacts: string; contact_email: string },
  ) =>
    request<Lead>(`/leads/${encodeURIComponent(leadId)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  queueEmailGeneration: (leadId: string, idempotencyKey: string) =>
    request<EmailGenerationJob>(`/leads/${encodeURIComponent(leadId)}/email-generations`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ idempotency_key: idempotencyKey }),
    }),

  listEmails: () => request<Email[]>('/emails'),
  getEmailReplySummary: () => request<EmailReplySummary>('/email-replies/summary'),
  getEmail: (emailId: string) =>
    request<Email>(`/emails/${encodeURIComponent(emailId)}`),
  editEmail: (
    emailId: string,
    payload: {
      recipient_email?: string | null;
      subject?: string;
      body?: string;
    },
  ) =>
    request<Email>(`/emails/${encodeURIComponent(emailId)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  setEmailStatus: (
    emailId: string,
    status: EmailStatus,
    expectedContentHash?: string,
  ) =>
    request<Email>(`/emails/${encodeURIComponent(emailId)}/status`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        status,
        ...(expectedContentHash ? { expected_content_hash: expectedContentHash } : {}),
      }),
    }),
  sendEmail: (
    emailId: string,
    payload: {
      idempotency_key: string;
      expected_content_hash: string;
      acknowledge_duplicate_risk: boolean;
    },
  ) =>
    request<EmailDeliveryJob>(`/emails/${encodeURIComponent(emailId)}/send`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
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
