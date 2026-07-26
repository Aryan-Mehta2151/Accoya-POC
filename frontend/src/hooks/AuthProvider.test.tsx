// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { AuthProvider } from './AuthProvider';
import { useAuth } from './useAuth';
import { api, clearApiAuthState } from '../lib/api';

const base = 'http://localhost:8000/api';
const futureExpiry = '2099-01-01T00:00:00Z';
const server = setupServer();

class FakeBroadcastChannel {
  static instances: FakeBroadcastChannel[] = [];
  onmessage: ((event: MessageEvent<unknown>) => void) | null = null;
  readonly name: string;
  readonly postMessage = vi.fn();
  readonly close = vi.fn();

  constructor(name: string) {
    this.name = name;
    FakeBroadcastChannel.instances.push(this);
  }

  receive(data: unknown) {
    this.onmessage?.({ data } as MessageEvent<unknown>);
  }
}

function Probe() {
  const auth = useAuth();
  const signOut = () => {
    void auth.logout().catch(() => undefined);
  };
  const signIn = () => {
    void auth.login('bob@example.com', 'correct horse battery').catch(() => undefined);
  };
  const resetPassword = () => {
    void auth.resetPassword('r'.repeat(32), 'a new secure password').catch(() => undefined);
  };

  return (
    <div>
      <span data-testid='status'>{auth.status}</span>
      <span data-testid='email'>{auth.user?.email ?? 'none'}</span>
      <button type='button' onClick={() => void auth.retryVerification()}>Retry</button>
      <button type='button' onClick={signOut}>Logout</button>
      <button type='button' onClick={signIn}>Login as Bob</button>
      <button type='button' onClick={resetPassword}>Reset password</button>
    </div>
  );
}

function renderProvider(queryClient = new QueryClient()) {
  render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <Probe />
      </AuthProvider>
    </QueryClientProvider>,
  );
  return queryClient;
}

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
beforeEach(() => {
  clearApiAuthState();
  FakeBroadcastChannel.instances = [];
  window.localStorage.clear();
  server.use(
    http.get(`${base}/auth/csrf`, () => HttpResponse.json({ csrf_token: 'csrf-boot' })),
    http.get(`${base}/auth/me`, () => HttpResponse.json({
      id: 'user-1',
      email: 'approved@example.com',
      name: 'Approved User',
      session_expires_at: futureExpiry,
    })),
  );
});
afterEach(() => {
  cleanup();
  server.resetHandlers();
  clearApiAuthState();
  vi.unstubAllGlobals();
});
afterAll(() => server.close());

describe('AuthProvider', () => {
  it('boots from the cookie session and removes legacy auth storage', async () => {
    window.localStorage.setItem('access_token', 'legacy-token');
    window.localStorage.setItem('user', '{"email":"stale@example.com"}');
    const calls: string[] = [];
    server.use(
      http.get(`${base}/auth/csrf`, () => {
        calls.push('csrf');
        return HttpResponse.json({ csrf_token: 'csrf-boot' });
      }),
      http.get(`${base}/auth/me`, () => {
        calls.push('me');
        return HttpResponse.json({
          id: 'user-1',
          email: 'approved@example.com',
          name: 'Approved User',
          session_expires_at: futureExpiry,
        });
      }),
    );

    renderProvider();

    expect(screen.getByTestId('status')).toHaveTextContent('checking');
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('authenticated'));
    expect(screen.getByTestId('email')).toHaveTextContent('approved@example.com');
    expect(calls).toEqual(['csrf', 'me']);
    expect(window.localStorage.getItem('access_token')).toBeNull();
    expect(window.localStorage.getItem('user')).toBeNull();
  });

  it('treats a rejected session as anonymous', async () => {
    server.use(
      http.get(`${base}/auth/me`, () => HttpResponse.json(
        { detail: { code: 'authentication_required', message: 'Sign in is required.' } },
        { status: 401 },
      )),
    );

    renderProvider();

    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('anonymous'));
    expect(screen.getByTestId('email')).toHaveTextContent('none');
  });

  it('shows a verification outage and can retry without treating it as logout', async () => {
    server.use(
      http.get(`${base}/auth/me`, () => HttpResponse.json({ detail: 'Unavailable' }, { status: 503 })),
    );
    renderProvider();

    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('verification_error'));

    server.use(
      http.get(`${base}/auth/me`, () => HttpResponse.json({
        id: 'user-1',
        email: 'approved@example.com',
        name: 'Approved User',
        session_expires_at: futureExpiry,
      })),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));

    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('authenticated'));
  });

  it('retains the authenticated UI and cache when logout fails', async () => {
    let attempts = 0;
    server.use(
      http.post(`${base}/auth/logout`, () => {
        attempts += 1;
        return HttpResponse.json({ detail: 'Unavailable' }, { status: 503 });
      }),
    );
    const queryClient = new QueryClient();
    queryClient.setQueryData(['private-data'], { secret: true });
    renderProvider(queryClient);
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('authenticated'));

    fireEvent.click(screen.getByRole('button', { name: 'Logout' }));

    await waitFor(() => expect(attempts).toBe(1));
    expect(screen.getByTestId('status')).toHaveTextContent('authenticated');
    expect(queryClient.getQueryData(['private-data'])).toEqual({ secret: true });
  });

  it('clears private cache and becomes anonymous after logout', async () => {
    let csrfHeader: string | null = null;
    server.use(
      http.post(`${base}/auth/logout`, ({ request }) => {
        csrfHeader = request.headers.get('X-CSRF-Token');
        return HttpResponse.json({ message: 'Signed out.' });
      }),
    );
    const queryClient = new QueryClient();
    queryClient.setQueryData(['private-data'], { secret: true });
    renderProvider(queryClient);
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('authenticated'));

    fireEvent.click(screen.getByRole('button', { name: 'Logout' }));

    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('anonymous'));
    expect(csrfHeader).toBe('csrf-boot');
    expect(queryClient.getQueryData(['private-data'])).toBeUndefined();
  });

  it('treats a 401 from logout as an already-completed local sign-out', async () => {
    server.use(
      http.post(`${base}/auth/logout`, () => HttpResponse.json(
        { detail: { code: 'authentication_required', message: 'Sign in is required.' } },
        { status: 401 },
      )),
    );
    const queryClient = new QueryClient();
    queryClient.setQueryData(['private-data'], { secret: true });
    renderProvider(queryClient);
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('authenticated'));

    fireEvent.click(screen.getByRole('button', { name: 'Logout' }));

    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('anonymous'));
    expect(queryClient.getQueryData(['private-data'])).toBeUndefined();
  });

  it('clears private state at the server-provided session expiry', async () => {
    const expiresAt = new Date(Date.now() + 250).toISOString();
    server.use(
      http.get(`${base}/auth/me`, () => HttpResponse.json({
        id: 'user-1',
        email: 'approved@example.com',
        name: 'Approved User',
        session_expires_at: expiresAt,
      })),
    );
    const queryClient = new QueryClient();
    queryClient.setQueryData(['private-data'], { secret: true });
    renderProvider(queryClient);
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('authenticated'));

    await waitFor(
      () => expect(screen.getByTestId('status')).toHaveTextContent('anonymous'),
      { timeout: 1500 },
    );
    expect(queryClient.getQueryData(['private-data'])).toBeUndefined();
  });

  it('keeps authenticated content mounted while focus revalidation is pending', async () => {
    renderProvider();
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('authenticated'));

    let releaseMe!: () => void;
    let markMeStarted!: () => void;
    const meStarted = new Promise<void>((resolve) => {
      markMeStarted = resolve;
    });
    const meGate = new Promise<void>((resolve) => {
      releaseMe = resolve;
    });
    server.use(
      http.get(`${base}/auth/me`, async () => {
        markMeStarted();
        await meGate;
        return HttpResponse.json(
          { detail: { code: 'authentication_required', message: 'Sign in is required.' } },
          { status: 401 },
        );
      }),
    );

    fireEvent.focus(window);
    await meStarted;
    expect(screen.getByTestId('status')).toHaveTextContent('authenticated');
    expect(screen.getByTestId('email')).toHaveTextContent('approved@example.com');

    releaseMe();
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('anonymous'));
  });

  it('broadcasts password login so peer tabs re-check the shared cookie', async () => {
    vi.stubGlobal('BroadcastChannel', FakeBroadcastChannel);
    server.use(
      http.post(`${base}/auth/login`, () => HttpResponse.json({
        user: {
          id: 'user-2',
          email: 'bob@example.com',
          name: 'Bob',
          session_expires_at: futureExpiry,
        },
        csrf_token: 'csrf-bob',
      })),
    );
    renderProvider();
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('authenticated'));

    fireEvent.click(screen.getByRole('button', { name: 'Login as Bob' }));

    await waitFor(() => expect(screen.getByTestId('email')).toHaveTextContent('bob@example.com'));
    expect(FakeBroadcastChannel.instances[0].postMessage).toHaveBeenCalledWith({
      type: 'session-changed',
    });
  });

  it('broadcasts and re-verifies when a concurrent login supersedes a peer event', async () => {
    vi.stubGlobal('BroadcastChannel', FakeBroadcastChannel);
    let currentEmail = 'approved@example.com';
    let meCalls = 0;
    let releaseLogin!: () => void;
    let markLoginStarted!: () => void;
    const loginStarted = new Promise<void>((resolve) => {
      markLoginStarted = resolve;
    });
    const loginGate = new Promise<void>((resolve) => {
      releaseLogin = resolve;
    });
    server.use(
      http.get(`${base}/auth/me`, () => {
        meCalls += 1;
        return HttpResponse.json({
          id: currentEmail === 'bob@example.com' ? 'user-2' : 'user-1',
          email: currentEmail,
          name: currentEmail === 'bob@example.com' ? 'Bob' : 'Approved User',
          session_expires_at: futureExpiry,
        });
      }),
      http.post(`${base}/auth/login`, async () => {
        markLoginStarted();
        await loginGate;
        return HttpResponse.json({
          user: {
            id: 'user-2',
            email: 'bob@example.com',
            name: 'Bob',
            session_expires_at: futureExpiry,
          },
          csrf_token: 'csrf-bob',
        });
      }),
    );
    renderProvider();
    await waitFor(() => expect(meCalls).toBe(1));
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('authenticated'));

    fireEvent.click(screen.getByRole('button', { name: 'Login as Bob' }));
    await loginStarted;
    FakeBroadcastChannel.instances[0].receive({ type: 'session-changed' });
    await waitFor(() => expect(meCalls).toBe(2));

    currentEmail = 'bob@example.com';
    releaseLogin();

    await waitFor(() => expect(screen.getByTestId('email')).toHaveTextContent('bob@example.com'));
    expect(meCalls).toBe(3);
    expect(FakeBroadcastChannel.instances[0].postMessage).toHaveBeenCalledWith({
      type: 'session-changed',
    });
  });

  it('clears this tab and broadcasts sign-out after a password reset', async () => {
    vi.stubGlobal('BroadcastChannel', FakeBroadcastChannel);
    server.use(
      http.post(`${base}/auth/reset-password`, () => HttpResponse.json({
        message: 'Password reset successfully.',
      })),
    );
    const queryClient = new QueryClient();
    queryClient.setQueryData(['private-data'], { secret: true });
    renderProvider(queryClient);
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('authenticated'));

    fireEvent.click(screen.getByRole('button', { name: 'Reset password' }));

    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('anonymous'));
    expect(queryClient.getQueryData(['private-data'])).toBeUndefined();
    expect(FakeBroadcastChannel.instances[0].postMessage).toHaveBeenCalledWith({
      type: 'signed-out',
    });
  });

  it('expires the local session when a business request returns 401', async () => {
    server.use(
      http.get(`${base}/leads`, () => HttpResponse.json(
        { detail: { code: 'authentication_required', message: 'Sign in is required.' } },
        { status: 401 },
      )),
    );
    const queryClient = new QueryClient();
    queryClient.setQueryData(['private-data'], { secret: true });
    renderProvider(queryClient);
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('authenticated'));

    await api.listLeads().catch(() => undefined);

    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('anonymous'));
    expect(queryClient.getQueryData(['private-data'])).toBeUndefined();
  });

  it('does not let a stale session check restore a cleared login', async () => {
    let releaseMe!: () => void;
    let markMeStarted!: () => void;
    const meStarted = new Promise<void>((resolve) => {
      markMeStarted = resolve;
    });
    const meGate = new Promise<void>((resolve) => {
      releaseMe = resolve;
    });
    server.use(
      http.get(`${base}/auth/me`, async () => {
        markMeStarted();
        await meGate;
        return HttpResponse.json({
          id: 'user-1',
          email: 'approved@example.com',
          name: 'Approved User',
          session_expires_at: futureExpiry,
        });
      }),
      http.get(`${base}/leads`, () => HttpResponse.json(
        { detail: { code: 'authentication_required', message: 'Sign in is required.' } },
        { status: 401 },
      )),
    );

    renderProvider();
    await meStarted;
    await api.listLeads().catch(() => undefined);
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('anonymous'));

    releaseMe();
    await new Promise((resolve) => window.setTimeout(resolve, 0));
    expect(screen.getByTestId('status')).toHaveTextContent('anonymous');
    expect(screen.getByTestId('email')).toHaveTextContent('none');
  });

  it('clears private state when another tab broadcasts logout', async () => {
    vi.stubGlobal('BroadcastChannel', FakeBroadcastChannel);
    const queryClient = new QueryClient();
    queryClient.setQueryData(['private-data'], { secret: true });
    renderProvider(queryClient);
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('authenticated'));

    expect(FakeBroadcastChannel.instances).toHaveLength(1);
    expect(FakeBroadcastChannel.instances[0].name).toBe('accoya-auth');
    server.use(
      http.get(`${base}/auth/me`, () => HttpResponse.json(
        { detail: { code: 'authentication_required', message: 'Sign in is required.' } },
        { status: 401 },
      )),
    );
    FakeBroadcastChannel.instances[0].receive({ type: 'signed-out' });

    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('anonymous'));
    expect(queryClient.getQueryData(['private-data'])).toBeUndefined();
  });

  it('verifies the cookie instead of trusting a stale cross-tab logout', async () => {
    vi.stubGlobal('BroadcastChannel', FakeBroadcastChannel);
    const queryClient = new QueryClient();
    queryClient.setQueryData(['private-data'], { secret: true });
    renderProvider(queryClient);
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('authenticated'));

    FakeBroadcastChannel.instances[0].receive({ type: 'signed-out' });

    expect(queryClient.getQueryData(['private-data'])).toBeUndefined();
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('authenticated'));
    expect(screen.getByTestId('email')).toHaveTextContent('approved@example.com');
  });

  it('clears stale identity data and verifies a cross-tab account change', async () => {
    vi.stubGlobal('BroadcastChannel', FakeBroadcastChannel);
    const queryClient = new QueryClient();
    queryClient.setQueryData(['private-data'], { owner: 'alice' });
    renderProvider(queryClient);
    await waitFor(() => expect(screen.getByTestId('email')).toHaveTextContent('approved@example.com'));

    server.use(
      http.get(`${base}/auth/me`, () => HttpResponse.json({
        id: 'user-2',
        email: 'bob@example.com',
        name: 'Bob',
        session_expires_at: futureExpiry,
      })),
    );
    FakeBroadcastChannel.instances[0].receive({ type: 'session-changed' });

    await waitFor(() => expect(screen.getByTestId('email')).toHaveTextContent('bob@example.com'));
    expect(screen.getByTestId('status')).toHaveTextContent('authenticated');
    expect(queryClient.getQueryData(['private-data'])).toBeUndefined();
  });
});
