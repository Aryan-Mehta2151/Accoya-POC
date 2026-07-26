// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen } from '@testing-library/react';
import { createMemoryRouter, RouterProvider, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { api } from '../../lib/api';
import type { Email } from '../../types';
import { OutreachDetailPage } from './OutreachDetailPage';
import { OutreachPage } from './OutreachPage';

vi.mock('../../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../lib/api')>();
  return { ...actual, api: { getEmail: vi.fn() } };
});

const email: Email = {
  id: 'email-1',
  lead_id: 'lead-1',
  recipient_email: 'maya@example.com',
  subject: 'Accoya proposal',
  body: 'Hello Maya',
  status: 'pending_review',
  latest_delivery: null,
  has_unknown_delivery: false,
  delivery_content_hash: 'b'.repeat(64),
  created_at: '2026-07-20T11:00:00Z',
  updated_at: '2026-07-21T11:00:00Z',
};

function Destination() {
  const location = useLocation();
  return <p>Destination: {location.pathname}{location.search}</p>;
}

function renderRoute(path: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createMemoryRouter([
    { path: '/outreach', element: <OutreachPage /> },
    { path: '/outreach/:emailId', element: <OutreachDetailPage /> },
    { path: '/opportunities', element: <Destination /> },
    { path: '/opportunities/:leadId', element: <Destination /> },
  ], { initialEntries: [path] });
  return render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.getEmail).mockResolvedValue(email);
});

afterEach(() => cleanup());

describe('legacy outreach routes', () => {
  it('redirects the outreach queue to the opportunity review filter', async () => {
    renderRoute('/outreach');
    expect(await screen.findByText('Destination: /opportunities?outreach=pending_review')).toBeInTheDocument();
  });

  it('resolves an old email URL to its canonical opportunity workspace URL', async () => {
    renderRoute('/outreach/email-1');
    expect(await screen.findByText('Destination: /opportunities/lead-1?email=email-1')).toBeInTheDocument();
    expect(api.getEmail).toHaveBeenCalledWith('email-1');
  });
});
