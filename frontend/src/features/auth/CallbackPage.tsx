import { useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000/api';

export function CallbackPage() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const [error, setError] = useState('');

  useEffect(() => {
    const token = searchParams.get('token');
    const oauthError = searchParams.get('error');

    if (oauthError) {
      setError(
        oauthError === 'access_denied'
          ? 'Google sign-in was cancelled.'
          : 'Google sign-in could not be completed.',
      );
      const redirectTimer = window.setTimeout(() => navigate('/login'), 2000);
      return () => window.clearTimeout(redirectTimer);
    }

    if (!token) {
      setError('No token received from authentication');
      const redirectTimer = window.setTimeout(() => navigate('/login'), 2000);
      return () => window.clearTimeout(redirectTimer);
    }

    // Store token and redirect
    try {
      localStorage.setItem('access_token', token);
      
      // Fetch user info from /api/auth/me endpoint
      const fetchUserInfo = async () => {
        const res = await fetch(`${API_BASE_URL}/auth/me`, {
          headers: { 'Authorization': `Bearer ${token}` },
        });
        
        if (res.ok) {
          const user = await res.json();
          localStorage.setItem('user', JSON.stringify(user));
          navigate('/');
        } else {
          // Still redirect even if we can't fetch user info
          navigate('/');
        }
      };
      
      fetchUserInfo();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to process authentication');
      setTimeout(() => navigate('/login'), 2000);
    }
  }, [searchParams, navigate]);

  return (
    <div style={{ 
      display: 'flex', 
      justifyContent: 'center', 
      alignItems: 'center', 
      minHeight: '100vh',
      flexDirection: 'column',
      gap: '20px'
    }}>
      {error ? (
        <div style={{ textAlign: 'center' }}>
          <p style={{ color: '#dc2626' }}>Error: {error}</p>
          <p style={{ color: '#6b7280' }}>Redirecting to login...</p>
        </div>
      ) : (
        <div style={{ textAlign: 'center' }}>
          <p>Signing you in...</p>
          <div style={{ 
            marginTop: '20px',
            width: '40px',
            height: '40px',
            border: '4px solid #e5e7eb',
            borderTop: '4px solid #3b82f6',
            borderRadius: '50%',
            animation: 'spin 1s linear infinite',
          }} />
        </div>
      )}
      <style>{`
        @keyframes spin {
          to { transform: rotate(360deg); }
        }
      `}</style>
    </div>
  );
}
