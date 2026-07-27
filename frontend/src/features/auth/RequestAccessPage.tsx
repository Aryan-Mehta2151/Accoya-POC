import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useAuth } from '../../hooks/useAuth';
import styles from './auth.module.css';

export function RequestAccessPage() {
  const { requestAccess } = useAuth();
  const [email, setEmail] = useState('');
  const [name, setName] = useState('');
  const [error, setError] = useState('');
  const [successMessage, setSuccessMessage] = useState('');
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError('');
    setSuccessMessage('');
    setLoading(true);

    try {
      const message = await requestAccess(email, name);
      setSuccessMessage(message || 'Your access request has been sent for review.');
      setEmail('');
      setName('');
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Access request failed.');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className={styles.container}>
      <div className={styles.card}>
        <div className={styles.header}>
          <h1>Request Access</h1>
          <p>Ask your administrator to approve your sign-in account.</p>
        </div>

        {error && <div className={styles.error}>{error}</div>}
        {successMessage && <div className={styles.success}>{successMessage}</div>}

        <form onSubmit={handleSubmit} className={styles.form}>
          <div className={styles.formGroup}>
            <label htmlFor='request-email'>Work Email Address</label>
            <input
              id='request-email'
              type='email'
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              placeholder='you@example.com'
              autoComplete='email'
              required
            />
          </div>

          <div className={styles.formGroup}>
            <label htmlFor='request-name'>Name (optional)</label>
            <input
              id='request-name'
              type='text'
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder='Your full name'
              autoComplete='name'
              maxLength={255}
            />
          </div>

          <button type='submit' className={styles.submitButton} disabled={loading}>
            {loading ? 'Submitting...' : 'Request Access'}
          </button>
        </form>

        <div className={styles.toggle}>
          <p>
            Already approved?
            <Link to='/login' className={styles.toggleButton}>
              Sign in
            </Link>
          </p>
        </div>
      </div>
    </div>
  );
}
