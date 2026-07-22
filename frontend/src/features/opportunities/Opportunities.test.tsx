// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { api, ApiError } from '../../lib/api';
import type { Email, Lead } from '../../types';
import { OpportunitiesPage } from './OpportunitiesPage';
import { OpportunityDetailPage } from './OpportunityDetailPage';

vi.mock('../../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../lib/api')>();
  return {
    ...actual,
    api: {
      listLeads: vi.fn(),
      listEmails: vi.fn(),
      syncLeads: vi.fn(),
      uploadLeadsCsv: vi.fn(),
      generateEmail: vi.fn(),
    },
  };
});

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

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

const generatedEmail: Email = {
  id: 'email-1',
  lead_id: 'lead-1',
  subject: 'Accoya for Harbour Arts Centre',
  body: 'Hello Alex',
  status: 'pending_review',
  created_at: '2026-07-02T00:00:00Z',
  updated_at: '2026-07-02T00:00:00Z',
};

function renderAt(path: string, detail = false) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const router = createMemoryRouter(
    [
      { path: '/opportunities', element: <OpportunitiesPage /> },
      { path: '/opportunities/:leadId', element: detail ? <OpportunityDetailPage /> : <OpportunitiesPage /> },
      { path: '/outreach/:emailId', element: <p>Email destination</p> },
    ],
    { initialEntries: [path] },
  );
  return render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.listLeads).mockResolvedValue([
    lead(),
    lead({ id: 'lead-2', project: 'Cedar Library', location: 'Austin', state: 'TX', score: 72 }),
  ]);
  vi.mocked(api.listEmails).mockResolvedValue([]);
  vi.mocked(api.syncLeads).mockResolvedValue({ created: 1, updated: 1, total: 2, feed: 'earlybid/client' });
  vi.mocked(api.uploadLeadsCsv).mockResolvedValue([lead()]);
});

afterEach(() => cleanup());

describe('Opportunities', () => {
  it('filters the table locally and keeps controls in the URL-backed view', async () => {
    const user = userEvent.setup();
    renderAt('/opportunities');

    const table = await screen.findByRole('table', { name: 'EarlyBid opportunities' });
    expect(within(table).getAllByRole('row')).toHaveLength(3);

    await user.type(screen.getByPlaceholderText(/Search project/i), 'Harbour');
    await waitFor(() => expect(within(table).getAllByRole('row')).toHaveLength(2));
    expect(within(table).getByText('Harbour Arts Centre')).toBeInTheDocument();
    expect(within(table).queryByText('Cedar Library')).not.toBeInTheDocument();
  });

  it('runs explicit feed sync and CSV import mutations', async () => {
    const user = userEvent.setup();
    renderAt('/opportunities');
    await screen.findByRole('table');

    await user.click(screen.getByRole('button', { name: 'Sync EarlyBid' }));
    await waitFor(() => expect(api.syncLeads).toHaveBeenCalledTimes(1));

    const csv = new File(['Project,Location\nHarbour,Portland'], 'leads.csv', { type: 'text/csv' });
    await user.upload(screen.getByLabelText('Choose an EarlyBid CSV file'), csv);
    await waitFor(() => expect(api.uploadLeadsCsv).toHaveBeenCalledWith(csv));
  });

  it('shows only a generation waiting state and routes to the resulting draft', async () => {
    const user = userEvent.setup();
    let resolveEmail: (email: Email) => void = () => undefined;
    vi.mocked(api.generateEmail).mockReturnValue(new Promise((resolve) => { resolveEmail = resolve; }));
    renderAt('/opportunities/lead-1', true);

    const generateButtons = await screen.findAllByRole('button', { name: 'Generate outreach' });
    await user.click(generateButtons[0]);
    expect(screen.getByRole('status')).toHaveTextContent('Generating outreach');
    expect(api.generateEmail).toHaveBeenCalledTimes(1);

    resolveEmail(generatedEmail);
    expect(await screen.findByText('Email destination')).toBeInTheDocument();
  });

  it('explains when more lead context is needed', async () => {
    const user = userEvent.setup();
    vi.mocked(api.generateEmail).mockRejectedValue(new ApiError({
      status: 422,
      code: 'insufficient_context',
      message: 'Not enough context',
      warnings: ['Add a useful project summary.'],
    }));
    renderAt('/opportunities/lead-1', true);

    const generateButtons = await screen.findAllByRole('button', { name: 'Generate outreach' });
    await user.click(generateButtons[0]);

    expect(await screen.findByRole('heading', { name: 'More context is needed' })).toBeInTheDocument();
    expect(screen.getByText('Add a useful project summary.')).toBeInTheDocument();
    expect(screen.queryByText(/model|token|telemetry/i)).not.toBeInTheDocument();
  });
});
