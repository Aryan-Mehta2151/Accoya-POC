import { createBrowserRouter, RouterProvider, Navigate } from 'react-router-dom';
import { Toaster } from 'sonner';
import { AppShell } from './app/AppShell';
import { NotFoundPage } from './app/NotFoundPage';
import { AssistantPage } from './features/assistant';
import { KnowledgePage } from './features/knowledge';
import { OpportunitiesPage, OpportunityDetailPage } from './features/opportunities';
import { OutreachDetailPage, OutreachPage } from './features/outreach';
import { OverviewPage } from './features/overview/OverviewPage';
import { 
  CallbackPage, 
  ForgotPasswordPage, 
  LoginPage, 
  ResetPasswordPage 
} from './features/auth';
import { useAuth } from './hooks/useAuth';

// Protected route wrapper
function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();

  if (loading) {
    return <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', height: '100vh' }}>Loading...</div>;
  }

  if (!user) {
    return <Navigate to="/login" replace />;
  }

  return children;
}

const router = createBrowserRouter([
  {
    path: '/login',
    element: <LoginPage />,
  },
  {
    path: '/auth/callback',
    element: <CallbackPage />,
  },
  {
    path: '/forgot-password',
    element: <ForgotPasswordPage />,
  },
  {
    path: '/reset-password',
    element: <ResetPasswordPage />,
  },
  {
    element: (
      <ProtectedRoute>
        <AppShell />
      </ProtectedRoute>
    ),
    children: [
      { index: true, element: <OverviewPage /> },
      { path: 'opportunities', element: <OpportunitiesPage /> },
      { path: 'opportunities/:leadId', element: <OpportunityDetailPage /> },
      { path: 'outreach', element: <OutreachPage /> },
      { path: 'outreach/:emailId', element: <OutreachDetailPage /> },
      { path: 'knowledge', element: <KnowledgePage /> },
      { path: 'assistant', element: <AssistantPage /> },
      { path: '*', element: <NotFoundPage /> },
    ],
  },
]);

export default function App() {
  return (
    <>
      <RouterProvider router={router} />
      <Toaster position='top-right' closeButton duration={4200} />
    </>
  );
}
