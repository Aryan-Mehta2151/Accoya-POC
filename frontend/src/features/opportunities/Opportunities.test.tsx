// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { api, ApiError } from '../../lib/api';
import { queryKeys } from '../../lib/queryKeys';
import type {
  EarlyBidSyncRunStatus,
  EarlyBidSyncStatus,
  Email,
  EmailDeliveryJob,
  EmailGenerationJob,
  Lead,
  LeadWorkspace,
} from '../../types';
import { OpportunitiesPage } from './OpportunitiesPage';
import { OpportunityDetailPage } from './OpportunityDetailPage';

vi.mock('../../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../lib/api')>();
  return {
    ...actual,
    api: {
      listLeads: vi.fn(),
      getLeadSyncStatus: vi.fn(),
      syncLeads: vi.fn(),
      uploadLeadsCsv: vi.fn(),
      deleteLead: vi.fn(),
      getLeadWorkspace: vi.fn(),
      queueEmailGeneration: vi.fn(),
      editEmail: vi.fn(),
      setEmailStatus: vi.fn(),
      sendEmail: vi.fn(),
    },
  };
});

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const lead = (overrides: Partial<Lead> = {}): Lead => ({
  id: 'lead-1',
  external_id: 'external-1',
  section: 'Commercial',
  project: 'Harbour Arts Centre',
  location: 'Portland',
  state: 'OR',
  signal: 'Specification',
  intelligence: 'Architect evaluating exterior timber.',
  score: 91,
  timing: 'Q4',
  awarded_to: null,
  priority_reasons: 'Strong specification signal',
  summary: 'A premium exterior timber opportunity.',
  contacts: 'Alex Morgan',
  contact_email: 'alex@example.com',
  meeting_date: null,
  tags: 'siding, architect',
  url: 'https://example.com/opportunity',
  source_feed: 'earlybid/client',
  created_at: '2026-07-01T00:00:00Z',
  ...overrides,
});

const email = (overrides: Partial<Email> = {}): Email => ({
  id: 'email-1',
  lead_id: 'lead-1',
  recipient_email: 'alex@example.com',
  subject: 'Accoya for Harbour Arts Centre',
  body: 'Hello Alex,\n\nA thoughtful message.',
  signature: null,
  rendered_body: 'Hello Alex,\n\nA thoughtful message.',
  status: 'pending_review',
  latest_delivery: null,
  has_unknown_delivery: false,
  delivery_content_hash: 'b'.repeat(64),
  created_at: '2026-07-02T00:00:00Z',
  updated_at: '2026-07-02T00:00:00Z',
  ...overrides,
});

const job = (overrides: Partial<EmailGenerationJob> = {}): EmailGenerationJob => ({
  id: 'job-1',
  lead_id: 'lead-1',
  retry_of_job_id: null,
  agent_run_id: null,
  trigger: 'manual',
  status: 'queued',
  requested_input_hash: 'a'.repeat(64),
  idempotency_key: '00000000-0000-4000-8000-000000000001',
  error_code: null,
  attempt_count: 0,
  queued_at: '2026-07-02T00:00:00Z',
  claimed_at: null,
  heartbeat_at: null,
  completed_at: null,
  ...overrides,
});

const deliveryJob = (overrides: Partial<EmailDeliveryJob> = {}): EmailDeliveryJob => ({
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
  subject: 'Accoya for Harbour Arts Centre',
  body_snapshot: 'Hello Alex,\n\nA thoughtful message.',
  error_code: null,
  attempt_count: 0,
  queued_at: '2026-07-02T00:02:00Z',
  claimed_at: null,
  heartbeat_at: null,
  send_started_at: null,
  accepted_at: null,
  completed_at: null,
  ...overrides,
});

const workspace = (overrides: Partial<LeadWorkspace> = {}): LeadWorkspace => ({
  lead: lead(),
  emails: [email()],
  default_email_signature: 'Doug Gillikin\nAccsys',
  current_email_id: 'email-1',
  current_email_is_stale: false,
  latest_generation: job({ status: 'generated', completed_at: '2026-07-02T00:01:00Z' }),
  ...overrides,
});

const automaticSyncStatus = (
  status: EarlyBidSyncRunStatus = 'succeeded',
  overrides: Partial<EarlyBidSyncStatus> = {},
): EarlyBidSyncStatus => ({
  timezone: 'America/Los_Angeles',
  next_scheduled_at: '2026-07-26T07:00:00Z',
  overdue: false,
  latest_run: {
    id: 'sync-run-1',
    feed: 'reseller/client',
    schedule_date: '2026-07-25',
    scheduled_for: '2026-07-25T07:00:00Z',
    status,
    attempt_count: 1,
    error_code: null,
    next_attempt_at: status === 'retry_wait' ? '2026-07-25T07:05:00Z' : null,
    created: status === 'succeeded' ? 1 : 0,
    updated: status === 'succeeded' ? 2 : 0,
    total: status === 'succeeded' ? 3 : 0,
    generation_queued: status === 'succeeded' ? 1 : 0,
    claimed_at: status === 'queued' ? null : '2026-07-25T07:00:01Z',
    completed_at: status === 'succeeded' || status === 'failed'
      ? '2026-07-25T07:00:30Z'
      : null,
  },
  ...overrides,
});

function renderAt(path: string) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const router = createMemoryRouter([
    { path: '/opportunities', element: <OpportunitiesPage /> },
    { path: '/opportunities/:leadId', element: <OpportunityDetailPage /> },
    { path: '/', element: <p>Overview destination</p> },
  ], { initialEntries: [path] });
  return {
    user: userEvent.setup(),
    router,
    queryClient,
    ...render(
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    ),
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.listLeads).mockResolvedValue([
    lead({
      current_email: {
        id: 'email-1',
        status: 'pending_review',
        recipient_email: 'alex@example.com',
        created_at: '2026-07-02T00:00:00Z',
        updated_at: '2026-07-02T00:00:00Z',
      },
    }),
    lead({ id: 'lead-2', project: 'Cedar Library', location: 'Austin', state: 'TX', score: 72 }),
  ]);
  vi.mocked(api.getLeadWorkspace).mockResolvedValue(workspace());
  vi.mocked(api.getLeadSyncStatus).mockResolvedValue(automaticSyncStatus());
  vi.mocked(api.syncLeads).mockResolvedValue({
    created: 1,
    updated: 1,
    total: 2,
    feed: 'earlybid/client',
    generation_queued: 1,
  });
  vi.mocked(api.uploadLeadsCsv).mockResolvedValue({
    items: [lead()],
    created: 1,
    updated: 0,
    total: 1,
    generation_queued: 1,
  });
  vi.mocked(api.deleteLead).mockResolvedValue({ id: 'lead-1', archived: true });
});

afterEach(() => cleanup());

describe('Opportunities list', () => {
  it('keeps automatic sync status visible while opportunities are loading', async () => {
    vi.mocked(api.listLeads).mockImplementation(() => new Promise(() => undefined));
    renderAt('/opportunities');

    expect(await screen.findByRole('heading', { name: 'Last automatic sync completed' })).toBeInTheDocument();
    expect(screen.getByText('Loading opportunities…')).toBeInTheDocument();
  });

  it('keeps automatic sync status visible when opportunities fail to load', async () => {
    vi.mocked(api.listLeads).mockRejectedValue(new Error('lead read failed'));
    renderAt('/opportunities');

    expect(await screen.findByRole('heading', { name: 'Opportunities could not be loaded' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Last automatic sync completed' })).toBeInTheDocument();
  });

  it('keeps automatic sync status and its manual-sync guard in the empty state', async () => {
    vi.mocked(api.listLeads).mockResolvedValue([]);
    vi.mocked(api.getLeadSyncStatus).mockResolvedValue(automaticSyncStatus('queued'));
    renderAt('/opportunities');

    expect(await screen.findByRole('heading', { name: 'No opportunities yet' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Automatic sync queued' })).toBeInTheDocument();
    const syncButtons = screen.getAllByRole('button', { name: 'Sync EarlyBid' });
    expect(syncButtons).toHaveLength(2);
    syncButtons.forEach((button) => expect(button).toBeDisabled());
  });

  it('keeps search and outreach filters in the URL-backed view', async () => {
    const { user } = renderAt('/opportunities?outreach=pending_review');
    const table = await screen.findByRole('table', { name: 'EarlyBid opportunities' });
    expect(within(table).getAllByRole('row')).toHaveLength(2);
    expect(within(table).getByText('Harbour Arts Centre')).toBeInTheDocument();
    expect(within(table).queryByText('Cedar Library')).not.toBeInTheDocument();

    await user.selectOptions(screen.getByRole('combobox', { name: 'Outreach' }), '');
    await waitFor(() => expect(within(table).getAllByRole('row')).toHaveLength(3));
    await user.type(screen.getByPlaceholderText(/Search project/i), 'Cedar');
    expect(screen.getByPlaceholderText(/Search project/i)).toHaveValue('Cedar');
    await waitFor(() => expect(within(table).getAllByRole('row')).toHaveLength(2));
  });

  it('sorts from the contact and score column headers and clears back to score order', async () => {
    vi.mocked(api.listLeads).mockResolvedValue([
      lead({
        id: 'lead-missing',
        project: 'Missing Contact Project',
        score: 99,
        contacts: null,
        contact_email: '   ',
      }),
      lead({
        id: 'lead-email',
        project: 'Email Contact Project',
        score: 80,
        contacts: null,
        contact_email: 'email@example.com',
      }),
      lead({
        id: 'lead-name',
        project: 'Named Contact Project',
        score: 60,
        contacts: 'Taylor Reed',
        contact_email: null,
      }),
    ]);
    const { user, router } = renderAt('/opportunities');
    const table = await screen.findByRole('table', { name: 'EarlyBid opportunities' });
    const rowOrder = () => within(table).getAllByRole('row').slice(1).map((row) => row.getAttribute('aria-label'));
    const scoreHeader = within(table).getByRole('button', { name: 'Sort score low to high' }).closest('th');

    expect(screen.queryByRole('combobox', { name: 'Sort by' })).not.toBeInTheDocument();
    expect(scoreHeader).toHaveAttribute('aria-sort', 'descending');

    await user.click(within(table).getByRole('button', { name: 'Sort contacts with provided first' }));
    expect(router.state.location.search).toBe('?sort=contact_present');
    expect(rowOrder()).toEqual([
      'Open opportunity Email Contact Project',
      'Open opportunity Named Contact Project',
      'Open opportunity Missing Contact Project',
    ]);
    expect(within(table).getByRole('button', { name: 'Sort contacts with missing first' }).closest('th'))
      .toHaveAttribute('aria-sort', 'descending');
    expect(scoreHeader).not.toHaveAttribute('aria-sort');
    expect(screen.getByText('Sorted by contact: provided first')).toBeInTheDocument();

    await user.click(within(table).getByRole('button', { name: 'Sort contacts with missing first' }));
    expect(router.state.location.search).toBe('?sort=contact_missing');
    expect(rowOrder()).toEqual([
      'Open opportunity Missing Contact Project',
      'Open opportunity Email Contact Project',
      'Open opportunity Named Contact Project',
    ]);
    expect(within(table).getByRole('button', { name: 'Sort contacts with provided first' }).closest('th'))
      .toHaveAttribute('aria-sort', 'ascending');
    expect(screen.getByText('Sorted by contact: missing first')).toBeInTheDocument();

    await user.click(within(table).getByRole('button', { name: 'Sort score high to low' }));
    expect(router.state.location.search).toBe('');
    expect(rowOrder()).toEqual([
      'Open opportunity Missing Contact Project',
      'Open opportunity Email Contact Project',
      'Open opportunity Named Contact Project',
    ]);

    await user.click(within(table).getByRole('button', { name: 'Sort score low to high' }));
    expect(router.state.location.search).toBe('?sort=asc');
    expect(rowOrder()).toEqual([
      'Open opportunity Named Contact Project',
      'Open opportunity Email Contact Project',
      'Open opportunity Missing Contact Project',
    ]);

    await user.click(screen.getByRole('button', { name: 'Clear' }));
    expect(router.state.location.search).toBe('');
    expect(rowOrder()).toEqual([
      'Open opportunity Missing Contact Project',
      'Open opportunity Email Contact Project',
      'Open opportunity Named Contact Project',
    ]);
    expect(within(table).getByRole('button', { name: 'Sort score low to high' }).closest('th'))
      .toHaveAttribute('aria-sort', 'descending');
    expect(screen.getByText('Sorted by score: high to low')).toBeInTheDocument();
  });

  it('keeps the existing ascending-score URL compatible with the score column header', async () => {
    renderAt('/opportunities?sort=asc');
    const table = await screen.findByRole('table', { name: 'EarlyBid opportunities' });

    expect(within(table).getByRole('button', { name: 'Sort score high to low' }).closest('th'))
      .toHaveAttribute('aria-sort', 'ascending');
    expect(within(table).getAllByRole('row').slice(1).map((row) => row.getAttribute('aria-label'))).toEqual([
      'Open opportunity Cedar Library',
      'Open opportunity Harbour Arts Centre',
    ]);
    expect(screen.getByText('Sorted by score: low to high')).toBeInTheDocument();
  });

  it('runs explicit feed sync and CSV import mutations', async () => {
    const { user } = renderAt('/opportunities');
    await screen.findByRole('table');

    await user.click(screen.getByRole('button', { name: 'Sync EarlyBid' }));
    await waitFor(() => expect(api.syncLeads).toHaveBeenCalledTimes(1));

    const csv = new File(['Project,Location\nHarbour,Portland'], 'leads.csv', { type: 'text/csv' });
    await user.upload(screen.getByLabelText('Choose an EarlyBid CSV file'), csv);
    await waitFor(() => expect(api.uploadLeadsCsv).toHaveBeenCalledWith(csv));
  });

  it('opens details when clicking anywhere on an opportunity row', async () => {
    const { user } = renderAt('/opportunities');
    await screen.findByRole('table');

    await user.click(screen.getByLabelText('Open opportunity Harbour Arts Centre'));

    expect(await screen.findByRole('heading', { name: 'Email workspace' })).toBeInTheDocument();
  });

  it('deletes an opportunity from the list after confirmation', async () => {
    const { user } = renderAt('/opportunities');
    await screen.findByRole('table');

    await user.click(screen.getAllByRole('button', { name: 'Delete opportunity Harbour Arts Centre' })[0]);
    const modal = screen.getByRole('dialog', { name: 'Delete opportunity?' });
    expect(within(modal).getByText('Harbour Arts Centre')).toBeInTheDocument();
    await user.click(within(modal).getByRole('button', { name: 'Delete opportunity' }));

    await waitFor(() => expect(api.deleteLead).toHaveBeenCalledWith('lead-1'));
  });

  it('shows the latest automatic result and next Pacific midnight without posting on mount', async () => {
    renderAt('/opportunities');

    expect(await screen.findByRole('heading', { name: 'Last automatic sync completed' })).toBeInTheDocument();
    expect(screen.getByText('3 processed · 1 created · 2 updated · 1 drafts queued')).toBeInTheDocument();
    expect(screen.getByText('Jul 26, 2026, 12:00 AM PDT')).toBeInTheDocument();
    const statusRegion = screen.getByRole('heading', { name: 'Last automatic sync completed' }).closest('section');
    expect(statusRegion).toHaveAttribute('aria-live', 'polite');
    expect(statusRegion).toHaveAttribute('aria-atomic', 'true');
    expect(api.getLeadSyncStatus).toHaveBeenCalledTimes(1);
    expect(api.syncLeads).not.toHaveBeenCalled();
  });

  it('refreshes leads once when the first automatic status is already successful', async () => {
    const { queryClient } = renderAt('/opportunities');
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');

    expect(await screen.findByRole('heading', { name: 'Last automatic sync completed' })).toBeInTheDocument();
    await waitFor(() => expect(invalidateSpy).toHaveBeenCalledTimes(1));
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.leads });
    await waitFor(() => expect(api.listLeads).toHaveBeenCalledTimes(2));

    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(invalidateSpy).toHaveBeenCalledTimes(1);
    expect(api.listLeads).toHaveBeenCalledTimes(2);
  });

  it.each([
    ['queued', 'Automatic sync queued'],
    ['running', 'Automatic sync in progress'],
    ['retry_wait', 'Automatic sync retry scheduled'],
  ] as const)('disables manual sync while the daily run is %s', async (status, heading) => {
    vi.mocked(api.getLeadSyncStatus).mockResolvedValue(automaticSyncStatus(status));
    const { user } = renderAt('/opportunities');

    expect(await screen.findByRole('heading', { name: heading })).toBeInTheDocument();
    const syncButton = screen.getByRole('button', { name: 'Sync EarlyBid' });
    expect(syncButton).toBeDisabled();
    await user.click(syncButton);
    expect(api.syncLeads).not.toHaveBeenCalled();
    if (status === 'retry_wait') {
      expect(screen.getByText('Jul 25, 2026, 12:05 AM PDT')).toBeInTheDocument();
    }
  });

  it.each([
    'queued',
    'running',
    'retry_wait',
  ] as const)('keeps manual sync disabled when a %s daily run is overdue', async (status) => {
    vi.mocked(api.getLeadSyncStatus).mockResolvedValue(automaticSyncStatus(status, { overdue: true }));
    const intervalSpy = vi.spyOn(window, 'setInterval');
    renderAt('/opportunities');

    expect(await screen.findByText('Overdue')).toBeInTheDocument();
    const syncButton = screen.getByRole('button', { name: 'Sync EarlyBid' });
    expect(syncButton).toBeDisabled();
    expect(api.syncLeads).not.toHaveBeenCalled();
    expect(intervalSpy.mock.calls.some((call) => call[1] === 5_000)).toBe(true);
    intervalSpy.mockRestore();
  });

  it('shows safe failure and overdue context while keeping manual sync available', async () => {
    const failed = automaticSyncStatus('failed', { overdue: true });
    if (failed.latest_run) {
      failed.latest_run.error_code = 'upstream_rate_limited';
      failed.latest_run.attempt_count = 4;
    }
    vi.mocked(api.getLeadSyncStatus).mockResolvedValue(failed);
    renderAt('/opportunities');

    expect(await screen.findByRole('heading', { name: 'Last automatic sync failed' })).toBeInTheDocument();
    expect(screen.getByText('EarlyBid temporarily limited requests.')).toBeInTheDocument();
    expect(screen.getByText('Overdue')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Sync EarlyBid' })).toBeEnabled();
  });

  it('uses active and idle polling intervals and refreshes leads on automatic success', async () => {
    vi.mocked(api.getLeadSyncStatus).mockResolvedValue(automaticSyncStatus('queued'));
    const intervalSpy = vi.spyOn(window, 'setInterval');
    const { queryClient } = renderAt('/opportunities');
    await screen.findByRole('heading', { name: 'Automatic sync queued' });
    await waitFor(() => expect(intervalSpy.mock.calls.some((call) => call[1] === 5_000)).toBe(true));
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');

    act(() => {
      queryClient.setQueryData(queryKeys.leadSyncStatus, automaticSyncStatus('succeeded'));
    });

    expect(await screen.findByRole('heading', { name: 'Last automatic sync completed' })).toBeInTheDocument();
    await waitFor(() => expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.leads }));
    await waitFor(() => expect(intervalSpy.mock.calls.some((call) => call[1] === 60_000)).toBe(true));
    intervalSpy.mockRestore();
  });
});

describe('Opportunity email workspace', () => {
  it('shows the generated email inline without generating on mount', async () => {
    renderAt('/opportunities/lead-1');
    expect(await screen.findByRole('heading', { name: 'Email workspace' })).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'To' })).toHaveValue('alex@example.com');
    expect(screen.getByRole('textbox', { name: 'Subject' })).toHaveValue('Accoya for Harbour Arts Centre');
    expect(screen.getAllByText('alex@example.com', { selector: 'dd' })).toHaveLength(2);
    expect(api.queueEmailGeneration).not.toHaveBeenCalled();
  });

  it('queues the first email and shows a passive waiting state', async () => {
    vi.mocked(api.getLeadWorkspace).mockResolvedValue(workspace({
      emails: [],
      current_email_id: null,
      latest_generation: null,
    }));
    vi.mocked(api.queueEmailGeneration).mockResolvedValue(job());
    const { user } = renderAt('/opportunities/lead-1');

    await user.click(await screen.findByRole('button', { name: 'Generate email' }));
    await waitFor(() => expect(api.queueEmailGeneration).toHaveBeenCalledTimes(1));
    expect(api.queueEmailGeneration).toHaveBeenCalledWith('lead-1', expect.stringMatching(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
    ));
    expect(await screen.findByText('Email queued')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Generation in progress/i })).toBeDisabled();
  });

  it('refreshes opportunity badges when an active generation fails without creating an email', async () => {
    const queuedWorkspace = workspace({
      emails: [],
      current_email_id: null,
      latest_generation: job({ status: 'queued' }),
    });
    vi.mocked(api.getLeadWorkspace).mockResolvedValue(queuedWorkspace);
    const { queryClient } = renderAt('/opportunities/lead-1');
    await screen.findByText('Email queued');
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');

    act(() => {
      queryClient.setQueryData(queryKeys.leadWorkspace('lead-1'), {
        ...queuedWorkspace,
        latest_generation: job({
          status: 'provider_error',
          error_code: 'provider_unavailable',
          completed_at: '2026-07-02T00:02:00Z',
        }),
      });
    });

    await screen.findByText('Email generation needs attention');
    await waitFor(() => expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.leads }));
  });

  it('saves edits before enabling review actions', async () => {
    const updated = email({ subject: 'A considered Accoya proposal' });
    vi.mocked(api.editEmail).mockResolvedValue(updated);
    vi.mocked(api.setEmailStatus).mockResolvedValue({ ...updated, status: 'approved' });
    const { user } = renderAt('/opportunities/lead-1');

    const subject = await screen.findByRole('textbox', { name: 'Subject' });
    await user.clear(subject);
    await user.type(subject, updated.subject);
    expect(screen.getByRole('button', { name: 'Approve' })).toBeDisabled();

    await user.click(screen.getByRole('button', { name: 'Save changes' }));
    await waitFor(() => expect(api.editEmail).toHaveBeenCalledWith('email-1', {
      recipient_email: 'alex@example.com',
      subject: updated.subject,
      body: email().body,
    }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Approve' })).toBeEnabled());
    await user.click(screen.getByRole('button', { name: 'Approve' }));
    expect(await screen.findByRole('heading', { name: 'Approve this email?' })).toBeInTheDocument();
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Approve' }));
    await waitFor(() => expect(api.setEmailStatus).toHaveBeenCalledWith(
      'email-1',
      'approved',
      updated.delivery_content_hash,
    ));
  });

  it('does not expose signature controls for non-US opportunities', async () => {
    const unsigned = email({ signature: null, rendered_body: email().body });
    vi.mocked(api.getLeadWorkspace).mockResolvedValue(workspace({
      lead: lead({ state: 'NL', location: 'Amsterdam' }),
      emails: [unsigned],
      default_email_signature: '',
    }));
    const { user } = renderAt('/opportunities/lead-1');

    await screen.findByRole('textbox', { name: 'Message' });
    expect(screen.queryByRole('textbox', { name: 'Email signature' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /add default signature/i })).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Approve' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('alex@example.com')).toBeInTheDocument();
    expect(dialog.querySelector('pre')?.textContent).toBe(unsigned.rendered_body);
  });

  it('closes a stale approval preview and refreshes the workspace', async () => {
    vi.mocked(api.setEmailStatus).mockRejectedValue(new ApiError({
      status: 409,
      code: 'content_changed',
      message: 'The email changed after the preview opened',
    }));
    const { user, queryClient } = renderAt('/opportunities/lead-1');
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');

    await user.click(await screen.findByRole('button', { name: 'Approve' }));
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Approve' }));

    await waitFor(() => expect(
      screen.queryByRole('heading', { name: 'Approve this email?' }),
    ).not.toBeInTheDocument());
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: queryKeys.leadWorkspace('lead-1'),
    });
  });

  it('lets a reviewer add a missing recipient and blocks approval until it is saved', async () => {
    const withoutRecipient = email({ recipient_email: null });
    vi.mocked(api.getLeadWorkspace).mockResolvedValue(workspace({ emails: [withoutRecipient] }));
    vi.mocked(api.editEmail).mockResolvedValue(email({ recipient_email: 'reviewer@example.com' }));
    const { user } = renderAt('/opportunities/lead-1');

    const recipient = await screen.findByRole('textbox', { name: 'To' });
    expect(recipient).toHaveValue('');
    expect(screen.getByRole('button', { name: 'Approve' })).toBeDisabled();
    expect(screen.getByText(/Add a valid recipient, subject, and message/i)).toBeInTheDocument();

    await user.type(recipient, 'reviewer@example.com');
    await user.click(screen.getByRole('button', { name: 'Save changes' }));
    await waitFor(() => expect(api.editEmail).toHaveBeenCalledWith('email-1', {
      recipient_email: 'reviewer@example.com',
      subject: withoutRecipient.subject,
      body: withoutRecipient.body,
    }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Approve' })).toBeEnabled());
  });

  it('validates recipient edits while allowing a saved recipient to be cleared', async () => {
    vi.mocked(api.editEmail).mockResolvedValue(email({ recipient_email: null }));
    const { user } = renderAt('/opportunities/lead-1');
    const recipient = await screen.findByRole('textbox', { name: 'To' });

    await user.clear(recipient);
    await user.type(recipient, 'not-an-email');
    await user.click(screen.getByRole('button', { name: 'Save changes' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Enter a valid recipient email address.');
    expect(api.editEmail).not.toHaveBeenCalled();

    await user.clear(recipient);
    await user.click(screen.getByRole('button', { name: 'Save changes' }));
    await waitFor(() => expect(api.editEmail).toHaveBeenCalledWith('email-1', {
      recipient_email: null,
      subject: email().subject,
      body: email().body,
    }));
  });

  it('returns an edited approved email to review before it can be sent', async () => {
    const approved = email({ status: 'approved' });
    vi.mocked(api.getLeadWorkspace).mockResolvedValue(workspace({ emails: [approved] }));
    vi.mocked(api.editEmail).mockResolvedValue(email({
      recipient_email: 'new-recipient@example.com',
      status: 'pending_review',
    }));
    const { user } = renderAt('/opportunities/lead-1');

    const recipient = await screen.findByRole('textbox', { name: 'To' });
    await user.clear(recipient);
    await user.type(recipient, 'new-recipient@example.com');
    expect(screen.getByText('Saving these changes will require approval again.')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Save changes' }));

    expect(await screen.findByRole('button', { name: 'Approve' })).toBeEnabled();
    expect(screen.queryByRole('button', { name: 'Send email' })).not.toBeInTheDocument();
  });

  it('confirms and queues a real delivery with the current content hash', async () => {
    const approved = email({ status: 'approved' });
    vi.mocked(api.getLeadWorkspace).mockResolvedValue(workspace({ emails: [approved] }));
    vi.mocked(api.sendEmail).mockResolvedValue(deliveryJob());
    const { user } = renderAt('/opportunities/lead-1');

    await user.click(await screen.findByRole('button', { name: 'Send email' }));
    const dialog = screen.getByRole('dialog');
    const confirmation = within(dialog).getByText(/real external email/i);
    expect(confirmation).toHaveTextContent('Recipient: alex@example.com');
    expect(confirmation).toHaveTextContent('Subject: Accoya for Harbour Arts Centre');
    await user.click(within(dialog).getByRole('button', { name: 'Send email' }));

    await waitFor(() => expect(api.sendEmail).toHaveBeenCalledWith('email-1', {
      idempotency_key: expect.stringMatching(
        /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
      ),
      expected_content_hash: 'b'.repeat(64),
      acknowledge_duplicate_risk: false,
    }));
    expect(await screen.findByText('Email queued for delivery')).toBeInTheDocument();
  });

  it('reuses the delivery idempotency key after a network error', async () => {
    const approved = email({ status: 'approved' });
    vi.mocked(api.getLeadWorkspace).mockResolvedValue(workspace({ emails: [approved] }));
    vi.mocked(api.sendEmail)
      .mockRejectedValueOnce(new ApiError({ status: 0, message: 'Connection interrupted' }))
      .mockResolvedValueOnce(deliveryJob());
    const { user } = renderAt('/opportunities/lead-1');

    await user.click(await screen.findByRole('button', { name: 'Send email' }));
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Send email' }));
    await waitFor(() => expect(api.sendEmail).toHaveBeenCalledTimes(1));

    expect(screen.getByRole('textbox', { name: 'To' })).toHaveAttribute('readonly');
    expect(screen.getByRole('button', { name: 'Regenerate email' })).toBeDisabled();
    expect(screen.getByText(/safely reuse the same request key/i)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Retry send' }));
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Retry send' }));
    await waitFor(() => expect(api.sendEmail).toHaveBeenCalledTimes(2));
    const firstPayload = vi.mocked(api.sendEmail).mock.calls[0][1];
    const secondPayload = vi.mocked(api.sendEmail).mock.calls[1][1];
    expect(secondPayload.idempotency_key).toBe(firstPayload.idempotency_key);
  });

  it('uses a new idempotency key after a definitive API response', async () => {
    const approved = email({ status: 'approved' });
    vi.mocked(api.getLeadWorkspace).mockResolvedValue(workspace({ emails: [approved] }));
    vi.mocked(api.sendEmail)
      .mockRejectedValueOnce(new ApiError({ status: 503, message: 'Delivery is not configured' }))
      .mockResolvedValueOnce(deliveryJob());
    const { user } = renderAt('/opportunities/lead-1');

    await user.click(await screen.findByRole('button', { name: 'Send email' }));
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Send email' }));
    await waitFor(() => expect(api.sendEmail).toHaveBeenCalledTimes(1));
    expect(screen.getByRole('textbox', { name: 'To' })).not.toHaveAttribute('readonly');

    await user.click(screen.getByRole('button', { name: 'Send email' }));
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Send email' }));
    await waitFor(() => expect(api.sendEmail).toHaveBeenCalledTimes(2));
    const firstPayload = vi.mocked(api.sendEmail).mock.calls[0][1];
    const secondPayload = vi.mocked(api.sendEmail).mock.calls[1][1];
    expect(secondPayload.idempotency_key).not.toBe(firstPayload.idempotency_key);
  });

  it('suppresses duplicate delivery submissions while the first request is pending', async () => {
    const approved = email({ status: 'approved' });
    vi.mocked(api.getLeadWorkspace).mockResolvedValue(workspace({ emails: [approved] }));
    let resolveDelivery!: (job: EmailDeliveryJob) => void;
    vi.mocked(api.sendEmail).mockImplementation(() => new Promise((resolve) => {
      resolveDelivery = resolve;
    }));
    const { user } = renderAt('/opportunities/lead-1');

    await user.click(await screen.findByRole('button', { name: 'Send email' }));
    await user.dblClick(within(screen.getByRole('dialog')).getByRole('button', { name: 'Send email' }));
    expect(api.sendEmail).toHaveBeenCalledTimes(1);
    act(() => resolveDelivery(deliveryJob()));
    expect(await screen.findByText('Email queued for delivery')).toBeInTheDocument();
  });

  it('locks the editor and refreshes every two seconds while delivery is active', async () => {
    const intervalSpy = vi.spyOn(window, 'setInterval');
    const active = email({ status: 'approved', latest_delivery: deliveryJob({ status: 'running' }) });
    vi.mocked(api.getLeadWorkspace).mockResolvedValue(workspace({ emails: [active] }));
    renderAt('/opportunities/lead-1');

    expect(await screen.findByText('Sending email')).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'To' })).toHaveAttribute('readonly');
    expect(screen.getByRole('button', { name: 'Sending...' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Regenerate email' })).toBeDisabled();
    await waitFor(() => expect(intervalSpy.mock.calls.some((call) => call[1] === 2_000)).toBe(true));
    intervalSpy.mockRestore();
  });

  it('offers a normal retry after definite failure and warns before an uncertain resend', async () => {
    const uncertain = email({
      status: 'approved',
      latest_delivery: deliveryJob({
        status: 'delivery_unknown',
        error_code: 'smtp_outcome_unknown',
        completed_at: '2026-07-02T00:03:00Z',
      }),
      has_unknown_delivery: true,
    });
    vi.mocked(api.getLeadWorkspace).mockResolvedValue(workspace({ emails: [uncertain] }));
    vi.mocked(api.sendEmail).mockResolvedValue(deliveryJob({
      id: 'delivery-2',
      retry_of_job_id: 'delivery-1',
    }));
    const { user } = renderAt('/opportunities/lead-1');

    expect(await screen.findByText('Delivery status is uncertain')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Regenerate email' })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: 'Send again anyway' }));
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByText(/could create a duplicate/i)).toBeInTheDocument();
    await user.click(within(dialog).getByRole('button', { name: 'Send again anyway' }));
    await waitFor(() => expect(api.sendEmail).toHaveBeenCalledWith('email-1', expect.objectContaining({
      acknowledge_duplicate_risk: true,
    })));

    cleanup();
    vi.mocked(api.getLeadWorkspace).mockResolvedValue(workspace({
      emails: [email({
        status: 'approved',
        latest_delivery: deliveryJob({ status: 'failed', error_code: 'smtp_rejected' }),
      })],
    }));
    renderAt('/opportunities/lead-1');
    expect(await screen.findByRole('button', { name: 'Retry send' })).toBeEnabled();
    expect(screen.getByText('Email could not be sent')).toBeInTheDocument();
  });

  it('retains an unknown-delivery warning when edits require reapproval', async () => {
    const uncertain = email({
      status: 'approved',
      latest_delivery: deliveryJob({ status: 'delivery_unknown', completed_at: '2026-07-02T00:03:00Z' }),
      has_unknown_delivery: true,
    });
    const edited = email({
      status: 'pending_review',
      subject: 'Updated after uncertain delivery',
      latest_delivery: uncertain.latest_delivery,
      has_unknown_delivery: true,
    });
    vi.mocked(api.getLeadWorkspace).mockResolvedValue(workspace({ emails: [uncertain] }));
    vi.mocked(api.editEmail).mockResolvedValue(edited);
    vi.mocked(api.setEmailStatus).mockResolvedValue({ ...edited, status: 'approved' });
    const { user } = renderAt('/opportunities/lead-1');

    const subject = await screen.findByRole('textbox', { name: 'Subject' });
    await user.clear(subject);
    await user.type(subject, edited.subject);
    await user.click(screen.getByRole('button', { name: 'Save changes' }));

    expect(await screen.findByRole('button', { name: 'Approve' })).toBeEnabled();
    expect(screen.getByText('Delivery status is uncertain')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Send again anyway' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Approve' }));
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Approve' }));
    expect(await screen.findByRole('button', { name: 'Send again anyway' })).toBeEnabled();
  });

  it('opens historical drafts read-only from the email query parameter', async () => {
    const previous = email({
      id: 'email-old',
      subject: 'Earlier draft',
      created_at: '2026-07-01T00:00:00Z',
      updated_at: '2026-07-01T00:00:00Z',
    });
    vi.mocked(api.getLeadWorkspace).mockResolvedValue(workspace({ emails: [email(), previous] }));
    renderAt('/opportunities/lead-1?email=email-old');

    expect(await screen.findByRole('textbox', { name: 'Subject' })).toHaveValue('Earlier draft');
    expect(screen.getByRole('textbox', { name: 'Subject' })).toHaveAttribute('readonly');
    expect(screen.getByText('Previous draft', { selector: 'span' })).toBeInTheDocument();
  });

  it('does not replace unsaved edits when a newly generated draft arrives', async () => {
    const nextEmail = email({
      id: 'email-2',
      subject: 'Newly generated draft',
      created_at: '2026-07-03T00:00:00Z',
      updated_at: '2026-07-03T00:00:00Z',
    });
    const { user, queryClient } = renderAt('/opportunities/lead-1');
    const subject = await screen.findByRole('textbox', { name: 'Subject' });
    await user.type(subject, ' with edits');

    act(() => {
      queryClient.setQueryData(queryKeys.leadWorkspace('lead-1'), workspace({
        emails: [nextEmail, email()],
        current_email_id: 'email-2',
        latest_generation: job({ status: 'generated', completed_at: '2026-07-03T00:01:00Z' }),
      }));
    });

    expect(await screen.findByText('New draft ready')).toBeInTheDocument();
    expect(subject).toHaveValue('Accoya for Harbour Arts Centre with edits');
    await user.click(screen.getByRole('button', { name: 'Discard edits and open' }));
    await waitFor(() => expect(screen.getByRole('textbox', { name: 'Subject' })).toHaveValue('Newly generated draft'));
  });

  it('keeps a previous email visible when regeneration fails', async () => {
    vi.mocked(api.getLeadWorkspace).mockResolvedValue(workspace({
      latest_generation: job({ status: 'provider_error', error_code: 'provider_unavailable' }),
    }));
    renderAt('/opportunities/lead-1');

    expect(await screen.findByText('Email generation needs attention')).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Subject' })).toHaveValue('Accoya for Harbour Arts Centre');
    expect(screen.getByRole('button', { name: 'Retry generation' })).toBeInTheDocument();
  });

  it('shows a small draft-quality notice when generated with limited context', async () => {
    vi.mocked(api.getLeadWorkspace).mockResolvedValue(workspace({
      latest_generation: job({ status: 'generated', error_code: 'limited_context_best_effort' }),
      emails: [email()],
      current_email_id: 'email-1',
    }));
    renderAt('/opportunities/lead-1');

    expect(await screen.findByText(/Draft quality notice/i)).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Subject' })).toHaveValue('Accoya for Harbour Arts Centre');
  });
});
