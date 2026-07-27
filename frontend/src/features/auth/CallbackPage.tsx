import { useEffect, useRef } from 'react';
import { Link, Navigate, useSearchParams } from 'react-router-dom';
import { useAuth } from '../../hooks/useAuth';
import styles from './auth.module.css';

function callbackDestination(): string {
  try {
    const stored = window.sessionStorage.getItem('accoya-auth-return-to');
    if (stored?.startsWith('/') && !stored.startsWith('//')) return stored;
  } catch {
    // Fall back to the application home when browser storage is unavailable.
  }
  return '/';
}

export function CallbackPage() {
  const [searchParams] = useSearchParams();
  const { announceSessionChanged, status, retryVerification } = useAuth();
  const announcedRef = useRef(false);
  const oauthError = searchParams.get('error');
  const oauthFailureMessage =
    oauthError === 'access_not_approved'
      ? 'This Google account does not have access yet. Please ask your admin to approve your account, then try again.'
      : oauthError === 'access_denied'
        ? 'Google sign-in was cancelled before completion.'
        : 'Google sign-in could not be completed. Please try again.';

  useEffect(() => {
    if (!oauthError && status === 'authenticated' && !announcedRef.current) {
      announcedRef.current = true;
      announceSessionChanged();
    }
  }, [announceSessionChanged, oauthError, status]);

  if (oauthError) {
    return (
      <main className={styles.callbackContainer}>
        <div className={styles.callbackCard}>
          <h1>Google sign-in failed</h1>
          <p>{oauthFailureMessage}</p>
          <Link to='/login' className={styles.callbackActionButton}>
            Go to sign in
          </Link>
        </div>
      </main>
    );
  }

  if (status === 'authenticated') {
    return <Navigate to={callbackDestination()} replace />;
  }

  if (status === 'verification_error') {
    return (
      <main className={styles.callbackContainer}>
        <div className={styles.callbackCard}>
          <h1>We could not verify your sign-in</h1>
          <p>Check your connection and retry the secure session check.</p>
          <button type='button' className='primaryButton' onClick={() => void retryVerification()}>
            Retry
          </button>
        </div>
      </main>
    );
  }

  if (status === 'anonymous') {
    return (
      <main className={styles.callbackContainer}>
        <div className={styles.callbackCard}>
          <h1>Sign-in was not completed</h1>
          <p>Your session is not active. Please try signing in again.</p>
          <Link to='/login' className={styles.callbackActionButton}>
            Go to sign in
          </Link>
        </div>
      </main>
    );
  }

  return (
    <main className={styles.callbackContainer}>
      <p>Verifying your secure session...</p>
    </main>
  );
}
