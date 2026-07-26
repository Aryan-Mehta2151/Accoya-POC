import { useLayoutEffect, useState } from 'react';
import { Eye, EyeOff } from 'lucide-react';
import { useLocation, useNavigate } from 'react-router-dom';
import { useAuth } from '../../hooks/useAuth';
import styles from './auth.module.css';

export function ResetPasswordPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const { resetPassword } = useAuth();
  const [token] = useState(() => (
    new URLSearchParams(location.hash.replace(/^#/, '')).get('token')
  ));
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [showConfirmPassword, setShowConfirmPassword] = useState(false);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState(false);
  const [loading, setLoading] = useState(false);

  useLayoutEffect(() => {
    if (!token || !location.hash) return;
    // Keep the one-time secret only in component memory. Removing it before
    // paint prevents later same-origin requests and copied history entries from
    // retaining the reset URL.
    window.history.replaceState(
      window.history.state,
      '',
      location.pathname,
    );
  }, [location.hash, location.pathname, token]);

  if (!token) {
    return (
      <div className={styles.container}>
        <div className={styles.card}>
          <div className={styles.header}>
            <h1>Invalid Link</h1>
            <p>The password reset link is invalid or expired</p>
          </div>
          <button
            onClick={() => navigate('/login')}
            className={styles.submitButton}
          >
            Back to Sign in
          </button>
        </div>
      </div>
    );
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');

    if (password !== confirmPassword) {
      setError('Passwords do not match');
      return;
    }

    if (password.length < 12) {
      setError('Password must be at least 12 characters');
      return;
    }

    if (new TextEncoder().encode(password).length > 72) {
      setError('Password must be no more than 72 UTF-8 bytes');
      return;
    }

    setLoading(true);

    try {
      await resetPassword(token, password);
      setSuccess(true);
      setTimeout(() => navigate('/login'), 2000);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'An error occurred');
    } finally {
      setLoading(false);
    }
  };

  if (success) {
    return (
      <div className={styles.container}>
        <div className={styles.card}>
          <div className={styles.header}>
            <h1>Password Reset</h1>
            <p>Your password has been successfully reset</p>
          </div>
          <p style={{ textAlign: 'center', color: '#6b7280', fontSize: '14px' }}>
            Redirecting to login in 2 seconds...
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className={styles.container}>
      <div className={styles.card}>
        <div className={styles.header}>
          <h1>Set New Password</h1>
          <p>Create a new password for your account</p>
        </div>

        {error && <div className={styles.error}>{error}</div>}

        <form onSubmit={handleSubmit} className={styles.form}>
          <div className={styles.formGroup}>
            <label htmlFor="password">New Password</label>
            <div className={styles.passwordField}>
              <input
                id='password'
                type={showPassword ? 'text' : 'password'}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder='Create a secure password'
                required
                autoFocus
                minLength={12}
                maxLength={72}
              />
              <button
                type='button'
                className={styles.passwordToggle}
                onClick={() => setShowPassword((visible) => !visible)}
                aria-label={showPassword ? 'Hide new password' : 'Show new password'}
                title={showPassword ? 'Hide password' : 'Show password'}
              >
                {showPassword ? <EyeOff aria-hidden='true' /> : <Eye aria-hidden='true' />}
              </button>
            </div>
            <span className={styles.fieldHint}>
              12 characters minimum; 72 UTF-8 bytes maximum
            </span>
          </div>

          <div className={styles.formGroup}>
            <label htmlFor="confirmPassword">Confirm Password</label>
            <div className={styles.passwordField}>
              <input
                id='confirmPassword'
                type={showConfirmPassword ? 'text' : 'password'}
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                placeholder='Re-enter your new password'
                required
                minLength={12}
                maxLength={72}
              />
              <button
                type='button'
                className={styles.passwordToggle}
                onClick={() => setShowConfirmPassword((visible) => !visible)}
                aria-label={showConfirmPassword ? 'Hide confirmed password' : 'Show confirmed password'}
                title={showConfirmPassword ? 'Hide password' : 'Show password'}
              >
                {showConfirmPassword ? <EyeOff aria-hidden='true' /> : <Eye aria-hidden='true' />}
              </button>
            </div>
          </div>

          <button
            type="submit"
            className={styles.submitButton}
            disabled={loading}
          >
            {loading ? 'Resetting...' : 'Reset Password'}
          </button>
        </form>

        <div className={styles.toggle}>
          <p>
            <button
              type="button"
              onClick={() => navigate('/login')}
              className={styles.toggleButton}
            >
              Back to Sign in
            </button>
          </p>
        </div>
      </div>
    </div>
  );
}
