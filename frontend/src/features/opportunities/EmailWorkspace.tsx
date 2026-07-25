import { useCallback, useEffect, useRef, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import {
  Check,
  CheckCircle2,
  Clock3,
  History,
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
  EmailGenerationJob,
  EmailGenerationJobStatus,
  EmailStatus,
  Lead,
  LeadWorkspace,
} from '../../types';
import outreachStyles from '../outreach/Outreach.module.css';
import styles from './opportunities.module.css';

type EmailForm = {
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
      return { title: 'Approved and ready', description: 'Record it as sent once handled outside this workspace.' };
    case 'sent':
      return { title: 'Recorded as sent', description: 'This message has completed its review workflow.' };
    case 'rejected':
      return { title: 'Closed without sending', description: 'This message was rejected and is read-only.' };
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
  const [markSentOpen, setMarkSentOpen] = useState(false);
  const lastCurrentEmailId = useRef<string | null | undefined>(undefined);
  const lastGenerationStatus = useRef<EmailGenerationJobStatus | undefined>(undefined);
  const generationSubmitLock = useRef(false);
  const generationIdempotencyKey = useRef<string | null>(null);
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
  } = useForm<EmailForm>({ defaultValues: { subject: '', body: '' } });

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
    if (selectedEmail && !isDirty) {
      reset({
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
      subject: values.subject.trim(),
      body: normalizeEmailBody(values.body),
    }),
    onSuccess: (updated) => {
      replaceCachedEmail(updated);
      reset({ subject: updated.subject ?? '', body: normalizeEmailBody(updated.body) });
      toast.success('Changes saved');
      if (workspace.current_email_id && workspace.current_email_id !== updated.id) {
        window.setTimeout(() => selectEmail(workspace.current_email_id!, true), 0);
      }
    },
  });

  const statusMutation = useMutation<Email, ApiError, EmailStatus>({
    mutationFn: (status) => api.setEmailStatus(selectedEmail!.id, status),
    onSuccess: (updated) => {
      replaceCachedEmail(updated);
      setMarkSentOpen(false);
      void queryClient.invalidateQueries({ queryKey: queryKeys.leads });
      void queryClient.invalidateQueries({ queryKey: queryKeys.emails });
      const messages: Partial<Record<EmailStatus, string>> = {
        pending_review: 'Submitted for review',
        approved: 'Outreach approved',
        rejected: 'Outreach rejected',
        sent: 'Outreach marked as sent',
      };
      toast.success(messages[updated.status] ?? 'Status updated');
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
    if (!isDirty && !statusMutation.isPending) statusMutation.mutate(status);
  };

  const activeGeneration = isGenerationActive(workspace.latest_generation);
  const generationFailure = workspace.latest_generation && isGenerationFailure(workspace.latest_generation.status)
    ? workspace.latest_generation
    : null;
  const isHistorical = Boolean(selectedEmail && selectedEmail.id !== workspace.current_email_id);
  const newDraftReadyId = isDirty && selectedEmail && workspace.current_email_id !== selectedEmail.id
    ? workspace.current_email_id
    : null;
  const canFinishSupersededEdit = Boolean(isHistorical && newDraftReadyId);
  const isTerminal = selectedEmail?.status === 'sent' || selectedEmail?.status === 'rejected';
  const isReadOnly = Boolean(isTerminal || (isHistorical && !canFinishSupersededEdit));
  const workflowLocked = isReadOnly || isDirty || saveMutation.isPending || statusMutation.isPending;
  const generationLocked = activeGeneration
    || generationMutation.isPending
    || isDirty
    || saveMutation.isPending
    || statusMutation.isPending;
  const generationLabel = generationFailure
    ? 'Retry generation'
    : workspace.emails.length ? 'Regenerate email' : 'Generate email';
  const queueGeneration = () => {
    if (generationSubmitLock.current || generationLocked) return;
    generationSubmitLock.current = true;
    generationMutation.mutate();
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
            <span>Save your current edits or discard them to open the new draft.</span>
          </div>
          <button
            className={styles.secondaryButton}
            type='button'
            onClick={() => {
              if (!selectedEmail || !newDraftReadyId) return;
              const nextId = newDraftReadyId;
              reset({
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
              ) : isReadOnly ? (
                <span className={outreachStyles.readonlyBadge}>{isHistorical ? 'Previous draft' : 'Read only'}</span>
              ) : (
                <StatusBadge status={selectedEmail.status} />
              )}
            </div>

            <form className={outreachStyles.editorForm} onSubmit={handleSubmit((values) => saveMutation.mutate(values))}>
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
                    : isReadOnly
                      ? 'Previous and completed messages are read-only.'
                      : isDirty
                        ? 'Save before changing the review status.'
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
              <p className={outreachStyles.sectionEyebrow}>Delivery snapshot</p>
              <h2>Recipient</h2>
              <dl className={outreachStyles.contextList}>
                <div>
                  <dt><UserRound aria-hidden='true' size={16} /> Email at generation</dt>
                  <dd>{selectedEmail.recipient_email || 'No recipient was captured'}</dd>
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
                  Save your changes to unlock review actions.
                </p>
              ) : null}

              {statusMutation.error ? (
                <p className={outreachStyles.inlineError} role='alert'>
                  {errorMessage(statusMutation.error, 'The status could not be updated. Please try again.')}
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
                        disabled={workflowLocked}
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

                  {selectedEmail.status === 'approved' ? (
                    <button
                      className={outreachStyles.sentButton}
                      type='button'
                      disabled={workflowLocked}
                      onClick={() => setMarkSentOpen(true)}
                    >
                      <CheckCircle2 aria-hidden='true' size={17} /> Mark as sent
                    </button>
                  ) : null}
                </div>
              ) : null}
            </section>
          </aside>
        </div>
      )}

      <ConfirmDialog
        open={markSentOpen}
        onOpenChange={setMarkSentOpen}
        title='Mark this outreach as sent?'
        description='This records the status in Accoya Outreach only. It does not deliver the email to the recipient. Sent is a final status in this interface.'
        confirmLabel='Mark as sent'
        onConfirm={() => changeStatus('sent')}
        pending={statusMutation.isPending}
        variant='danger'
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
