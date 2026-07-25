// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { api } from '../../lib/api';
import { queryKeys } from '../../lib/queryKeys';
import type { Email, EmailGenerationJob, Lead, LeadWorkspace } from '../../types';
import { OpportunitiesPage } from './OpportunitiesPage';
import { OpportunityDetailPage } from './OpportunityDetailPage';

vi.mock('../../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../lib/api')>();
  return {
    ...actual,
    api: {
      listLeads: vi.fn(),
      syncLeads: vi.fn(),
      uploadLeadsCsv: vi.fn(),
      getLeadWorkspace: vi.fn(),
      queueEmailGeneration: vi.fn(),
      editEmail: vi.fn(),
      setEmailStatus: vi.fn(),
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
  status: 'pending_review',
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

const workspace = (overrides: Partial<LeadWorkspace> = {}): LeadWorkspace => ({
  lead: lead(),
  emails: [email()],
  current_email_id: 'email-1',
  current_email_is_stale: false,
  latest_generation: job({ status: 'generated', completed_at: '2026-07-02T00:01:00Z' }),
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
});

afterEach(() => cleanup());

describe('Opportunities list', () => {
  it('keeps search and outreach filters in the URL-backed view', async () => {
    const { user } = renderAt('/opportunities?outreach=pending_review');
    const table = await screen.findByRole('table', { name: 'EarlyBid opportunities' });
    expect(within(table).getAllByRole('row')).toHaveLength(2);
    expect(within(table).getByText('Harbour Arts Centre')).toBeInTheDocument();
    expect(within(table).queryByText('Cedar Library')).not.toBeInTheDocument();

    await user.selectOptions(screen.getByRole('combobox', { name: 'Outreach' }), '');
    await waitFor(() => expect(within(table).getAllByRole('row')).toHaveLength(3));
    await user.type(screen.getByPlaceholderText(/Search project/i), 'Cedar');
    await waitFor(() => expect(within(table).getAllByRole('row')).toHaveLength(2));
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
});

describe('Opportunity email workspace', () => {
  it('shows the generated email inline without generating on mount', async () => {
    renderAt('/opportunities/lead-1');
    expect(await screen.findByRole('heading', { name: 'Email workspace' })).toBeInTheDocument();
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
      subject: updated.subject,
      body: email().body,
    }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Approve' })).toBeEnabled());
    await user.click(screen.getByRole('button', { name: 'Approve' }));
    await waitFor(() => expect(api.setEmailStatus).toHaveBeenCalledWith('email-1', 'approved'));
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
});
