import { createContext } from 'react';
import type { AuthUser } from '../lib/api';

export type AuthStatus =
  | 'checking'
  | 'authenticated'
  | 'anonymous'
  | 'verification_error';

export interface AuthContextValue {
  user: AuthUser | null;
  status: AuthStatus;
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
  requestAccess: (email: string, name?: string) => Promise<string>;
  logout: () => Promise<void>;
  forgotPassword: (email: string) => Promise<void>;
  resetPassword: (token: string, password: string) => Promise<void>;
  retryVerification: () => Promise<void>;
  announceSessionChanged: () => void;
  startGoogleLogin: () => void;
}

export const AuthContext = createContext<AuthContextValue | null>(null);
