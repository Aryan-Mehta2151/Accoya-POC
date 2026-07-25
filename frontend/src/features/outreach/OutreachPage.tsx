import { Navigate } from 'react-router-dom';

export function OutreachPage() {
  return <Navigate to='/opportunities?outreach=pending_review' replace />;
}
