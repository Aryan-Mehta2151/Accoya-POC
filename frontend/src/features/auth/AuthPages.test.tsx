// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AuthContextValue } from '../../hooks/authContext';
import { useAuth } from '../../hooks/useAuth';
import { CallbackPage } from './CallbackPage';
import { LoginPage } from './LoginPage';
import { RequestAccessPage } from './RequestAccessPage';
import { ResetPasswordPage } from './ResetPasswordPage';

vi.mock('../../hooks/useAuth', () => ({ useAuth: vi.fn() }));

const auth: AuthContextValue = {
  user: null,
  status: 'anonymous',
  loading: false,
  login: vi.fn(),
  requestAccess: vi.fn(),
  logout: vi.fn(),
  forgotPassword: vi.fn(),
  resetPassword: vi.fn(),
  retryVerification: vi.fn(),
  announceSessionChanged: vi.fn(),
  startGoogleLogin: vi.fn(),
};

beforeEach(() => {
  vi.mocked(useAuth).mockReturnValue(auth);
  window.localStorage.clear();
  window.sessionStorage.clear();
  vi.clearAllMocks();
});
afterEach(cleanup);

describe('authentication pages', () => {
  it('offers approved-account login and preserves a protected deep link', async () => {
    render(
      <MemoryRouter initialEntries={[{
        pathname: '/login',
        state: { from: '/opportunities/lead-1?review=true' },
      }]}>
        <Routes>
          <Route path='/login' element={<LoginPage />} />
          <Route path='/opportunities/:leadId' element={<p>Opportunity workspace</p>} />
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByRole('link', { name: 'Request access' })).toBeVisible();
    fireEvent.change(screen.getByLabelText('Email Address'), {
      target: { value: 'approved@example.com' },
    });
    fireEvent.change(screen.getByLabelText('Password'), {
      target: { value: 'correct horse battery staple' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Sign In' }));

    await waitFor(() => expect(auth.login).toHaveBeenCalledWith(
      'approved@example.com',
      'correct horse battery staple',
    ));
    expect(await screen.findByText('Opportunity workspace')).toBeVisible();
  });

  it('starts Google login through the backend and stores only a return path', () => {
    render(
      <MemoryRouter initialEntries={['/login']}>
        <LoginPage />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Continue with Google' }));

    expect(auth.startGoogleLogin).toHaveBeenCalledTimes(1);
    expect(window.sessionStorage.getItem('accoya-auth-return-to')).toBe('/');
    expect(window.localStorage.getItem('access_token')).toBeNull();
  });

  it('submits a request-access form and shows backend success message', async () => {
    vi.mocked(auth.requestAccess).mockResolvedValueOnce(
      'If access can be granted, the request will be reviewed shortly.',
    );
    render(
      <MemoryRouter initialEntries={['/request-access']}>
        <RequestAccessPage />
      </MemoryRouter>,
    );

    fireEvent.change(screen.getByLabelText('Work Email Address'), {
      target: { value: 'new.user@example.com' },
    });
    fireEvent.change(screen.getByLabelText('Name (optional)'), {
      target: { value: 'New User' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Request Access' }));

    await waitFor(() => expect(auth.requestAccess).toHaveBeenCalledWith(
      'new.user@example.com',
      'New User',
    ));
    expect(
      await screen.findByText('If access can be granted, the request will be reviewed shortly.'),
    ).toBeVisible();
  });

  it('hydrates an OAuth cookie session without reading a token from the URL', () => {
    window.sessionStorage.setItem('accoya-auth-return-to', '/knowledge');
    vi.mocked(useAuth).mockReturnValue({
      ...auth,
      status: 'authenticated',
      user: {
        id: 'user-1',
        email: 'approved@example.com',
        name: null,
        session_expires_at: '2099-01-01T00:00:00Z',
      },
    });
    render(
      <MemoryRouter initialEntries={['/auth/callback?token=must-not-be-used']}>
        <Routes>
          <Route path='/auth/callback' element={<CallbackPage />} />
          <Route path='/knowledge' element={<p>Knowledge base</p>} />
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByText('Knowledge base')).toBeVisible();
    expect(window.localStorage.getItem('access_token')).toBeNull();
    expect(auth.announceSessionChanged).toHaveBeenCalledTimes(1);
  });

  it('enforces the 12-character reset password minimum', () => {
    const replaceState = vi.spyOn(window.history, 'replaceState');
    render(
      <MemoryRouter initialEntries={['/reset-password#token=reset-token']}>
        <ResetPasswordPage />
      </MemoryRouter>,
    );

    fireEvent.change(screen.getByLabelText('New Password'), {
      target: { value: 'short-pass1' },
    });
    fireEvent.change(screen.getByLabelText('Confirm Password'), {
      target: { value: 'short-pass1' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Reset Password' }));

    expect(screen.getByText('Password must be at least 12 characters')).toBeVisible();
    expect(auth.resetPassword).not.toHaveBeenCalled();
    expect(replaceState).toHaveBeenCalledWith(
      window.history.state,
      '',
      '/reset-password',
    );
  });

  it('shows an OAuth failure without announcing an account change', () => {
    vi.mocked(useAuth).mockReturnValue({
      ...auth,
      status: 'authenticated',
      user: {
        id: 'user-1',
        email: 'approved@example.com',
        name: null,
        session_expires_at: '2099-01-01T00:00:00Z',
      },
    });
    render(
      <MemoryRouter initialEntries={['/auth/callback?error=access_not_approved']}>
        <CallbackPage />
      </MemoryRouter>,
    );

    expect(screen.getByText('Google sign-in failed')).toBeVisible();
    expect(
      screen.getByText(
        'This Google account does not have access yet. Please ask your admin to approve your account, then try again.',
      ),
    ).toBeVisible();
    expect(screen.getByRole('link', { name: 'Go to sign in' })).toBeVisible();
    expect(auth.announceSessionChanged).not.toHaveBeenCalled();
  });
});
