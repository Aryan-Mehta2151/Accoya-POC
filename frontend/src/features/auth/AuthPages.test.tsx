// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AuthContextValue } from '../../hooks/authContext';
import { useAuth } from '../../hooks/useAuth';
import { CallbackPage } from './CallbackPage';
import { LoginPage } from './LoginPage';
import { ResetPasswordPage } from './ResetPasswordPage';

vi.mock('../../hooks/useAuth', () => ({ useAuth: vi.fn() }));

const auth: AuthContextValue = {
  user: null,
  status: 'anonymous',
  loading: false,
  login: vi.fn(),
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
  it('offers approved-account login without signup and preserves a protected deep link', async () => {
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

    expect(screen.queryByText('Sign up')).not.toBeInTheDocument();
    expect(screen.getByText('Accounts are managed by your Accoya administrator.')).toBeVisible();
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
      <MemoryRouter initialEntries={['/auth/callback?error=oauth_failed']}>
        <CallbackPage />
      </MemoryRouter>,
    );

    expect(screen.getByText('Google sign-in failed')).toBeVisible();
    expect(auth.announceSessionChanged).not.toHaveBeenCalled();
  });
});
