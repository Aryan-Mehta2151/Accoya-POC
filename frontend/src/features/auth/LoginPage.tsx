import { useState } from 'react';
import { Eye, EyeOff } from 'lucide-react';
import { Navigate, useLocation, useNavigate } from 'react-router-dom';
import { useAuth } from '../../hooks/useAuth';
import styles from './auth.module.css';

function GoogleLogo() {
  return (
    <svg aria-hidden='true' viewBox='0 0 24 24'>
      <path fill='#4285F4' d='M21.6 12.23c0-.71-.06-1.4-.18-2.07H12v3.91h5.38a4.6 4.6 0 0 1-2 3.02v2.54h3.24c1.9-1.75 2.98-4.32 2.98-7.4Z' />
      <path fill='#34A853' d='M12 22c2.7 0 4.97-.9 6.62-2.43l-3.24-2.54c-.9.6-2.05.96-3.38.96-2.61 0-4.82-1.76-5.61-4.13H3.04v2.62A10 10 0 0 0 12 22Z' />
      <path fill='#FBBC05' d='M6.39 13.86A6.02 6.02 0 0 1 6.07 12c0-.65.11-1.28.32-1.86V7.52H3.04A10 10 0 0 0 2 12c0 1.61.38 3.14 1.04 4.48l3.35-2.62Z' />
      <path fill='#EA4335' d='M12 6.01c1.47 0 2.79.51 3.83 1.5l2.87-2.88A9.64 9.64 0 0 0 12 2a10 10 0 0 0-8.96 5.52l3.35 2.62C7.18 7.77 9.39 6.01 12 6.01Z' />
    </svg>
  );
}

function returnPath(state: unknown): string {
  if (
    state &&
    typeof state === 'object' &&
    'from' in state &&
    typeof state.from === 'string' &&
    state.from.startsWith('/') &&
    !state.from.startsWith('//')
  ) {
    return state.from;
  }
  return '/';
}

export function LoginPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const { login, startGoogleLogin, status } = useAuth();
  const destination = returnPath(location.state);
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  if (status === 'authenticated') {
    return <Navigate to={destination} replace />;
  }

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError('');
    setLoading(true);
    try {
      await login(email, password);
      navigate(destination, { replace: true });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Sign in failed.');
    } finally {
      setLoading(false);
    }
  };

  const handleGoogleLogin = () => {
    try {
      window.sessionStorage.setItem('accoya-auth-return-to', destination);
    } catch {
      // The OAuth flow still works when browser storage is unavailable.
    }
    startGoogleLogin();
  };

  return (
    <div className={styles.container}>
      <div className={styles.card}>
        <div className={styles.header}>
          <h1>Sign In</h1>
          <p>Welcome back</p>
        </div>

        {error && <div className={styles.error}>{error}</div>}

        <form onSubmit={handleSubmit} className={styles.form}>
          <div className={styles.formGroup}>
            <label htmlFor='email'>Email Address</label>
            <input
              id='email'
              type='email'
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              placeholder='you@example.com'
              autoComplete='email'
              required
            />
          </div>

          <div className={styles.formGroup}>
            <label htmlFor='password'>Password</label>
            <div className={styles.passwordField}>
              <input
                id='password'
                type={showPassword ? 'text' : 'password'}
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                placeholder='Enter your password'
                autoComplete='current-password'
                required
              />
              <button
                type='button'
                className={styles.passwordToggle}
                onClick={() => setShowPassword((visible) => !visible)}
                aria-label={showPassword ? 'Hide password' : 'Show password'}
                title={showPassword ? 'Hide password' : 'Show password'}
              >
                {showPassword ? <EyeOff aria-hidden='true' /> : <Eye aria-hidden='true' />}
              </button>
            </div>
            <a href='/forgot-password' className={styles.forgotPassword}>
              Forgot password?
            </a>
          </div>

          <button type='submit' className={styles.submitButton} disabled={loading}>
            {loading ? 'Signing in...' : 'Sign In'}
          </button>
        </form>

        <div className={styles.divider}>
          <span>Or continue with</span>
        </div>

        <button
          type='button'
          className={styles.googleButton}
          onClick={handleGoogleLogin}
          disabled={loading}
        >
          <GoogleLogo />
          Continue with Google
        </button>

        <div className={styles.toggle}>
          <p>Accounts are managed by your Accoya administrator.</p>
        </div>
      </div>
    </div>
  );
}
