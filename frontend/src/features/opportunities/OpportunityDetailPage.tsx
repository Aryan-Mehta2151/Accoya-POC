import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowLeft, CalendarDays, ExternalLink, Mail, MapPin, Pencil, Save, X } from 'lucide-react';
import type { ReactNode } from 'react';
import { Link, useParams } from 'react-router-dom';
import { toast } from 'sonner';

import { EmptyState, ErrorState, LoadingState, PageHeader } from '../../components/ui';
import { api, ApiError } from '../../lib/api';
import { queryKeys } from '../../lib/queryKeys';
import type { Lead, LeadWorkspace } from '../../types';
import { EmailWorkspace } from './EmailWorkspace';
import styles from './opportunities.module.css';

function workIsActive(workspace: {
  latest_generation?: { status: string } | null;
  emails?: Array<{ latest_delivery?: { status: string } | null }>;
} | undefined) {
  const generationStatus = workspace?.latest_generation?.status;
  if (generationStatus === 'queued' || generationStatus === 'running') return true;
  return workspace?.emails?.some((email) => (
    email.latest_delivery?.status === 'queued' || email.latest_delivery?.status === 'running'
  )) ?? false;
}

const scoreFormatter = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
const dateFormatter = new Intl.DateTimeFormat(undefined, {
  month: 'short',
  day: 'numeric',
  year: 'numeric',
});
const dateTimeFormatter = new Intl.DateTimeFormat(undefined, {
  month: 'short',
  day: 'numeric',
  year: 'numeric',
  hour: 'numeric',
  minute: '2-digit',
});

function formatDate(value: string | null, includeTime = false) {
  if (!value) return 'Not provided';
  const dateOnly = /^([0-9]{4})-([0-9]{2})-([0-9]{2})$/.exec(value);
  const parsed = dateOnly
    ? new Date(Number(dateOnly[1]), Number(dateOnly[2]) - 1, Number(dateOnly[3]))
    : new Date(value);
  if (Number.isNaN(parsed.valueOf())) return value;
  return includeTime ? dateTimeFormatter.format(parsed) : dateFormatter.format(parsed);
}

function textValue(value: string | null | undefined) {
  return value?.trim() || 'Not provided';
}

function safeExternalUrl(value: string | null) {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === 'http:' || url.protocol === 'https:' ? url.toString() : null;
  } catch {
    return null;
  }
}

function DetailItem({ label, children, wide = false }: { label: string; children: ReactNode; wide?: boolean }) {
  return (
    <div className={wide ? styles.detailItemWide : styles.detailItem}>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function Tags({ value }: { value: string | null }) {
  const tags = value?.split(',').map((tag) => tag.trim()).filter(Boolean) ?? [];
  if (!tags.length) return <>Not provided</>;
  return (
    <span className={styles.tagList}>
      {tags.map((tag) => <span className={styles.tag} key={tag}>{tag}</span>)}
    </span>
  );
}

function ContactCard({ lead }: { lead: Lead }) {
  const queryClient = useQueryClient();
  const [isEditing, setIsEditing] = useState(false);
  const [contacts, setContacts] = useState(lead.contacts ?? '');
  const [contactEmail, setContactEmail] = useState(lead.contact_email ?? '');

  const startEditing = () => {
    setContacts(lead.contacts ?? '');
    setContactEmail(lead.contact_email ?? '');
    setIsEditing(true);
  };

  const saveMutation = useMutation({
    mutationFn: () => api.updateLeadContact(lead.id, {
      contacts,
      contact_email: contactEmail,
    }),
    onSuccess: (updatedLead) => {
      queryClient.setQueryData<LeadWorkspace>(queryKeys.leadWorkspace(updatedLead.id), (current) => (
        current ? { ...current, lead: updatedLead } : current
      ));
      queryClient.setQueryData<Lead[]>(queryKeys.leads, (current) => (
        current?.map((item) => (item.id === updatedLead.id ? { ...item, ...updatedLead } : item))
      ));
      setIsEditing(false);
      toast.success('Contact information saved');
    },
    onError: (error) => {
      const message = error instanceof ApiError || error instanceof Error
        ? error.message
        : 'Please try again in a moment.';
      toast.error('Could not save contact information', { description: message });
    },
  });

  const cancelEditing = () => {
    setContacts(lead.contacts ?? '');
    setContactEmail(lead.contact_email ?? '');
    setIsEditing(false);
  };

  return (
    <section className={styles.sideCard} aria-labelledby='contact-heading'>
      <div className={styles.sideCardHeader}>
        <div>
          <p className={styles.sectionEyebrow}>Primary contact</p>
          <h2 id='contact-heading'>{isEditing ? 'Edit contact' : lead.contacts || 'Contact not provided'}</h2>
        </div>
        {!isEditing && (
          <button className={styles.iconButton} type='button' onClick={startEditing} aria-label='Edit contact information'>
            <Pencil aria-hidden='true' size={15} />
          </button>
        )}
      </div>

      {isEditing ? (
        <form
          className={styles.contactForm}
          onSubmit={(event) => {
            event.preventDefault();
            saveMutation.mutate();
          }}
        >
          <label>
            <span>Contact name</span>
            <input value={contacts} onChange={(event) => setContacts(event.target.value)} autoComplete='name' />
          </label>
          <label>
            <span>Email address</span>
            <input type='email' value={contactEmail} onChange={(event) => setContactEmail(event.target.value)} autoComplete='email' />
          </label>
          <div className={styles.contactFormActions}>
            <button className={styles.primaryButton} type='submit' disabled={saveMutation.isPending}>
              <Save aria-hidden='true' size={15} /> {saveMutation.isPending ? 'Saving...' : 'Save contact'}
            </button>
            <button className={styles.secondaryButton} type='button' onClick={cancelEditing} disabled={saveMutation.isPending}>
              <X aria-hidden='true' size={15} /> Cancel
            </button>
          </div>
        </form>
      ) : (
        <>
          {lead.contact_email ? (
            <a className={styles.contactLink} href={`mailto:${lead.contact_email}`}>
              <Mail aria-hidden='true' size={16} /> {lead.contact_email}
            </a>
          ) : <p className={styles.muted}>No email address is available.</p>}
          <dl className={styles.compactDetails}>
            <div><dt>Contact name</dt><dd>{textValue(lead.contacts)}</dd></div>
            <div><dt>Contact email</dt><dd>{textValue(lead.contact_email)}</dd></div>
          </dl>
        </>
      )}
    </section>
  );
}

export function OpportunityDetailPage() {
  const { leadId } = useParams<{ leadId: string }>();
  const workspaceQuery = useQuery({
    queryKey: queryKeys.leadWorkspace(leadId ?? ''),
    queryFn: () => api.getLeadWorkspace(leadId!),
    enabled: Boolean(leadId),
    refetchInterval: (query) => workIsActive(query.state.data) ? 2000 : false,
    retry: (count, error) => !(error instanceof ApiError && error.status === 404) && count < 2,
  });

  if (workspaceQuery.isPending) {
    return <div className={styles.page}><LoadingState label='Loading opportunity...' /></div>;
  }

  if (workspaceQuery.isError) {
    if (workspaceQuery.error instanceof ApiError && workspaceQuery.error.status === 404) {
      return (
        <div className={styles.page}>
          <EmptyState
            title='Opportunity not found'
            description='It may have been removed or the link may be out of date.'
            action={<Link className={styles.primaryButton} to='/opportunities'>Back to opportunities</Link>}
          />
        </div>
      );
    }
    return (
      <div className={styles.page}>
        <ErrorState
          title='Opportunity could not be loaded'
          message={workspaceQuery.error.message || 'Check the backend connection and try again.'}
          onRetry={() => void workspaceQuery.refetch()}
        />
      </div>
    );
  }

  const workspace = workspaceQuery.data;
  const lead = workspace.lead;
  const sourceUrl = safeExternalUrl(lead.url);

  return (
    <div className={styles.page}>
      <Link className={styles.backLink} to='/opportunities'>
        <ArrowLeft aria-hidden='true' size={16} /> All opportunities
      </Link>

      <PageHeader
        eyebrow={lead.section || 'Opportunity intelligence'}
        title={lead.project || 'Untitled opportunity'}
        description={lead.summary || 'Review the available context and outreach for this opportunity.'}
      />

      <div className={styles.detailHighlights}>
        <div>
          <MapPin aria-hidden='true' size={18} />
          <span>{[lead.location, lead.state].filter(Boolean).join(', ') || 'Location not provided'}</span>
        </div>
        <div><CalendarDays aria-hidden='true' size={18} /><span>{textValue(lead.timing)}</span></div>
        <div>
          <span className={styles.highlightScore}>{lead.score === null ? '—' : scoreFormatter.format(lead.score)}</span>
          <span>Priority score</span>
        </div>
      </div>

      <div className={styles.detailLayout}>
        <main className={styles.detailMain}>
          <section className={styles.detailSection} aria-labelledby='opportunity-overview'>
            <div className={styles.sectionHeading}>
              <div>
                <p className={styles.sectionEyebrow}>Project profile</p>
                <h2 id='opportunity-overview'>Opportunity overview</h2>
              </div>
            </div>
            <dl className={styles.detailGrid}>
              <DetailItem label='Project'>{textValue(lead.project)}</DetailItem>
              <DetailItem label='Section'>{textValue(lead.section)}</DetailItem>
              <DetailItem label='Location'>{textValue(lead.location)}</DetailItem>
              <DetailItem label='State'>{textValue(lead.state)}</DetailItem>
              <DetailItem label='Timing'>{textValue(lead.timing)}</DetailItem>
              <DetailItem label='Meeting date'>{formatDate(lead.meeting_date)}</DetailItem>
              <DetailItem label='Awarded to'>{textValue(lead.awarded_to)}</DetailItem>
              <DetailItem label='Score'>{lead.score === null ? 'Not provided' : scoreFormatter.format(lead.score)}</DetailItem>
              <DetailItem label='Summary' wide>{textValue(lead.summary)}</DetailItem>
            </dl>
          </section>

          <section className={styles.detailSection} aria-labelledby='opportunity-intelligence'>
            <div className={styles.sectionHeading}>
              <div>
                <p className={styles.sectionEyebrow}>Signals</p>
                <h2 id='opportunity-intelligence'>Opportunity intelligence</h2>
              </div>
            </div>
            <dl className={styles.detailGrid}>
              <DetailItem label='Signal' wide>{textValue(lead.signal)}</DetailItem>
              <DetailItem label='Intelligence' wide>{textValue(lead.intelligence)}</DetailItem>
              <DetailItem label='Priority reasons' wide>{textValue(lead.priority_reasons)}</DetailItem>
              <DetailItem label='Tags' wide><Tags value={lead.tags} /></DetailItem>
            </dl>
          </section>

          <section className={styles.detailSection} aria-labelledby='opportunity-source'>
            <div className={styles.sectionHeading}>
              <div>
                <p className={styles.sectionEyebrow}>Record</p>
                <h2 id='opportunity-source'>Source information</h2>
              </div>
            </div>
            <dl className={styles.detailGrid}>
              <DetailItem label='External ID'>{textValue(lead.external_id)}</DetailItem>
              <DetailItem label='Record ID'><span className={styles.monoValue}>{lead.id}</span></DetailItem>
              <DetailItem label='Source feed'>{textValue(lead.source_feed)}</DetailItem>
              <DetailItem label='Created'>{formatDate(lead.created_at, true)}</DetailItem>
              <DetailItem label='Source URL' wide>
                {sourceUrl ? (
                  <a className={styles.inlineLink} href={sourceUrl} target='_blank' rel='noreferrer'>
                    Open original opportunity <ExternalLink aria-hidden='true' size={14} />
                  </a>
                ) : textValue(lead.url)}
              </DetailItem>
            </dl>
          </section>
        </main>

        <aside className={styles.detailSidebar} aria-label='Opportunity contact'>
          <ContactCard lead={lead} />
        </aside>
      </div>

      <EmailWorkspace key={lead.id} workspace={workspace} />
    </div>
  );
}
