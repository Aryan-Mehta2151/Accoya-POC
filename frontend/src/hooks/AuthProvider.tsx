import { useQueryClient } from '@tanstack/react-query';
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import {
  ApiError,
  authApi,
  clearApiAuthState,
  subscribeToUnauthorized,
  type AuthUser,
} from '../lib/api';
import { AuthContext, type AuthContextValue, type AuthStatus } from './authContext';

const AUTH_CHANNEL = 'accoya-auth';

function removeLegacyAuthStorage(): void {
  try {
    window.localStorage.removeItem('access_token');
    window.localStorage.removeItem('user');
  } catch {
    // Storage may be unavailable in hardened browser modes. Cookie auth still works.
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [user, setUser] = useState<AuthUser | null>(null);
  const [status, setStatus] = useState<AuthStatus>('checking');
  const channelRef = useRef<BroadcastChannel | null>(null);
  const sessionEpochRef = useRef(0);
  const userIdRef = useRef<string | null>(null);

  const clearSession = useCallback((broadcast: boolean) => {
    sessionEpochRef.current += 1;
    clearApiAuthState();
    queryClient.clear();
    userIdRef.current = null;
    setUser(null);
    setStatus('anonymous');
    if (broadcast) channelRef.current?.postMessage({ type: 'signed-out' });
  }, [queryClient]);

  const announceSessionChanged = useCallback(() => {
    channelRef.current?.postMessage({ type: 'session-changed' });
  }, []);

  const verifySession = useCallback(async () => {
    const operationEpoch = ++sessionEpochRef.current;
    setStatus('checking');
    try {
      await authApi.prepareCsrf(true);
      const currentUser = await authApi.getCurrentUser();
      if (sessionEpochRef.current !== operationEpoch) return;
      if (userIdRef.current && userIdRef.current !== currentUser.id) {
        clearApiAuthState();
        queryClient.clear();
      }
      userIdRef.current = currentUser.id;
      setUser(currentUser);
      setStatus('authenticated');
    } catch (error) {
      if (sessionEpochRef.current !== operationEpoch) return;
      userIdRef.current = null;
      setUser(null);
      if (error instanceof ApiError && error.status === 401) {
        clearApiAuthState();
        setStatus('anonymous');
        return;
      }
      setStatus('verification_error');
    }
  }, [queryClient]);

  const revalidateSession = useCallback(async () => {
    const operationEpoch = ++sessionEpochRef.current;
    try {
      await authApi.prepareCsrf(true);
      const currentUser = await authApi.getCurrentUser();
      if (sessionEpochRef.current !== operationEpoch) return;
      if (userIdRef.current && userIdRef.current !== currentUser.id) {
        clearApiAuthState();
        queryClient.clear();
      }
      userIdRef.current = currentUser.id;
      setUser(currentUser);
    } catch (error) {
      if (sessionEpochRef.current !== operationEpoch) return;
      if (error instanceof ApiError && error.status === 401) {
        clearSession(true);
      }
      // A background connectivity/server failure leaves the verified UI
      // mounted. The next focus or explicit API request will check again.
    }
  }, [clearSession, queryClient]);

  const refreshChangedSession = useCallback(() => {
    sessionEpochRef.current += 1;
    clearApiAuthState();
    queryClient.clear();
    userIdRef.current = null;
    setUser(null);
    void verifySession();
  }, [queryClient, verifySession]);

  useEffect(() => {
    removeLegacyAuthStorage();
    const timer = window.setTimeout(() => void verifySession(), 0);
    return () => {
      window.clearTimeout(timer);
      sessionEpochRef.current += 1;
    };
  }, [verifySession]);

  useEffect(() => {
    if (typeof BroadcastChannel === 'undefined') return;
    const channel = new BroadcastChannel(AUTH_CHANNEL);
    channelRef.current = channel;
    channel.onmessage = (event: MessageEvent<unknown>) => {
      if (
        event.data &&
        typeof event.data === 'object' &&
        'type' in event.data &&
        typeof event.data.type === 'string'
      ) {
        if (
          event.data.type === 'signed-out'
          || event.data.type === 'session-changed'
        ) {
          // Broadcast delivery can be reordered across tabs. Clear private UI
          // immediately, then let the browser-global cookie decide whether the
          // latest external event was a login or a logout.
          refreshChangedSession();
        }
      }
    };
    return () => {
      channel.close();
      if (channelRef.current === channel) channelRef.current = null;
    };
  }, [clearSession, refreshChangedSession]);

  useEffect(
    () => subscribeToUnauthorized(() => clearSession(true)),
    [clearSession],
  );

  useEffect(() => {
    if (status !== 'authenticated' || !user) return;
    const expiresAt = Date.parse(user.session_expires_at);
    const remaining = expiresAt - Date.now();
    const boundedDelay = Number.isFinite(remaining)
      ? Math.min(Math.max(0, remaining), 2_147_483_647)
      : 0;
    const timer = window.setTimeout(
      () => clearSession(true),
      boundedDelay,
    );
    return () => window.clearTimeout(timer);
  }, [clearSession, status, user]);

  useEffect(() => {
    if (status !== 'authenticated') return;
    const revalidate = () => void revalidateSession();
    const revalidateVisible = () => {
      if (document.visibilityState === 'visible') revalidate();
    };
    window.addEventListener('focus', revalidate);
    document.addEventListener('visibilitychange', revalidateVisible);
    return () => {
      window.removeEventListener('focus', revalidate);
      document.removeEventListener('visibilitychange', revalidateVisible);
    };
  }, [revalidateSession, status]);

  const login = useCallback(async (email: string, password: string) => {
    const operationEpoch = ++sessionEpochRef.current;
    const currentUser = await authApi.login(email, password);
    // The successful response has already changed the browser-global cookie,
    // even if another tab changed session state while this request was in
    // flight. Peers must always re-check after that external fact.
    announceSessionChanged();
    if (sessionEpochRef.current !== operationEpoch) {
      clearApiAuthState();
      await verifySession();
      return;
    }
    queryClient.clear();
    userIdRef.current = currentUser.id;
    setUser(currentUser);
    setStatus('authenticated');
  }, [announceSessionChanged, queryClient, verifySession]);

  const logout = useCallback(async () => {
    await authApi.logout();
    clearSession(true);
  }, [clearSession]);

  const forgotPassword = useCallback(async (email: string) => {
    await authApi.forgotPassword(email);
  }, []);

  const resetPassword = useCallback(async (token: string, password: string) => {
    await authApi.resetPassword(token, password);
    clearSession(true);
  }, [clearSession]);

  const startGoogleLogin = useCallback(() => {
    window.location.assign(authApi.googleStartUrl());
  }, []);

  const value = useMemo<AuthContextValue>(() => ({
    user,
    status,
    loading: status === 'checking',
    login,
    logout,
    forgotPassword,
    resetPassword,
    retryVerification: verifySession,
    announceSessionChanged,
    startGoogleLogin,
  }), [
    forgotPassword,
    announceSessionChanged,
    login,
    logout,
    resetPassword,
    startGoogleLogin,
    status,
    user,
    verifySession,
  ]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
