import { useQuery } from '@tanstack/react-query';
import { Mail } from 'lucide-react';
import { Link, Navigate, useParams } from 'react-router-dom';

import { EmptyState, ErrorState, LoadingState } from '../../components/ui';
import { api, ApiError } from '../../lib/api';
import { queryKeys } from '../../lib/queryKeys';

export function OutreachDetailPage() {
  const { emailId } = useParams<{ emailId: string }>();
  const emailQuery = useQuery({
    queryKey: queryKeys.email(emailId ?? ''),
    queryFn: () => api.getEmail(emailId!),
    enabled: Boolean(emailId),
    retry: (count, error) => !(error instanceof ApiError && error.status === 404) && count < 2,
  });

  if (!emailId) return <Navigate to='/opportunities' replace />;
  if (emailQuery.isPending) return <LoadingState label='Opening opportunity...' />;

  if (emailQuery.isError) {
    if (emailQuery.error instanceof ApiError && emailQuery.error.status === 404) {
      return (
        <EmptyState
          icon={<Mail aria-hidden='true' />}
          title='Outreach not found'
          description='It may have been removed, or this older link may no longer be valid.'
          action={<Link className='button buttonSecondary' to='/opportunities'>View opportunities</Link>}
        />
      );
    }
    return (
      <ErrorState
        title='This outreach could not be opened'
        message={emailQuery.error.message || 'Please try again.'}
        onRetry={() => void emailQuery.refetch()}
      />
    );
  }

  const email = emailQuery.data;
  const destination = `/opportunities/${encodeURIComponent(email.lead_id)}?email=${encodeURIComponent(email.id)}`;
  return <Navigate to={destination} replace />;
}
