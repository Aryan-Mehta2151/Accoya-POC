import { useQuery } from '@tanstack/react-query';
import {
  ArrowRight,
  BookOpenText,
  CircleCheck,
  MailCheck,
  MessageCircleReply,
  Target,
} from 'lucide-react';
import { useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';
import { ErrorState, LoadingState, PageHeader, StatusBadge } from '../../components/ui';
import { api } from '../../lib/api';
import { formatDate, formatLocation, formatScore } from '../../lib/format';
import { queryKeys } from '../../lib/queryKeys';
import type { Email } from '../../types';
import styles from './overview.module.css';

type AppliedRange = {
  startAt: string;
  endBefore: string;
};

function localDate(value: string, nextDay = false): Date {
  const [year, month, day] = value.split('-').map(Number);
  return new Date(year, month - 1, day + (nextDay ? 1 : 0));
}

function apiRange(startDate: string, endDate: string): AppliedRange {
  return {
    startAt: localDate(startDate).toISOString(),
    endBefore: localDate(endDate, true).toISOString(),
  };
}

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
  const [startDate, setStartDate] = useState('');
  const [endDate, setEndDate] = useState('');
  const [rangeError, setRangeError] = useState('');
  const [appliedRange, setAppliedRange] = useState<AppliedRange | null>(null);
  const leadsQuery = useQuery({
    queryKey: queryKeys.leads,
    queryFn: () => api.listLeads(),
    refetchInterval: 60_000,
  });
  const emailsQuery = useQuery({ queryKey: queryKeys.emails, queryFn: api.listEmails });
  const summaryQuery = useQuery({
    queryKey: queryKeys.overviewSummary(
      appliedRange?.startAt,
      appliedRange?.endBefore,
    ),
    queryFn: () => api.getOverviewSummary(
      appliedRange
        ? {
            start_at: appliedRange.startAt,
            end_before: appliedRange.endBefore,
          }
        : undefined,
    ),
    refetchInterval: 30_000,
    refetchOnWindowFocus: true,
  });
  const documentsQuery = useQuery({ queryKey: queryKeys.documents, queryFn: api.listDocuments });

  const isInitialLoading = leadsQuery.isLoading && emailsQuery.isLoading && summaryQuery.isLoading && documentsQuery.isLoading;
  const allFailed = leadsQuery.isError && emailsQuery.isError && summaryQuery.isError && documentsQuery.isError;

  if (isInitialLoading) return <LoadingState label='Preparing your workspace…' />;
  if (allFailed) {
    return (
      <ErrorState
        title='Your workspace is unavailable'
        message='We could not reach the service. Check that the backend is running, then try again.'
        onRetry={() => void Promise.all([
          leadsQuery.refetch(),
          emailsQuery.refetch(),
          summaryQuery.refetch(),
          documentsQuery.refetch(),
        ])}
      />
    );
  }

  const leads = leadsQuery.data ?? [];
  const emails = emailsQuery.data ?? [];
  const summary = summaryQuery.data;
  const documents = documentsQuery.data ?? [];
  const replyTotalsCurrent = !summaryQuery.isError && summary?.reply_sync_status === 'healthy';
  const activeLeadIds = new Set(leads.map((lead) => lead.id));
  const currentEmails = newestEmailsByLead(emails).filter((email) => activeLeadIds.has(email.lead_id));
  const topLeads = leads.slice(0, 5);
  const recentEmails = currentEmails.slice(0, 5);

  const applyRange = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!startDate || !endDate) {
      setRangeError('Enter both a start date and an end date.');
      return;
    }
    if (startDate > endDate) {
      setRangeError('Start date must be on or before end date.');
      return;
    }
    setRangeError('');
    setAppliedRange(apiRange(startDate, endDate));
  };

  const clearRange = () => {
    setStartDate('');
    setEndDate('');
    setRangeError('');
    setAppliedRange(null);
  };

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

      {(leadsQuery.isError || emailsQuery.isError || summaryQuery.isError || documentsQuery.isError) && (
        <div className={styles.partialNotice} role='status'>
          Some live totals are temporarily unavailable. The rest of your workspace is ready.
        </div>
      )}

      <form className={styles.dateFilter} aria-label='Overview date range' onSubmit={applyRange}>
        <div className={styles.dateFilterCopy}>
          <strong>Reporting period</strong>
          <span>{appliedRange ? 'Showing the selected date range' : 'Showing all time'}</span>
        </div>
        <label>
          <span>Start date</span>
          <input
            type='date'
            value={startDate}
            aria-invalid={Boolean(rangeError)}
            onChange={(event) => setStartDate(event.target.value)}
          />
        </label>
        <label>
          <span>End date</span>
          <input
            type='date'
            value={endDate}
            aria-invalid={Boolean(rangeError)}
            onChange={(event) => setEndDate(event.target.value)}
          />
        </label>
        <div className={styles.dateFilterActions}>
          <button className='button buttonPrimary' type='submit'>Apply</button>
          <button
            className='button buttonGhost'
            type='button'
            disabled={!startDate && !endDate && !appliedRange}
            onClick={clearRange}
          >
            Clear
          </button>
        </div>
        {rangeError ? <p className={styles.dateError} role='alert'>{rangeError}</p> : null}
      </form>

      <section className={styles.metrics} aria-label='Workspace summary'>
        <Metric icon={<Target />} label='Opportunities' value={summaryQuery.isError ? '—' : (summary?.opportunities ?? '—')} tone='forest' />
        <Metric icon={<MailCheck />} label='Needs review' value={summaryQuery.isError ? '—' : (summary?.needs_review ?? '—')} tone='clay' />
        <Metric icon={<CircleCheck />} label='Sent' value={summaryQuery.isError ? '—' : (summary?.sent ?? '—')} tone='timber' />
        {summary?.reply_sync_status !== 'disabled' ? (
          <Metric
            icon={<MessageCircleReply />}
            label='Replies'
            value={replyTotalsCurrent ? summary.replies : '—'}
            tone='mist'
          />
        ) : null}
        <Metric icon={<BookOpenText />} label='Strategy docs' value={documentsQuery.isError ? '—' : documents.length} tone='sage' to='/knowledge' />
      </section>

      {(summaryQuery.isError || (summary && summary.reply_sync_status !== 'healthy' && summary.reply_sync_status !== 'disabled')) ? (
        <div className={styles.replyNotice} role='status'>
          Reply totals are {!summaryQuery.isError && summary?.reply_sync_status === 'initializing' ? 'being prepared' : 'temporarily unavailable'}; cached data is not shown as current.
        </div>
      ) : null}

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

function Metric({ icon, label, value, tone, to }: { icon: React.ReactNode; label: string; value: number | string; tone: string; to?: string }) {
  const content = (
    <>
      <span className={styles.metricIcon} aria-hidden='true'>{icon}</span>
      <strong>{value}</strong>
      <span>{label}</span>
    </>
  );
  const className = `${styles.metric} ${styles[tone]} ${to ? styles.metricLink : ''}`;
  return to ? (
    <Link className={className} to={to} aria-label={`${label}: ${String(value)}`}>
      {content}
    </Link>
  ) : (
    <article className={className} aria-label={`${label}: ${String(value)}`}>
      {content}
    </article>
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
