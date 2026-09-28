// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { api } from '../../lib/api';
import { queryKeys } from '../../lib/queryKeys';
import type { Email, Lead, OverviewSummary } from '../../types';
import { OverviewPage } from './OverviewPage';

vi.mock('../../lib/api', () => ({
  api: {
    listLeads: vi.fn(),
    listEmails: vi.fn(),
    getOverviewSummary: vi.fn(),
    listDocuments: vi.fn(),
  },
}));

const lead = (id: string, project: string): Lead => ({
  id,
  external_id: `external-${id}`,
  section: null,
  project,
  location: 'Portland',
  state: 'OR',
  signal: null,
  intelligence: null,
  score: 80,
  timing: null,
  awarded_to: null,
  priority_reasons: null,
  summary: null,
  contacts: null,
  contact_email: null,
  meeting_date: null,
  tags: null,
  url: null,
  reported: null,
  due_date: null,
  award_date: null,
  start_date: null,
  response_deadline_evidence: null,
  keywords_matched: [],
  review_status: 'active',
  deleted_by: null,
  deleted_reasons: [],
  source_feed: null,
  created_at: '2026-07-01T00:00:00Z',
});

const email = (overrides: Partial<Email>): Email => ({
  id: 'email-1',
  lead_id: 'lead-1',
  recipient_email: null,
  subject: 'Current email',
  body: 'Body',
  signature: null,
  rendered_body: 'Body',
  status: 'approved',
  latest_delivery: null,
  has_unknown_delivery: false,
  delivery_content_hash: 'b'.repeat(64),
  created_at: '2026-07-03T00:00:00Z',
  updated_at: '2026-07-03T00:00:00Z',
  ...overrides,
});

const summary = (overrides: Partial<OverviewSummary> = {}): OverviewSummary => ({
  opportunities: 2,
  needs_review: 0,
  sent: 1,
  replies: 3,
  reply_sync_status: 'healthy',
  reply_last_synced_at: '2026-07-03T00:00:00Z',
  ...overrides,
});

function renderOverview(queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false } },
})) {
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter><OverviewPage /></MemoryRouter>
    </QueryClientProvider>,
  );
  return queryClient;
}

beforeEach(() => {
  vi.mocked(api.listLeads).mockResolvedValue([]);
  vi.mocked(api.listEmails).mockResolvedValue([]);
  vi.mocked(api.listDocuments).mockResolvedValue([]);
  vi.mocked(api.getOverviewSummary).mockResolvedValue(summary());
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('OverviewPage', () => {
  it('uses server totals, keeps filtered metrics static, and preserves recent outreach', async () => {
    vi.mocked(api.listLeads).mockResolvedValue([
      lead('lead-1', 'Harbour Arts Centre'),
      lead('lead-2', 'Cedar Library'),
    ]);
    vi.mocked(api.listEmails).mockResolvedValue([
      email({ id: 'email-old', subject: 'Historical sent email', status: 'sent', created_at: '2026-07-01T00:00:00Z' }),
      email({ id: 'email-current', status: 'approved' }),
      email({ id: 'email-2', lead_id: 'lead-2', subject: 'Library email', status: 'sent' }),
      email({
        id: 'email-dismissed',
        lead_id: 'lead-dismissed',
        subject: 'Dismissed pending email',
        status: 'pending_review',
      }),
    ]);

    renderOverview();

    const metrics = await screen.findByRole('region', { name: 'Workspace summary' });
    expect(within(metrics).getByLabelText('Opportunities: 2')).toBeInTheDocument();
    expect(within(metrics).getByLabelText('Needs review: 0')).toBeInTheDocument();
    expect(within(metrics).getByLabelText('Sent: 1')).toBeInTheDocument();
    expect(within(metrics).getByLabelText('Replies: 3')).toBeInTheDocument();
    expect(within(metrics).getByLabelText('Sent: 1').closest('a')).toBeNull();
    expect(within(metrics).getByLabelText('Strategy docs: 0')).toHaveAttribute('href', '/knowledge');
    expect(screen.queryByText('Historical sent email')).not.toBeInTheDocument();
    expect(screen.queryByText('Dismissed pending email')).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Current email/i })).toHaveAttribute(
      'href',
      '/opportunities/lead-1?email=email-current',
    );
  });

  it('applies inclusive local calendar dates only to the four summary metrics and clears to all time', async () => {
    vi.mocked(api.listLeads).mockResolvedValue([lead('lead-1', 'Harbour Arts Centre')]);
    vi.mocked(api.listEmails).mockResolvedValue([email({ id: 'email-current' })]);
    vi.mocked(api.getOverviewSummary)
      .mockResolvedValueOnce(summary())
      .mockResolvedValueOnce(summary({ opportunities: 1, needs_review: 1, sent: 0, replies: 2 }))
      .mockResolvedValue(summary());

    renderOverview();
    await screen.findByLabelText('Opportunities: 2');

    fireEvent.change(screen.getByLabelText('Start date'), { target: { value: '2026-07-01' } });
    fireEvent.change(screen.getByLabelText('End date'), { target: { value: '2026-07-31' } });
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }));

    const expectedRange = {
      start_at: new Date(2026, 6, 1).toISOString(),
      end_before: new Date(2026, 7, 1).toISOString(),
    };
    await waitFor(() => expect(api.getOverviewSummary).toHaveBeenCalledWith(expectedRange));
    expect(await screen.findByLabelText('Opportunities: 1')).toBeInTheDocument();
    expect(screen.getByLabelText('Replies: 2')).toBeInTheDocument();
    expect(screen.getByText('Harbour Arts Centre')).toBeInTheDocument();
    expect(screen.getByText('Current email')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
    expect(screen.getByLabelText('Start date')).toHaveValue('');
    expect(screen.getByLabelText('End date')).toHaveValue('');
    expect(await screen.findByText('Showing all time')).toBeInTheDocument();
    expect(await screen.findByLabelText('Opportunities: 2')).toBeInTheDocument();
  });

  it('validates complete ordered ranges without issuing another request', async () => {
    renderOverview();
    await screen.findByLabelText('Opportunities: 2');

    fireEvent.change(screen.getByLabelText('Start date'), { target: { value: '2026-07-20' } });
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Enter both a start date and an end date.');
    expect(api.getOverviewSummary).toHaveBeenCalledTimes(1);

    fireEvent.change(screen.getByLabelText('End date'), { target: { value: '2026-07-10' } });
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Start date must be on or before end date.');
    expect(api.getOverviewSummary).toHaveBeenCalledTimes(1);
  });

  it('does not present cached reply totals after summary refresh fails', async () => {
    vi.mocked(api.getOverviewSummary).mockRejectedValue(new Error('offline'));
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    queryClient.setQueryData(queryKeys.overviewSummary(), summary({ replies: 5 }));

    renderOverview(queryClient);

    await screen.findByText(/cached data is not shown as current/i);
    const metrics = screen.getByRole('region', { name: 'Workspace summary' });
    expect(within(metrics).getByLabelText('Replies: —')).toBeInTheDocument();
    expect(within(metrics).queryByLabelText('Replies: 5')).not.toBeInTheDocument();
  });

  it('keeps the reply metric hidden while tracking is feature-flagged off', async () => {
    vi.mocked(api.getOverviewSummary).mockResolvedValue(summary({
      replies: 0,
      reply_sync_status: 'disabled',
      reply_last_synced_at: null,
    }));

    renderOverview();

    await screen.findByRole('region', { name: 'Workspace summary' });
    await waitFor(() => expect(screen.queryByText('Replies')).not.toBeInTheDocument());
  });
});
