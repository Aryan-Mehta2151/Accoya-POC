import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { api, ApiError } from './api';

const base = 'http://localhost:8000/api';
const server = setupServer();

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterEach(() => server.resetHandlers());
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
