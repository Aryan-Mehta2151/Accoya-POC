import { ArrowLeft } from 'lucide-react';
import { Link } from 'react-router-dom';
import { EmptyState } from '../components/ui';

export function NotFoundPage() {
  return (
    <EmptyState
      title='This page could not be found'
      description='The link may be out of date, or the item may no longer be available.'
      action={
        <Link className='button buttonPrimary' to='/'>
          <ArrowLeft aria-hidden='true' /> Return to overview
        </Link>
      }
    />
  );
}
