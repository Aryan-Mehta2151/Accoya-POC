import { useQuery } from '@tanstack/react-query';
import {
  ArrowRight,
  BookOpenText,
  CircleCheck,
  MailCheck,
  Target,
} from 'lucide-react';
import { Link } from 'react-router-dom';
import { ErrorState, LoadingState, PageHeader, StatusBadge } from '../../components/ui';
import { api } from '../../lib/api';
import { formatDate, formatLocation, formatScore } from '../../lib/format';
import { queryKeys } from '../../lib/queryKeys';
import type { Email } from '../../types';
import styles from './overview.module.css';

function newestEmailsByLead(emails: Email[]): Email[] {
  const newest = new Map<string, Email>();
  for (const email of emails) {
    const current = newest.get(email.lead_id);
    if (!current) {
      newest.set(email.lead_id, email);
      continue;
    }
    const createdComparison = email.created_at.localeCompare(current.created_at);
    if (createdComparison > 0 || (createdComparison === 0 && email.id.localeCompare(current.id) > 0)) {
      newest.set(email.lead_id, email);
    }
  }
  return [...newest.values()].sort((a, b) => b.updated_at.localeCompare(a.updated_at));
}

export function OverviewPage() {
  const leadsQuery = useQuery({ queryKey: queryKeys.leads, queryFn: api.listLeads });
  const emailsQuery = useQuery({ queryKey: queryKeys.emails, queryFn: api.listEmails });
  const documentsQuery = useQuery({ queryKey: queryKeys.documents, queryFn: api.listDocuments });

  const isInitialLoading = leadsQuery.isLoading && emailsQuery.isLoading && documentsQuery.isLoading;
  const allFailed = leadsQuery.isError && emailsQuery.isError && documentsQuery.isError;

  if (isInitialLoading) return <LoadingState label='Preparing your workspace…' />;
  if (allFailed) {
    return (
      <ErrorState
        title='Your workspace is unavailable'
        message='We could not reach the service. Check that the backend is running, then try again.'
        onRetry={() => void Promise.all([
          leadsQuery.refetch(),
          emailsQuery.refetch(),
          documentsQuery.refetch(),
        ])}
      />
    );
  }

  const leads = leadsQuery.data ?? [];
  const emails = emailsQuery.data ?? [];
  const documents = documentsQuery.data ?? [];
  const currentEmails = newestEmailsByLead(emails);
  const pending = currentEmails.filter((email) => email.status === 'pending_review');
  const sent = currentEmails.filter((email) => email.status === 'sent');
  const topLeads = leads.slice(0, 5);
  const recentEmails = currentEmails.slice(0, 5);

  return (
    <div>
      <PageHeader
        eyebrow='Sales workspace'
        title='Make every opportunity count.'
        description='Prioritize the right projects, shape considered outreach, and keep each message moving with confidence.'
        actions={
          <Link className='button buttonPrimary' to='/opportunities'>
            Explore opportunities <ArrowRight aria-hidden='true' />
          </Link>
        }
      />

      {(leadsQuery.isError || emailsQuery.isError || documentsQuery.isError) && (
        <div className={styles.partialNotice} role='status'>
          Some live totals are temporarily unavailable. The rest of your workspace is ready.
        </div>
      )}

      <section className={styles.metrics} aria-label='Workspace summary'>
        <Metric icon={<Target />} label='Opportunities' value={leadsQuery.isError ? '—' : leads.length} tone='forest' to='/opportunities' />
        <Metric icon={<MailCheck />} label='Needs review' value={emailsQuery.isError ? '—' : pending.length} tone='clay' to='/opportunities?outreach=pending_review' />
        <Metric icon={<CircleCheck />} label='Sent' value={emailsQuery.isError ? '—' : sent.length} tone='timber' to='/opportunities?outreach=sent' />
        <Metric icon={<BookOpenText />} label='Strategy docs' value={documentsQuery.isError ? '—' : documents.length} tone='sage' to='/knowledge' />
      </section>

      <div className={styles.grid}>
        <section className={styles.panel}>
          <div className={styles.panelHeader}>
            <div>
              <p>Where to focus</p>
              <h2>Top opportunities</h2>
            </div>
            <Link to='/opportunities'>View all <ArrowRight aria-hidden='true' /></Link>
          </div>
          {topLeads.length > 0 ? (
            <div className={styles.leadList}>
              {topLeads.map((lead, index) => (
                <Link className={styles.leadRow} to={`/opportunities/${lead.id}`} key={lead.id}>
                  <span className={styles.rank}>{String(index + 1).padStart(2, '0')}</span>
                  <span className={styles.leadCopy}>
                    <strong>{lead.project ?? 'Untitled opportunity'}</strong>
                    <span>{formatLocation(lead.location, lead.state)}</span>
                  </span>
                  <span className={styles.score}>
                    <small>Score</small>
                    {formatScore(lead.score)}
                  </span>
                </Link>
              ))}
            </div>
          ) : (
            <PanelEmpty text='Sync EarlyBid or upload a CSV to see prioritized opportunities.' action='/opportunities' />
          )}
        </section>

        <section className={styles.panel}>
          <div className={styles.panelHeader}>
            <div>
              <p>Latest work</p>
              <h2>Recent outreach</h2>
            </div>
            <Link to='/opportunities?outreach=pending_review'>Review queue <ArrowRight aria-hidden='true' /></Link>
          </div>
          {recentEmails.length > 0 ? (
            <div className={styles.emailList}>
              {recentEmails.map((email) => {
                const lead = leads.find((item) => item.id === email.lead_id);
                return (
                  <Link
                    className={styles.emailRow}
                    to={`/opportunities/${encodeURIComponent(email.lead_id)}?email=${encodeURIComponent(email.id)}`}
                    key={email.id}
                  >
                    <span className={styles.emailCopy}>
                      <strong>{email.subject}</strong>
                      <span>{lead?.project ?? 'Opportunity'} · {formatDate(email.updated_at)}</span>
                    </span>
                    <StatusBadge status={email.status} />
                  </Link>
                );
              })}
            </div>
          ) : (
            <PanelEmpty text='Generated emails will appear here, ready for a thoughtful review.' action='/opportunities' />
          )}
        </section>
      </div>
    </div>
  );
}

function Metric({ icon, label, value, tone, to }: { icon: React.ReactNode; label: string; value: number | string; tone: string; to: string }) {
  return (
    <Link className={`${styles.metric} ${styles[tone]}`} to={to} aria-label={`${label}: ${String(value)}`}>
      <span className={styles.metricIcon} aria-hidden='true'>{icon}</span>
      <strong>{value}</strong>
      <span>{label}</span>
    </Link>
  );
}

function PanelEmpty({ text, action }: { text: string; action: string }) {
  return (
    <div className={styles.panelEmpty}>
      <p>{text}</p>
      <Link className='button buttonSecondary' to={action}>Get started</Link>
    </div>
  );
}
