import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import {
  api,
  ApiError,
  authApi,
  clearApiAuthState,
  subscribeToUnauthorized,
} from './api';

const base = 'http://localhost:8000/api';
const server = setupServer();

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
beforeEach(() => {
  clearApiAuthState();
  server.use(
    http.get(`${base}/auth/csrf`, () => HttpResponse.json({ csrf_token: 'csrf-token' })),
  );
});
afterEach(() => {
  server.resetHandlers();
  vi.unstubAllGlobals();
});
afterAll(() => server.close());

describe('API client', () => {
  it('loads typed lead data from the configured API prefix', async () => {
    server.use(
      http.get(`${base}/leads`, () => HttpResponse.json([
        {
          id: 'lead-1',
          external_id: 'source-1',
          section: null,
          project: 'Riverside Pavilion',
          location: 'Portland',
          state: 'OR',
          signal: null,
          intelligence: null,
          score: 92,
          timing: null,
          awarded_to: null,
          priority_reasons: null,
          summary: null,
          contacts: null,
          contact_email: null,
          meeting_date: null,
          tags: null,
          url: null,
          source_feed: null,
          created_at: '2026-07-01T00:00:00Z',
        },
      ])),
    );

    const leads = await api.listLeads();
    expect(leads).toHaveLength(1);
    expect(leads[0]).toMatchObject({ project: 'Riverside Pavilion', score: 92 });
  });

  it('loads the durable daily EarlyBid synchronization status', async () => {
    server.use(
      http.get(`${base}/leads/sync-status`, () => HttpResponse.json({
        timezone: 'America/Los_Angeles',
        next_scheduled_at: '2026-07-26T07:00:00Z',
        overdue: false,
        latest_run: {
          id: 'sync-run-1',
          feed: 'reseller/client',
          schedule_date: '2026-07-25',
          scheduled_for: '2026-07-25T07:00:00Z',
          status: 'retry_wait',
          attempt_count: 1,
          error_code: 'upstream_unavailable',
          next_attempt_at: '2026-07-25T07:05:00Z',
          created: 0,
          updated: 0,
          total: 0,
          generation_queued: 0,
          claimed_at: '2026-07-25T07:00:01Z',
          completed_at: null,
        },
      })),
    );

    await expect(api.getLeadSyncStatus()).resolves.toMatchObject({
      timezone: 'America/Los_Angeles',
      latest_run: {
        status: 'retry_wait',
        next_attempt_at: '2026-07-25T07:05:00Z',
      },
    });
  });

  it('preserves friendly generation queue errors and warnings', async () => {
    server.use(
      http.post(`${base}/leads/lead-1/email-generations`, () => HttpResponse.json(
        {
          code: 'insufficient_context',
          message: 'The lead needs more context.',
          warnings: ['Add a recipient or project summary.'],
        },
        { status: 422 },
      )),
    );

    const error = await api.queueEmailGeneration(
      'lead-1',
      '00000000-0000-4000-8000-000000000001',
    ).catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 422,
      code: 'insufficient_context',
      message: 'The lead needs more context.',
      warnings: ['Add a recipient or project summary.'],
    });
  });

  it('sends cookies, CSRF, and the unchanged idempotent delivery payload', async () => {
    let capturedAuthorization: string | null = null;
    let capturedCsrf: string | null = null;
    let capturedCredentials: RequestCredentials | null = null;
    let capturedBody: unknown;
    server.use(
      http.post(`${base}/emails/email-1/send`, async ({ request }) => {
        capturedAuthorization = request.headers.get('Authorization');
        capturedCsrf = request.headers.get('X-CSRF-Token');
        capturedCredentials = request.credentials;
        capturedBody = await request.json();
        return HttpResponse.json({
          id: 'delivery-1',
          email_id: 'email-1',
          retry_of_job_id: null,
          status: 'queued',
          requested_by: 'user-1',
          idempotency_key: '00000000-0000-4000-8000-000000000002',
          content_hash: 'b'.repeat(64),
          message_id: '<delivery-1@example.com>',
          sender_email: 'outreach@example.com',
          recipient_email: 'alex@example.com',
          subject: 'Accoya proposal',
          body_snapshot: 'Hello Alex',
          error_code: null,
          attempt_count: 0,
          queued_at: '2026-07-02T00:00:00Z',
          claimed_at: null,
          heartbeat_at: null,
          send_started_at: null,
          accepted_at: null,
          completed_at: null,
        }, { status: 202 });
      }),
    );

    await api.sendEmail('email-1', {
      idempotency_key: '00000000-0000-4000-8000-000000000002',
      expected_content_hash: 'b'.repeat(64),
      acknowledge_duplicate_risk: false,
    });

    expect(capturedAuthorization).toBeNull();
    expect(capturedCsrf).toBe('csrf-token');
    expect(capturedCredentials).toBe('include');
    expect(capturedBody).toEqual({
      idempotency_key: '00000000-0000-4000-8000-000000000002',
      expected_content_hash: 'b'.repeat(64),
      acknowledge_duplicate_risk: false,
    });
  });

  it('does not send CSRF on reads and notifies listeners on business-route 401s', async () => {
    let capturedCsrf: string | null = null;
    let capturedCredentials: RequestCredentials | null = null;
    const onUnauthorized = vi.fn();
    const unsubscribe = subscribeToUnauthorized(onUnauthorized);
    server.use(
      http.get(`${base}/leads`, ({ request }) => {
        capturedCsrf = request.headers.get('X-CSRF-Token');
        capturedCredentials = request.credentials;
        return HttpResponse.json(
          { detail: { code: 'authentication_required', message: 'Sign in is required.' } },
          { status: 401 },
        );
      }),
    );

    await expect(api.listLeads()).rejects.toMatchObject({
      status: 401,
      code: 'authentication_required',
    });
    expect(capturedCsrf).toBeNull();
    expect(capturedCredentials).toBe('include');
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    unsubscribe();
  });

  it('discards a business success that completes after the session changes', async () => {
    let releaseResponse!: () => void;
    let markStarted!: () => void;
    const started = new Promise<void>((resolve) => {
      markStarted = resolve;
    });
    const responseGate = new Promise<void>((resolve) => {
      releaseResponse = resolve;
    });
    server.use(
      http.get(`${base}/leads`, async () => {
        markStarted();
        await responseGate;
        return HttpResponse.json([]);
      }),
    );

    const staleRequest = api.listLeads();
    await started;
    clearApiAuthState();
    releaseResponse();

    await expect(staleRequest).rejects.toMatchObject({
      code: 'auth_state_changed',
    });
  });

  it('does not let a stale business 401 clear a newer session', async () => {
    let releaseResponse!: () => void;
    let markStarted!: () => void;
    const started = new Promise<void>((resolve) => {
      markStarted = resolve;
    });
    const responseGate = new Promise<void>((resolve) => {
      releaseResponse = resolve;
    });
    const onUnauthorized = vi.fn();
    const unsubscribe = subscribeToUnauthorized(onUnauthorized);
    server.use(
      http.get(`${base}/leads`, async () => {
        markStarted();
        await responseGate;
        return HttpResponse.json(
          { detail: { code: 'authentication_required', message: 'Sign in is required.' } },
          { status: 401 },
        );
      }),
    );

    const staleRequest = api.listLeads();
    await started;
    clearApiAuthState();
    releaseResponse();

    await expect(staleRequest).rejects.toMatchObject({
      code: 'auth_state_changed',
    });
    expect(onUnauthorized).not.toHaveBeenCalled();
    unsubscribe();
  });

  it('does not replay a CSRF failure but refreshes for the next explicit action', async () => {
    let attempts = 0;
    let csrfLoads = 0;
    const csrfHeaders: Array<string | null> = [];
    server.use(
      http.get(`${base}/auth/csrf`, () => {
        csrfLoads += 1;
        return HttpResponse.json({
          csrf_token: csrfLoads === 1 ? 'csrf-rejected' : 'csrf-refreshed',
        });
      }),
      http.post(`${base}/leads/sync`, ({ request }) => {
        attempts += 1;
        csrfHeaders.push(request.headers.get('X-CSRF-Token'));
        if (attempts === 1) {
          return HttpResponse.json(
            { detail: { code: 'csrf_failed', message: 'Security validation failed.' } },
            { status: 403 },
          );
        }
        return HttpResponse.json({ created: 0, updated: 0, total: 0 });
      }),
    );

    await expect(api.syncLeads()).rejects.toMatchObject({ status: 403, code: 'csrf_failed' });
    expect(attempts).toBe(1);
    expect(csrfLoads).toBe(1);

    await expect(api.syncLeads()).resolves.toMatchObject({ total: 0 });
    expect(attempts).toBe(2);
    expect(csrfLoads).toBe(2);
    expect(csrfHeaders).toEqual(['csrf-rejected', 'csrf-refreshed']);
  });

  it('uses the CSRF token rotated by a successful cookie login', async () => {
    let loginCsrf: string | null = null;
    let mutationCsrf: string | null = null;
    server.use(
      http.post(`${base}/auth/login`, ({ request }) => {
        loginCsrf = request.headers.get('X-CSRF-Token');
        return HttpResponse.json({
          user: {
            id: 'user-1',
            email: 'approved@example.com',
            name: null,
            session_expires_at: '2099-01-01T00:00:00Z',
          },
          csrf_token: 'csrf-after-login',
        });
      }),
      http.post(`${base}/leads/sync`, ({ request }) => {
        mutationCsrf = request.headers.get('X-CSRF-Token');
        return HttpResponse.json({ created: 0, updated: 0, total: 0 });
      }),
    );

    await authApi.login('approved@example.com', 'correct horse battery staple');
    await api.syncLeads();

    expect(loginCsrf).toBe('csrf-token');
    expect(mutationCsrf).toBe('csrf-after-login');
  });

  it('cannot restore a CSRF token from a request invalidated by logout', async () => {
    let csrfCalls = 0;
    let releaseFirst!: () => void;
    let markFirstStarted!: () => void;
    const firstStarted = new Promise<void>((resolve) => {
      markFirstStarted = resolve;
    });
    const firstGate = new Promise<void>((resolve) => {
      releaseFirst = resolve;
    });
    let mutationCsrf: string | null = null;
    server.use(
      http.get(`${base}/auth/csrf`, async () => {
        csrfCalls += 1;
        if (csrfCalls === 1) {
          markFirstStarted();
          await firstGate;
          return HttpResponse.json({ csrf_token: 'csrf-stale' });
        }
        return HttpResponse.json({ csrf_token: 'csrf-fresh' });
      }),
      http.post(`${base}/leads/sync`, ({ request }) => {
        mutationCsrf = request.headers.get('X-CSRF-Token');
        return HttpResponse.json({ created: 0, updated: 0, total: 0 });
      }),
    );

    const staleLoad = authApi.prepareCsrf(true);
    await firstStarted;
    clearApiAuthState();
    releaseFirst();

    await expect(staleLoad).rejects.toMatchObject({ code: 'auth_state_changed' });
    await api.syncLeads();
    expect(csrfCalls).toBe(2);
    expect(mutationCsrf).toBe('csrf-fresh');
  });

  it('turns FastAPI validation arrays into readable messages', async () => {
    server.use(
      http.post(`${base}/leads/upload-csv`, () => HttpResponse.json(
        { detail: [{ msg: 'Project is required' }, { msg: 'Location is required' }] },
        { status: 422 },
      )),
    );

    const file = new File(['Project,Location'], 'leads.csv', { type: 'text/csv' });
    await expect(api.uploadLeadsCsv(file)).rejects.toMatchObject({
      status: 422,
      message: 'Project is required Location is required',
    });
  });
});
