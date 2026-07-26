import { useCallback, useEffect, useRef, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  Clock3,
  History,
  LoaderCircle,
  Mail,
  RotateCcw,
  Save,
  Send,
  Sparkles,
  UserRound,
  X,
} from 'lucide-react';
import { useForm } from 'react-hook-form';
import { Link, useBeforeUnload, useBlocker, useSearchParams } from 'react-router-dom';
import { toast } from 'sonner';

import { ConfirmDialog, StatusBadge } from '../../components/ui';
import { api, ApiError } from '../../lib/api';
import { normalizeEmailBody } from '../../lib/emailText';
import { queryKeys } from '../../lib/queryKeys';
import type {
  Email,
  EmailDeliveryJob,
  EmailDeliveryJobStatus,
  EmailGenerationJob,
  EmailGenerationJobStatus,
  EmailStatus,
  Lead,
  LeadWorkspace,
} from '../../types';
import outreachStyles from '../outreach/Outreach.module.css';
import styles from './opportunities.module.css';

type EmailForm = {
  recipient_email: string;
  subject: string;
  body: string;
};

const dateTimeFormatter = new Intl.DateTimeFormat(undefined, {
  month: 'short',
  day: 'numeric',
  year: 'numeric',
  hour: 'numeric',
  minute: '2-digit',
});

function formatDateTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? 'Date unavailable' : dateTimeFormatter.format(date);
}

function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError || error instanceof Error) return error.message;
  return fallback;
}

function isGenerationActive(job: EmailGenerationJob | null | undefined): boolean {
  return job?.status === 'queued' || job?.status === 'running';
}

function isGenerationFailure(status: EmailGenerationJobStatus): boolean {
  return status === 'insufficient_context' || status === 'provider_error' || status === 'system_error';
}

function isDeliveryActive(job: EmailDeliveryJob | null | undefined): boolean {
  return job?.status === 'queued' || job?.status === 'running';
}

function isValidRecipient(value: string | null | undefined): boolean {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value?.trim() ?? '');
}

function hasDeliverableContent(email: Email): boolean {
  return isValidRecipient(email.recipient_email)
    && email.subject.trim().length > 0
    && email.body.trim().length > 0;
}

function makeIdempotencyKey(): string {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (character) => {
    const random = Math.floor(Math.random() * 16);
    const value = character === 'x' ? random : (random & 0x3) | 0x8;
    return value.toString(16);
  });
}

function updateEmailList(emails: Email[], updated: Email): Email[] {
  return emails.map((email) => (email.id === updated.id ? updated : email));
}

function transitionCopy(status: EmailStatus): { title: string; description: string } {
  switch (status) {
    case 'draft':
      return { title: 'Ready for another set of eyes?', description: 'Submit this draft for review.' };
    case 'pending_review':
      return { title: 'Make the final call', description: 'Approve a polished message or reject it.' };
    case 'approved':
      return { title: 'Approved and ready', description: 'Send this email when you are ready to contact the recipient.' };
    case 'sent':
      return { title: 'Email sent', description: 'The SMTP relay accepted this message for delivery.' };
    case 'rejected':
      return { title: 'Closed without sending', description: 'This message was rejected and is read-only.' };
  }
}

function deliveryMessage(status: EmailDeliveryJobStatus): { title: string; description: string } {
  switch (status) {
    case 'queued':
      return { title: 'Email queued for delivery', description: 'The delivery worker will send it shortly.' };
    case 'running':
      return { title: 'Sending email', description: 'The delivery worker is contacting the SMTP relay.' };
    case 'succeeded':
      return { title: 'Email sent', description: 'The SMTP relay accepted the message for delivery.' };
    case 'failed':
      return { title: 'Email could not be sent', description: 'The message was not accepted. Review the details and try again.' };
    case 'delivery_unknown':
      return {
        title: 'Delivery status is uncertain',
        description: 'The relay may have accepted this message. Sending again could create a duplicate.',
      };
  }
}

function generationMessage(job: EmailGenerationJob): { title: string; description: string } {
  if (job.status === 'queued') {
    return { title: 'Email queued', description: 'The drafting worker will start this email shortly.' };
  }
  if (job.status === 'running') {
    return { title: 'Generating email', description: 'The agent is using this opportunity and your strategy context.' };
  }
  if (job.status === 'insufficient_context') {
    return { title: 'More context is needed', description: 'The agent could not create a useful draft from the available context.' };
  }
  return {
    title: 'Email generation needs attention',
    description: 'The drafting service could not complete this request. The opportunity and any earlier draft are safe.',
  };
}

export function EmailWorkspace({ workspace }: { workspace: LeadWorkspace }) {
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const [sendOpen, setSendOpen] = useState(false);
  const lastCurrentEmailId = useRef<string | null | undefined>(undefined);
  const lastGenerationStatus = useRef<EmailGenerationJobStatus | undefined>(undefined);
  const lastDeliveryStatus = useRef<EmailDeliveryJobStatus | undefined>(undefined);
  const generationSubmitLock = useRef(false);
  const generationIdempotencyKey = useRef<string | null>(null);
  const deliverySubmitLock = useRef(false);
  const deliveryIdempotencyKey = useRef<string | null>(null);
  const deliveryIdempotencyEmailId = useRef<string | null>(null);
  const requestedEmailId = searchParams.get('email');
  const selectedEmail =
    workspace.emails.find((email) => email.id === requestedEmailId) ??
    workspace.emails.find((email) => email.id === workspace.current_email_id) ??
    null;

  const {
    register,
    handleSubmit,
    reset,
    formState: { errors, isDirty },
  } = useForm<EmailForm>({
    defaultValues: { recipient_email: '', subject: '', body: '' },
  });

  const selectEmail = useCallback((emailId: string, replace = false) => {
    const next = new URLSearchParams(searchParams);
    next.set('email', emailId);
    setSearchParams(next, { replace });
  }, [searchParams, setSearchParams]);

  useEffect(() => {
    const requestedIsValid = workspace.emails.some((email) => email.id === requestedEmailId);
    if ((!requestedEmailId || !requestedIsValid) && workspace.current_email_id) {
      selectEmail(workspace.current_email_id, true);
    }
  }, [requestedEmailId, selectEmail, workspace.current_email_id, workspace.emails]);

  useEffect(() => {
    const currentId = workspace.current_email_id;
    const previousId = lastCurrentEmailId.current;
    lastCurrentEmailId.current = currentId;
    if (previousId === undefined || !currentId || currentId === previousId) return;
    void queryClient.invalidateQueries({ queryKey: queryKeys.leads });
    void queryClient.invalidateQueries({ queryKey: queryKeys.emails });
    if (!isDirty) selectEmail(currentId, true);
  }, [isDirty, queryClient, selectEmail, workspace.current_email_id]);

  useEffect(() => {
    const currentStatus = workspace.latest_generation?.status;
    const previousStatus = lastGenerationStatus.current;
    lastGenerationStatus.current = currentStatus;
    const wasActive = previousStatus === 'queued' || previousStatus === 'running';
    const isTerminal = Boolean(currentStatus && currentStatus !== 'queued' && currentStatus !== 'running');
    if (wasActive && isTerminal) {
      void queryClient.invalidateQueries({ queryKey: queryKeys.leads });
    }
  }, [queryClient, workspace.latest_generation?.status]);

  useEffect(() => {
    const currentStatus = selectedEmail?.latest_delivery?.status;
    const previousStatus = lastDeliveryStatus.current;
    lastDeliveryStatus.current = currentStatus;
    const wasActive = previousStatus === 'queued' || previousStatus === 'running';
    const isTerminal = Boolean(currentStatus && currentStatus !== 'queued' && currentStatus !== 'running');
    if (wasActive && isTerminal) {
      void queryClient.invalidateQueries({ queryKey: queryKeys.leads });
      void queryClient.invalidateQueries({ queryKey: queryKeys.emails });
    }
  }, [queryClient, selectedEmail?.latest_delivery?.status]);

  useEffect(() => {
    if (selectedEmail && !isDirty) {
      reset({
        recipient_email: selectedEmail.recipient_email ?? '',
        subject: selectedEmail.subject ?? '',
        body: normalizeEmailBody(selectedEmail.body),
      });
    }
  }, [isDirty, reset, selectedEmail]);

  const replaceCachedEmail = (updated: Email) => {
    queryClient.setQueryData<LeadWorkspace>(queryKeys.leadWorkspace(workspace.lead.id), (current) => (
      current ? { ...current, emails: updateEmailList(current.emails, updated) } : current
    ));
    queryClient.setQueryData<Email[]>(queryKeys.emails, (current) => (
      current ? updateEmailList(current, updated) : current
    ));
    if (workspace.current_email_id === updated.id) {
      queryClient.setQueryData<Lead[]>(queryKeys.leads, (current) => current?.map((lead) => (
        lead.id === updated.lead_id
          ? {
              ...lead,
              current_email: {
                id: updated.id,
                status: updated.status,
                recipient_email: updated.recipient_email,
                created_at: updated.created_at,
                updated_at: updated.updated_at,
              },
            }
          : lead
      )));
    }
    queryClient.setQueryData(queryKeys.email(updated.id), updated);
  };

  const replaceCachedDelivery = (job: EmailDeliveryJob) => {
    const applyDelivery = (email: Email): Email => email.id === job.email_id
      ? {
          ...email,
          status: job.status === 'succeeded' ? 'sent' : email.status,
          latest_delivery: job,
          has_unknown_delivery: email.has_unknown_delivery || job.status === 'delivery_unknown',
        }
      : email;
    queryClient.setQueryData<LeadWorkspace>(queryKeys.leadWorkspace(workspace.lead.id), (current) => (
      current ? { ...current, emails: current.emails.map(applyDelivery) } : current
    ));
    queryClient.setQueryData<Email[]>(queryKeys.emails, (current) => current?.map(applyDelivery));
    queryClient.setQueryData<Email>(queryKeys.email(job.email_id), (current) => (
      current ? applyDelivery(current) : current
    ));
  };

  const generationMutation = useMutation({
    mutationFn: () => {
      generationIdempotencyKey.current ??= makeIdempotencyKey();
      return api.queueEmailGeneration(workspace.lead.id, generationIdempotencyKey.current);
    },
    onSuccess: (job) => {
      generationIdempotencyKey.current = null;
      queryClient.setQueryData<LeadWorkspace>(queryKeys.leadWorkspace(workspace.lead.id), (current) => (
        current ? { ...current, latest_generation: job } : current
      ));
      void queryClient.invalidateQueries({ queryKey: queryKeys.leads });
      toast.success(job.status === 'running' ? 'Email generation started' : 'Email queued');
    },
    onError: (error) => {
      toast.error('Could not queue email', {
        description: errorMessage(error, 'Please try again in a moment.'),
      });
    },
    onSettled: () => {
      generationSubmitLock.current = false;
    },
  });

  const saveMutation = useMutation<Email, ApiError, EmailForm>({
    mutationFn: (values) => api.editEmail(selectedEmail!.id, {
      recipient_email: values.recipient_email.trim() || null,
      subject: values.subject.trim(),
      body: normalizeEmailBody(values.body),
    }),
    onSuccess: (updated) => {
      const requiresReapproval = selectedEmail?.status === 'approved' && updated.status === 'pending_review';
      deliveryIdempotencyKey.current = null;
      deliveryIdempotencyEmailId.current = null;
      deliveryMutation.reset();
      replaceCachedEmail(updated);
      reset({
        recipient_email: updated.recipient_email ?? '',
        subject: updated.subject ?? '',
        body: normalizeEmailBody(updated.body),
      });
      toast.success(requiresReapproval ? 'Changes saved; approval required again' : 'Changes saved');
      if (workspace.current_email_id && workspace.current_email_id !== updated.id) {
        window.setTimeout(() => selectEmail(workspace.current_email_id!, true), 0);
      }
    },
  });

  const statusMutation = useMutation<Email, ApiError, EmailStatus>({
    mutationFn: (status) => api.setEmailStatus(selectedEmail!.id, status),
    onSuccess: (updated) => {
      replaceCachedEmail(updated);
      void queryClient.invalidateQueries({ queryKey: queryKeys.leads });
      void queryClient.invalidateQueries({ queryKey: queryKeys.emails });
      const messages: Partial<Record<EmailStatus, string>> = {
        pending_review: 'Submitted for review',
        approved: 'Outreach approved',
        rejected: 'Outreach rejected',
      };
      toast.success(messages[updated.status] ?? 'Status updated');
    },
  });

  const deliveryMutation = useMutation<EmailDeliveryJob, ApiError, { acknowledgeDuplicateRisk: boolean }>({
    mutationFn: ({ acknowledgeDuplicateRisk }) => {
      const emailId = selectedEmail!.id;
      if (deliveryIdempotencyEmailId.current !== emailId) {
        deliveryIdempotencyKey.current = null;
        deliveryIdempotencyEmailId.current = emailId;
      }
      deliveryIdempotencyKey.current ??= makeIdempotencyKey();
      return api.sendEmail(emailId, {
        idempotency_key: deliveryIdempotencyKey.current,
        expected_content_hash: selectedEmail!.delivery_content_hash,
        acknowledge_duplicate_risk: acknowledgeDuplicateRisk,
      });
    },
    onSuccess: (job) => {
      deliveryIdempotencyKey.current = null;
      replaceCachedDelivery(job);
      setSendOpen(false);
      if (job.status === 'queued' || job.status === 'running') {
        toast.success(job.status === 'running' ? 'Email delivery started' : 'Email queued for delivery');
      } else if (job.status === 'succeeded') {
        toast.success('Email sent');
      } else {
        toast.error(job.status === 'delivery_unknown' ? 'Delivery status is uncertain' : 'Email could not be sent');
      }
      if (job.status !== 'queued' && job.status !== 'running') {
        void queryClient.invalidateQueries({ queryKey: queryKeys.leads });
        void queryClient.invalidateQueries({ queryKey: queryKeys.emails });
      }
    },
    onError: (error) => {
      if (error.status !== 0) deliveryIdempotencyKey.current = null;
      if (error.status === 409) {
        void queryClient.invalidateQueries({ queryKey: queryKeys.leadWorkspace(workspace.lead.id) });
      }
      setSendOpen(false);
      toast.error('Could not queue email delivery', {
        description: errorMessage(error, 'Please try again in a moment.'),
      });
    },
    onSettled: () => {
      deliverySubmitLock.current = false;
    },
  });

  const shouldBlock = isDirty && !saveMutation.isPending;
  const blocker = useBlocker(shouldBlock);

  useBeforeUnload(useCallback((event) => {
    if (shouldBlock) {
      event.preventDefault();
      event.returnValue = '';
    }
  }, [shouldBlock]));

  const changeStatus = (status: EmailStatus) => {
    if (status !== 'sent' && !isDirty && !statusMutation.isPending) statusMutation.mutate(status);
  };

  const activeGeneration = isGenerationActive(workspace.latest_generation);
  const generationFailure = workspace.latest_generation && isGenerationFailure(workspace.latest_generation.status)
    ? workspace.latest_generation
    : null;
  const isHistorical = Boolean(selectedEmail && selectedEmail.id !== workspace.current_email_id);
  const newDraftReadyId = isDirty && selectedEmail && workspace.current_email_id !== selectedEmail.id
    ? workspace.current_email_id
    : null;
  const isTerminal = selectedEmail?.status === 'sent' || selectedEmail?.status === 'rejected';
  const latestDelivery = selectedEmail?.latest_delivery;
  const activeDelivery = isDeliveryActive(latestDelivery);
  const hasUnknownDelivery = Boolean(selectedEmail?.has_unknown_delivery);
  const deliveryRequestNeedsRetry = deliveryMutation.error?.status === 0;
  const deliveryBlocksGeneration = workspace.emails.some((email) => (
    isDeliveryActive(email.latest_delivery) || email.has_unknown_delivery
  ));
  const deliverySucceeded = latestDelivery?.status === 'succeeded';
  const isReadOnly = Boolean(isTerminal || activeDelivery || isHistorical || deliveryRequestNeedsRetry);
  const workflowLocked = isTerminal
    || activeGeneration
    || activeDelivery
    || isHistorical
    || isDirty
    || saveMutation.isPending
    || statusMutation.isPending
    || deliveryMutation.isPending;
  const savedContentIsDeliverable = Boolean(selectedEmail && hasDeliverableContent(selectedEmail));
  const generationLocked = activeGeneration
    || deliveryBlocksGeneration
    || deliveryRequestNeedsRetry
    || generationMutation.isPending
    || isDirty
    || saveMutation.isPending
    || statusMutation.isPending
    || deliveryMutation.isPending;
  const generationLabel = generationFailure
    ? 'Retry generation'
    : workspace.emails.length ? 'Regenerate email' : 'Generate email';
  const queueGeneration = () => {
    if (generationSubmitLock.current || generationLocked) return;
    generationSubmitLock.current = true;
    generationMutation.mutate();
  };
  const queueDelivery = () => {
    if (!selectedEmail || deliverySubmitLock.current || workflowLocked || !savedContentIsDeliverable) return;
    deliverySubmitLock.current = true;
    deliveryMutation.mutate({ acknowledgeDuplicateRisk: hasUnknownDelivery });
  };

  return (
    <section className={styles.emailWorkspace} aria-labelledby='email-workspace-heading'>
      <div className={styles.workspaceHeader}>
        <div>
          <p className={styles.sectionEyebrow}>Opportunity outreach</p>
          <h2 id='email-workspace-heading'>Email workspace</h2>
          <p>Review, edit, and move this opportunity's latest email through approval.</p>
        </div>
        <button
          className={styles.primaryButton}
          type='button'
          disabled={generationLocked}
          onClick={queueGeneration}
        >
          {generationFailure ? <RotateCcw aria-hidden='true' size={17} /> : <Sparkles aria-hidden='true' size={17} />}
          {activeGeneration || generationMutation.isPending ? 'Generation in progress...' : generationLabel}
        </button>
      </div>

      {activeGeneration && workspace.latest_generation ? (
        <div className={styles.generationState} data-status={workspace.latest_generation.status} role='status'>
          <span className={styles.generationStateIcon} aria-hidden='true'><Sparkles size={19} /></span>
          <div>
            <strong>{generationMessage(workspace.latest_generation).title}</strong>
            <p>{generationMessage(workspace.latest_generation).description}</p>
          </div>
        </div>
      ) : null}

      {generationFailure ? (
        <div className={styles.generationState} data-status='failed' role='alert'>
          <span className={styles.generationStateIcon} aria-hidden='true'><RotateCcw size={19} /></span>
          <div>
            <strong>{generationMessage(generationFailure).title}</strong>
            <p>{generationMessage(generationFailure).description}</p>
            {generationFailure.error_code ? <small>Reference: {generationFailure.error_code.replaceAll('_', ' ')}</small> : null}
          </div>
        </div>
      ) : null}

      {selectedEmail && latestDelivery ? (
        <div
          className={styles.deliveryState}
          data-status={latestDelivery.status}
          role={latestDelivery.status === 'failed' || latestDelivery.status === 'delivery_unknown' ? 'alert' : 'status'}
        >
          <span className={styles.deliveryStateIcon} aria-hidden='true'>
            {latestDelivery.status === 'queued' || latestDelivery.status === 'running'
              ? <LoaderCircle className={styles.spin} size={19} />
              : latestDelivery.status === 'succeeded'
                ? <CheckCircle2 size={19} />
                : latestDelivery.status === 'delivery_unknown'
                  ? <AlertTriangle size={19} />
                  : <X size={19} />}
          </span>
          <div>
            <strong>{deliveryMessage(latestDelivery.status).title}</strong>
            <p>{deliveryMessage(latestDelivery.status).description}</p>
            {latestDelivery.error_code ? (
              <small>Reference: {latestDelivery.error_code.replaceAll('_', ' ')}</small>
            ) : null}
          </div>
        </div>
      ) : null}

      {selectedEmail && hasUnknownDelivery && latestDelivery?.status !== 'delivery_unknown' && selectedEmail.status !== 'sent' ? (
        <div className={styles.deliveryState} data-status='delivery_unknown' role='alert'>
          <span className={styles.deliveryStateIcon} aria-hidden='true'><AlertTriangle size={19} /></span>
          <div>
            <strong>Earlier delivery is still uncertain</strong>
            <p>A previous attempt may have reached the recipient. Sending again could create a duplicate.</p>
          </div>
        </div>
      ) : null}

      {workspace.current_email_is_stale && workspace.current_email_id ? (
        <div className={styles.staleNotice} role='status'>
          <strong>Opportunity changed since this draft</strong>
          <span>Regenerate when you are ready to create a draft from the latest details.</span>
        </div>
      ) : null}

      {newDraftReadyId ? (
        <div className={styles.newDraftNotice} role='status'>
          <div>
            <strong>New draft ready</strong>
            <span>This earlier draft is now read-only. Discard your unsaved edits to open the new draft.</span>
          </div>
          <button
            className={styles.secondaryButton}
            type='button'
            onClick={() => {
              if (!selectedEmail || !newDraftReadyId) return;
              const nextId = newDraftReadyId;
              reset({
                recipient_email: selectedEmail.recipient_email ?? '',
                subject: selectedEmail.subject ?? '',
                body: normalizeEmailBody(selectedEmail.body),
              });
              window.setTimeout(() => selectEmail(nextId, true), 0);
            }}
          >
            Discard edits and open
          </button>
        </div>
      ) : null}

      {!selectedEmail ? (
        <div className={styles.workspaceEmpty}>
          <Mail aria-hidden='true' size={28} />
          <h3>{activeGeneration ? 'Your first email is on the way' : 'No email has been generated yet'}</h3>
          <p>
            {activeGeneration
              ? 'You can continue reviewing the opportunity while the worker prepares the draft.'
              : 'Generate an outreach email when you are ready. New imported opportunities are queued automatically.'}
          </p>
        </div>
      ) : (
        <div className={outreachStyles.detailLayout}>
          <main className={outreachStyles.editorCard}>
            <div className={outreachStyles.editorHeading}>
              <div>
                <p className={outreachStyles.sectionEyebrow}>Message editor</p>
                <h2>Email content</h2>
              </div>
              {isDirty ? (
                <span className={outreachStyles.unsavedBadge}>Unsaved changes</span>
              ) : isReadOnly && !activeDelivery ? (
                <span className={outreachStyles.readonlyBadge}>
                  {isHistorical ? 'Previous draft' : deliveryRequestNeedsRetry ? 'Retry required' : 'Read only'}
                </span>
              ) : (
                <StatusBadge status={selectedEmail.status} />
              )}
            </div>

            <form
              className={outreachStyles.editorForm}
              noValidate
              onSubmit={handleSubmit((values) => saveMutation.mutate(values))}
            >
              <label className={outreachStyles.field}>
                <span>To</span>
                <input
                  type='email'
                  autoComplete='email'
                  placeholder='recipient@example.com'
                  readOnly={isReadOnly}
                  aria-invalid={Boolean(errors.recipient_email)}
                  {...register('recipient_email', {
                    validate: (value) => !value.trim()
                      || isValidRecipient(value)
                      || 'Enter a valid recipient email address.',
                  })}
                />
                {errors.recipient_email && <small role='alert'>{errors.recipient_email.message}</small>}
              </label>

              <label className={outreachStyles.field}>
                <span>Subject</span>
                <input
                  type='text'
                  readOnly={isReadOnly}
                  aria-invalid={Boolean(errors.subject)}
                  {...register('subject', {
                    validate: (value) => value.trim().length > 0 || 'Enter a subject line.',
                  })}
                />
                {errors.subject && <small role='alert'>{errors.subject.message}</small>}
              </label>

              <label className={outreachStyles.field}>
                <span>Message</span>
                <textarea
                  rows={18}
                  readOnly={isReadOnly}
                  aria-invalid={Boolean(errors.body)}
                  {...register('body', {
                    validate: (value) => value.trim().length > 0 || 'Enter an email message.',
                  })}
                />
                {errors.body && <small role='alert'>{errors.body.message}</small>}
              </label>

              {saveMutation.error ? (
                <p className={outreachStyles.inlineError} role='alert'>
                  {errorMessage(saveMutation.error, 'Your changes could not be saved. Please try again.')}
                </p>
              ) : null}

              <div className={outreachStyles.formFooter}>
                <p aria-live='polite'>
                  {saveMutation.isPending
                    ? 'Saving changes...'
                    : activeDelivery
                      ? 'Delivery is in progress. Editing is temporarily locked.'
                      : deliveryRequestNeedsRetry
                        ? 'Retry the same send request before editing this approved email.'
                      : isReadOnly
                      ? 'Previous and completed messages are read-only.'
                      : isDirty
                        ? selectedEmail.status === 'approved'
                          ? 'Saving these changes will require approval again.'
                          : 'Save before changing the review status.'
                        : `Last updated ${formatDateTime(selectedEmail.updated_at)}`}
                </p>
                <button
                  className={outreachStyles.saveButton}
                  type='submit'
                  disabled={isReadOnly || !isDirty || saveMutation.isPending}
                >
                  <Save aria-hidden='true' size={17} />
                  {saveMutation.isPending ? 'Saving...' : 'Save changes'}
                </button>
              </div>
            </form>
          </main>

          <aside className={outreachStyles.detailSidebar} aria-label='Email details and workflow'>
            <section className={outreachStyles.contextCard}>
              <p className={outreachStyles.sectionEyebrow}>Saved delivery details</p>
              <h2>Recipient</h2>
              <dl className={outreachStyles.contextList}>
                <div>
                  <dt><UserRound aria-hidden='true' size={16} /> Current recipient</dt>
                  <dd>{selectedEmail.recipient_email || 'No recipient saved'}</dd>
                </div>
                <div>
                  <dt><Clock3 aria-hidden='true' size={16} /> Created</dt>
                  <dd>{formatDateTime(selectedEmail.created_at)}</dd>
                </div>
              </dl>
            </section>

            <section className={`${outreachStyles.contextCard} ${styles.historyCard}`}>
              <div className={styles.historyHeading}>
                <div>
                  <p className={outreachStyles.sectionEyebrow}>Draft history</p>
                  <h2>{workspace.emails.length} {workspace.emails.length === 1 ? 'email' : 'emails'}</h2>
                </div>
                <History aria-hidden='true' size={19} />
              </div>
              <ul className={styles.historyList}>
                {workspace.emails.map((email) => (
                  <li key={email.id}>
                    <Link
                      to={`/opportunities/${encodeURIComponent(workspace.lead.id)}?email=${encodeURIComponent(email.id)}`}
                      data-selected={email.id === selectedEmail.id}
                      aria-current={email.id === selectedEmail.id ? 'page' : undefined}
                    >
                      <span>{email.subject?.trim() || 'Untitled email'}</span>
                      <small>{formatDateTime(email.created_at)}</small>
                      <StatusBadge status={email.status} />
                    </Link>
                  </li>
                ))}
              </ul>
            </section>

            <section className={outreachStyles.workflowCard}>
              <div className={outreachStyles.workflowIcon} data-terminal={selectedEmail.status === 'sent'} aria-hidden='true'>
                {selectedEmail.status === 'sent' ? <CheckCircle2 size={21} /> : <Mail size={21} />}
              </div>
              <p className={outreachStyles.sectionEyebrow}>Review workflow</p>
              <h2>{isHistorical ? 'Previous draft' : transitionCopy(selectedEmail.status).title}</h2>
              <p>
                {isHistorical
                  ? 'Previous drafts are kept for reference and cannot re-enter the review workflow.'
                  : transitionCopy(selectedEmail.status).description}
              </p>

              {isDirty ? (
                <p className={outreachStyles.workflowNotice} role='status'>
                  {selectedEmail.status === 'approved'
                    ? 'Save your changes, then approve the updated email again.'
                    : 'Save your changes to unlock review actions.'}
                </p>
              ) : null}

              {!isDirty && selectedEmail.status === 'pending_review' && !savedContentIsDeliverable ? (
                <p className={outreachStyles.workflowNotice} role='status'>
                  Add a valid recipient, subject, and message, then save before approving.
                </p>
              ) : null}

              {!isDirty && selectedEmail.status === 'approved' && !savedContentIsDeliverable ? (
                <p className={outreachStyles.workflowNotice} role='status'>
                  Add valid delivery details and save them, then approve the updated email before sending.
                </p>
              ) : null}

              {activeDelivery ? (
                <p className={outreachStyles.workflowNotice} role='status'>
                  Delivery is in progress. Editing, review actions, and regeneration are locked.
                </p>
              ) : null}

              {deliveryRequestNeedsRetry ? (
                <p className={outreachStyles.workflowNotice} role='status'>
                  The connection ended before the request was confirmed. Retry Send to safely reuse the same request key.
                </p>
              ) : null}

              {statusMutation.error ? (
                <p className={outreachStyles.inlineError} role='alert'>
                  {errorMessage(statusMutation.error, 'The status could not be updated. Please try again.')}
                </p>
              ) : null}

              {deliveryMutation.error ? (
                <p className={outreachStyles.inlineError} role='alert'>
                  {errorMessage(deliveryMutation.error, 'The email could not be queued for delivery. Please try again.')}
                </p>
              ) : null}

              {!isHistorical ? (
                <div className={outreachStyles.workflowActions} aria-live='polite'>
                  {selectedEmail.status === 'draft' ? (
                    <button
                      className={outreachStyles.primaryAction}
                      type='button'
                      disabled={workflowLocked}
                      onClick={() => changeStatus('pending_review')}
                    >
                      <Send aria-hidden='true' size={17} />
                      {statusMutation.isPending ? 'Submitting...' : 'Submit for review'}
                    </button>
                  ) : null}

                  {selectedEmail.status === 'pending_review' ? (
                    <>
                      <button
                        className={outreachStyles.approveButton}
                        type='button'
                        disabled={workflowLocked || !savedContentIsDeliverable}
                        onClick={() => changeStatus('approved')}
                      >
                        <Check aria-hidden='true' size={17} /> Approve
                      </button>
                      <button
                        className={outreachStyles.rejectButton}
                        type='button'
                        disabled={workflowLocked}
                        onClick={() => changeStatus('rejected')}
                      >
                        <X aria-hidden='true' size={17} /> Reject
                      </button>
                    </>
                  ) : null}

                  {selectedEmail.status === 'approved' && !deliverySucceeded ? (
                    <button
                      className={outreachStyles.sentButton}
                      type='button'
                      disabled={workflowLocked || !savedContentIsDeliverable}
                      onClick={() => setSendOpen(true)}
                    >
                      {activeDelivery || deliveryMutation.isPending
                        ? <LoaderCircle className={styles.spin} aria-hidden='true' size={17} />
                        : hasUnknownDelivery
                          ? <AlertTriangle aria-hidden='true' size={17} />
                          : latestDelivery?.status === 'failed' || deliveryRequestNeedsRetry
                            ? <RotateCcw aria-hidden='true' size={17} />
                            : <Send aria-hidden='true' size={17} />}
                      {activeDelivery || deliveryMutation.isPending
                        ? 'Sending...'
                        : hasUnknownDelivery
                          ? 'Send again anyway'
                          : latestDelivery?.status === 'failed' || deliveryRequestNeedsRetry
                            ? 'Retry send'
                            : 'Send email'}
                    </button>
                  ) : null}
                </div>
              ) : null}
            </section>
          </aside>
        </div>
      )}

      <ConfirmDialog
        open={sendOpen}
        onOpenChange={setSendOpen}
        title={hasUnknownDelivery
          ? 'Send this email again anyway?'
          : deliveryRequestNeedsRetry ? 'Retry this send request?' : 'Send this email now?'}
        description={selectedEmail
          ? `${hasUnknownDelivery
            ? 'A previous attempt may already have reached this recipient, so sending again could create a duplicate. '
            : deliveryRequestNeedsRetry
              ? 'The previous request lost its connection; this retry will safely reuse the same request key. '
              : ''}This will send a real external email. Recipient: ${selectedEmail.recipient_email ?? 'the saved recipient'}. Subject: ${selectedEmail.subject}.`
          : 'This will send a real external email.'}
        confirmLabel={hasUnknownDelivery ? 'Send again anyway' : deliveryRequestNeedsRetry ? 'Retry send' : 'Send email'}
        onConfirm={queueDelivery}
        pending={deliveryMutation.isPending}
        variant={hasUnknownDelivery ? 'danger' : 'default'}
      />

      <ConfirmDialog
        open={blocker.state === 'blocked'}
        onOpenChange={(open) => {
          if (!open && blocker.state === 'blocked') blocker.reset();
        }}
        title='Discard unsaved changes?'
        description='Your edits have not been saved. Leaving now will permanently discard them.'
        confirmLabel='Discard and leave'
        onConfirm={() => {
          if (blocker.state === 'blocked') blocker.proceed();
        }}
        variant='danger'
      />
    </section>
  );
}
