// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { api } from '../../lib/api';
import { queryKeys } from '../../lib/queryKeys';
import type { Email, Lead } from '../../types';
import { OverviewPage } from './OverviewPage';

vi.mock('../../lib/api', () => ({
  api: {
    listLeads: vi.fn(),
    listEmails: vi.fn(),
    getEmailReplySummary: vi.fn(),
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

afterEach(() => cleanup());

describe('OverviewPage', () => {
  it('counts sent emails and links only the newest email for each opportunity', async () => {
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
    vi.mocked(api.listDocuments).mockResolvedValue([]);
    vi.mocked(api.getEmailReplySummary).mockResolvedValue({
      unread_reply_count: 3,
      replied_opportunity_count: 2,
      last_synced_at: '2026-07-03T00:00:00Z',
      sync_status: 'healthy',
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter><OverviewPage /></MemoryRouter>
      </QueryClientProvider>,
    );

    const summary = await screen.findByRole('region', { name: 'Workspace summary' });
    const needsReview = within(summary).getByText('Needs review').closest('a');
    const sent = within(summary).getByText('Sent').closest('a');
    expect(within(needsReview!).getByText('0')).toBeInTheDocument();
    expect(within(sent!).getByText('1')).toBeInTheDocument();
    const replies = within(summary).getByText('Unread replies').closest('a');
    expect(within(replies!).getByText('3')).toBeInTheDocument();
    expect(replies).toHaveAttribute(
      'href',
      '/opportunities?replies=unread&sort=latest_reply',
    );
    expect(screen.queryByText('Historical sent email')).not.toBeInTheDocument();
    expect(screen.queryByText('Dismissed pending email')).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Current email/i })).toHaveAttribute(
      'href',
      '/opportunities/lead-1?email=email-current',
    );
  });

  it('does not present a stale cached reply count as a current zero or total', async () => {
    vi.mocked(api.listLeads).mockResolvedValue([]);
    vi.mocked(api.listEmails).mockResolvedValue([]);
    vi.mocked(api.listDocuments).mockResolvedValue([]);
    vi.mocked(api.getEmailReplySummary).mockResolvedValue({
      unread_reply_count: 7,
      replied_opportunity_count: 4,
      last_synced_at: '2026-07-03T00:00:00Z',
      sync_status: 'stale',
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter><OverviewPage /></MemoryRouter>
      </QueryClientProvider>,
    );

    const summary = await screen.findByRole('region', { name: 'Workspace summary' });
    const replies = within(summary).getByText('Unread replies').closest('a');
    expect(within(replies!).getByText('—')).toBeInTheDocument();
    expect(within(replies!).queryByText('7')).not.toBeInTheDocument();
    expect(screen.getByText(/cached data is not shown as current/i)).toBeInTheDocument();
  });

  it('does not present a previously healthy count after a refresh fails', async () => {
    vi.mocked(api.listLeads).mockResolvedValue([]);
    vi.mocked(api.listEmails).mockResolvedValue([]);
    vi.mocked(api.listDocuments).mockResolvedValue([]);
    vi.mocked(api.getEmailReplySummary).mockRejectedValue(new Error('offline'));
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    queryClient.setQueryData(queryKeys.emailReplySummary, {
      unread_reply_count: 5,
      replied_opportunity_count: 3,
      last_synced_at: '2026-07-03T00:00:00Z',
      sync_status: 'healthy',
    });

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter><OverviewPage /></MemoryRouter>
      </QueryClientProvider>,
    );

    await screen.findByText(/cached data is not shown as current/i);
    const summary = screen.getByRole('region', { name: 'Workspace summary' });
    const replies = within(summary).getByText('Unread replies').closest('a');
    expect(within(replies!).getByText('—')).toBeInTheDocument();
    expect(within(replies!).queryByText('5')).not.toBeInTheDocument();
  });

  it('keeps the reply metric hidden while tracking is feature-flagged off', async () => {
    vi.mocked(api.listLeads).mockResolvedValue([]);
    vi.mocked(api.listEmails).mockResolvedValue([]);
    vi.mocked(api.listDocuments).mockResolvedValue([]);
    vi.mocked(api.getEmailReplySummary).mockResolvedValue({
      unread_reply_count: 0,
      replied_opportunity_count: 0,
      last_synced_at: null,
      sync_status: 'disabled',
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter><OverviewPage /></MemoryRouter>
      </QueryClientProvider>,
    );

    await screen.findByRole('region', { name: 'Workspace summary' });
    await waitFor(() => expect(screen.queryByText('Unread replies')).not.toBeInTheDocument());
  });
});
