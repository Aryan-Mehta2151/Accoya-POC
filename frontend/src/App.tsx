import { createBrowserRouter, RouterProvider } from 'react-router-dom';
import { Toaster } from 'sonner';
import { AppShell } from './app/AppShell';
import { NotFoundPage } from './app/NotFoundPage';
import { AssistantPage } from './features/assistant';
import { KnowledgePage } from './features/knowledge';
import { OpportunitiesPage, OpportunityDetailPage } from './features/opportunities';
import { OutreachDetailPage, OutreachPage } from './features/outreach';
import { OverviewPage } from './features/overview/OverviewPage';

const router = createBrowserRouter([
  {
    element: <AppShell />,
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
