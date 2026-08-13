// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { api } from '../../lib/api';
import type { Email, Lead } from '../../types';
import { OverviewPage } from './OverviewPage';

vi.mock('../../lib/api', () => ({
  api: {
    listLeads: vi.fn(),
    listEmails: vi.fn(),
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
    ]);
    vi.mocked(api.listDocuments).mockResolvedValue([]);
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter><OverviewPage /></MemoryRouter>
      </QueryClientProvider>,
    );

    const summary = await screen.findByRole('region', { name: 'Workspace summary' });
    const needsReview = within(summary).getByText('Needs review').closest('article');
    const sent = within(summary).getByText('Sent').closest('article');
    expect(within(needsReview!).getByText('0')).toBeInTheDocument();
    expect(within(sent!).getByText('1')).toBeInTheDocument();
    expect(screen.queryByText('Historical sent email')).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Current email/i })).toHaveAttribute(
      'href',
      '/opportunities/lead-1?email=email-current',
    );
  });
});
