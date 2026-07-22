import { useCallback, useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  CalendarDays,
  Check,
  CheckCircle2,
  Clock3,
  Mail,
  MapPin,
  Save,
  Send,
  UserRound,
  X,
} from "lucide-react";
import { useForm } from "react-hook-form";
import {
  Link,
  useBeforeUnload,
  useBlocker,
  useParams,
} from "react-router-dom";
import { toast } from "sonner";

import { api, ApiError } from "../../lib/api";
import { normalizeEmailBody } from "../../lib/emailText";
import { queryKeys } from "../../lib/queryKeys";
import type { Email, EmailStatus, Lead } from "../../types";
import {
  ConfirmDialog,
  EmptyState,
  ErrorState,
  LoadingState,
  PageHeader,
  StatusBadge,
} from "../../components/ui";
import styles from "./Outreach.module.css";

type EmailForm = {
  subject: string;
  body: string;
};

const dateTimeFormatter = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
  year: "numeric",
  hour: "numeric",
  minute: "2-digit",
});

function formatDateTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Date unavailable" : dateTimeFormatter.format(date);
}

function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError || error instanceof Error) {
    return error.message;
  }
  return fallback;
}

function updateCachedEmail(emails: Email[] | undefined, updated: Email): Email[] {
  if (!emails) {
    return [updated];
  }
  return emails.map((email) => (email.id === updated.id ? updated : email));
}

function getTransitionCopy(status: EmailStatus): { title: string; description: string } {
  switch (status) {
    case "draft":
      return {
        title: "Ready for another set of eyes?",
        description: "Submit this draft to place it in the review queue.",
      };
    case "pending_review":
      return {
        title: "Make the final call",
        description: "Approve a polished message or reject it when it should not move forward.",
      };
    case "approved":
      return {
        title: "Approved and ready",
        description: "Once the message is handled outside this workspace, record it as sent.",
      };
    case "sent":
      return {
        title: "Recorded as sent",
        description: "This message has completed its review workflow.",
      };
    case "rejected":
      return {
        title: "Closed without sending",
        description: "This message was rejected and is now read-only in the workflow.",
      };
  }
}

export function OutreachDetailPage() {
  const { emailId } = useParams<{ emailId: string }>();
  const queryClient = useQueryClient();
  const [markSentOpen, setMarkSentOpen] = useState(false);

  const emailsQuery = useQuery<Email[], ApiError>({
    queryKey: queryKeys.emails,
    queryFn: api.listEmails,
  });
  const leadsQuery = useQuery<Lead[], ApiError>({
    queryKey: queryKeys.leads,
    queryFn: api.listLeads,
  });

  const email = useMemo(
    () => emailsQuery.data?.find((candidate) => candidate.id === emailId),
    [emailId, emailsQuery.data],
  );
  const lead = useMemo(
    () => leadsQuery.data?.find((candidate) => candidate.id === email?.lead_id),
    [email?.lead_id, leadsQuery.data],
  );

  const {
    register,
    handleSubmit,
    reset,
    formState: { errors, isDirty },
  } = useForm<EmailForm>({
    defaultValues: { subject: "", body: "" },
  });

  useEffect(() => {
    if (email && !isDirty) {
      reset({
        subject: email.subject ?? "",
        body: normalizeEmailBody(email.body),
      });
    }
  }, [email, isDirty, reset]);

  const saveMutation = useMutation<Email, ApiError, EmailForm>({
    mutationFn: (values) =>
      api.editEmail(email!.id, {
        subject: values.subject.trim(),
        body: normalizeEmailBody(values.body),
      }),
    onSuccess: (updated) => {
      queryClient.setQueryData<Email[]>(queryKeys.emails, (current) =>
        updateCachedEmail(current, updated),
      );
      reset({
        subject: updated.subject ?? "",
        body: normalizeEmailBody(updated.body),
      });
      toast.success("Changes saved");
    },
  });

  const statusMutation = useMutation<Email, ApiError, EmailStatus>({
    mutationFn: (status) => api.setEmailStatus(email!.id, status),
    onSuccess: (updated) => {
      queryClient.setQueryData<Email[]>(queryKeys.emails, (current) =>
        updateCachedEmail(current, updated),
      );
      setMarkSentOpen(false);
      const message: Partial<Record<EmailStatus, string>> = {
        pending_review: "Submitted for review",
        approved: "Outreach approved",
        rejected: "Outreach rejected",
        sent: "Outreach marked as sent",
      };
      toast.success(message[updated.status] ?? "Status updated");
    },
  });

  const shouldBlock = isDirty && !saveMutation.isPending;
  const blocker = useBlocker(shouldBlock);

  useBeforeUnload(
    useCallback(
      (event) => {
        if (shouldBlock) {
          event.preventDefault();
          event.returnValue = "";
        }
      },
      [shouldBlock],
    ),
  );

  const keepEditing = () => {
    if (blocker.state === "blocked") {
      blocker.reset();
    }
  };

  const discardAndLeave = () => {
    if (blocker.state === "blocked") {
      blocker.proceed();
    }
  };

  const changeStatus = (status: EmailStatus) => {
    if (!isDirty && !statusMutation.isPending) {
      statusMutation.mutate(status);
    }
  };

  const retry = () => {
    void Promise.all([emailsQuery.refetch(), leadsQuery.refetch()]);
  };

  if (emailsQuery.isLoading || leadsQuery.isLoading) {
    return <LoadingState label="Opening outreach draft…" />;
  }

  const queryError = emailsQuery.error ?? leadsQuery.error;
  if (queryError) {
    return (
      <ErrorState
        title="This outreach couldn't be opened"
        message={errorMessage(queryError, "Please try again.")}
        onRetry={retry}
      />
    );
  }

  if (!email) {
    return (
      <EmptyState
        icon={<Mail aria-hidden="true" />}
        title="Outreach not found"
        description="It may have been removed, or the link may no longer be valid."
        action={
          <Link className={styles.secondaryAction} to="/outreach">
            Return to outreach
          </Link>
        }
      />
    );
  }

  const transitionCopy = getTransitionCopy(email.status);
  const isTerminal = email.status === "sent" || email.status === "rejected";
  const workflowLocked = isDirty || saveMutation.isPending || statusMutation.isPending;

  return (
    <div className={styles.page}>
      <Link className={styles.backLink} to="/outreach">
        <ArrowLeft aria-hidden="true" size={16} />
        Back to outreach
      </Link>

      <PageHeader
        eyebrow={lead?.project || "Outreach draft"}
        title={email.subject?.trim() || "Untitled outreach"}
        description="Refine the message and guide it through review."
        actions={<StatusBadge status={email.status} />}
      />

      <div className={styles.detailLayout}>
        <main className={styles.editorCard}>
          <div className={styles.editorHeading}>
            <div>
              <p className={styles.sectionEyebrow}>Message editor</p>
              <h2>Email content</h2>
            </div>
            {isDirty ? (
              <span className={styles.unsavedBadge}>Unsaved changes</span>
            ) : isTerminal ? (
              <span className={styles.readonlyBadge}>Read only</span>
            ) : null}
          </div>

          <form className={styles.editorForm} onSubmit={handleSubmit((values) => saveMutation.mutate(values))}>
            <label className={styles.field}>
              <span>Subject</span>
              <input
                type="text"
                readOnly={isTerminal}
                aria-invalid={Boolean(errors.subject)}
                {...register("subject", {
                  validate: (value) => value.trim().length > 0 || "Enter a subject line.",
                })}
              />
              {errors.subject && <small role="alert">{errors.subject.message}</small>}
            </label>

            <label className={styles.field}>
              <span>Message</span>
              <textarea
                rows={18}
                readOnly={isTerminal}
                aria-invalid={Boolean(errors.body)}
                {...register("body", {
                  validate: (value) => value.trim().length > 0 || "Enter an email message.",
                })}
              />
              {errors.body && <small role="alert">{errors.body.message}</small>}
            </label>

            {saveMutation.error && (
              <p className={styles.inlineError} role="alert">
                {errorMessage(saveMutation.error, "Your changes couldn't be saved. Please try again.")}
              </p>
            )}

            <div className={styles.formFooter}>
              <p aria-live="polite">
                {saveMutation.isPending
                  ? "Saving changes…"
                  : isTerminal
                    ? "Completed messages are read-only."
                  : isDirty
                    ? "Save before changing the review status."
                    : `Last updated ${formatDateTime(email.updated_at)}`}
              </p>
              <button
                className={styles.saveButton}
                type="submit"
                disabled={isTerminal || !isDirty || saveMutation.isPending}
              >
                <Save aria-hidden="true" size={17} />
                {saveMutation.isPending ? "Saving…" : "Save changes"}
              </button>
            </div>
          </form>
        </main>

        <aside className={styles.detailSidebar} aria-label="Outreach details and workflow">
          <section className={styles.contextCard}>
            <p className={styles.sectionEyebrow}>Opportunity</p>
            <h2>{lead?.project || "Opportunity details"}</h2>
            <dl className={styles.contextList}>
              {(lead?.contacts || lead?.contact_email) && (
                <div>
                  <dt>
                    <UserRound aria-hidden="true" size={16} /> Recipient
                  </dt>
                  <dd>{lead.contacts || lead.contact_email}</dd>
                  {lead.contacts && lead.contact_email && <dd>{lead.contact_email}</dd>}
                </div>
              )}
              {(lead?.location || lead?.state) && (
                <div>
                  <dt>
                    <MapPin aria-hidden="true" size={16} /> Location
                  </dt>
                  <dd>{[lead.location, lead.state].filter(Boolean).join(", ")}</dd>
                </div>
              )}
              <div>
                <dt>
                  <CalendarDays aria-hidden="true" size={16} /> Created
                </dt>
                <dd>{formatDateTime(email.created_at)}</dd>
              </div>
              <div>
                <dt>
                  <Clock3 aria-hidden="true" size={16} /> Last edited
                </dt>
                <dd>{formatDateTime(email.updated_at)}</dd>
              </div>
            </dl>
            {lead && (
              <Link className={styles.contextLink} to={`/opportunities/${encodeURIComponent(lead.id)}`}>
                View opportunity
              </Link>
            )}
          </section>

          <section className={styles.workflowCard}>
            <div className={styles.workflowIcon} data-terminal={email.status === "sent"} aria-hidden="true">
              {email.status === "sent" ? <CheckCircle2 size={21} /> : <Mail size={21} />}
            </div>
            <p className={styles.sectionEyebrow}>Review workflow</p>
            <h2>{transitionCopy.title}</h2>
            <p>{transitionCopy.description}</p>

            {isDirty && (
              <p className={styles.workflowNotice} role="status">
                Save your changes to unlock review actions.
              </p>
            )}

            {statusMutation.error && (
              <p className={styles.inlineError} role="alert">
                {errorMessage(statusMutation.error, "The status couldn't be updated. Please try again.")}
              </p>
            )}

            <div className={styles.workflowActions} aria-live="polite">
              {email.status === "draft" && (
                <button
                  className={styles.primaryAction}
                  type="button"
                  disabled={workflowLocked}
                  onClick={() => changeStatus("pending_review")}
                >
                  <Send aria-hidden="true" size={17} />
                  {statusMutation.isPending ? "Submitting…" : "Submit for review"}
                </button>
              )}

              {email.status === "pending_review" && (
                <>
                  <button
                    className={styles.approveButton}
                    type="button"
                    disabled={workflowLocked}
                    onClick={() => changeStatus("approved")}
                  >
                    <Check aria-hidden="true" size={17} />
                    Approve
                  </button>
                  <button
                    className={styles.rejectButton}
                    type="button"
                    disabled={workflowLocked}
                    onClick={() => changeStatus("rejected")}
                  >
                    <X aria-hidden="true" size={17} />
                    Reject
                  </button>
                </>
              )}

              {email.status === "approved" && (
                <button
                  className={styles.sentButton}
                  type="button"
                  disabled={workflowLocked}
                  onClick={() => setMarkSentOpen(true)}
                >
                  <CheckCircle2 aria-hidden="true" size={17} />
                  Mark as sent
                </button>
              )}
            </div>
          </section>
        </aside>
      </div>

      <ConfirmDialog
        open={markSentOpen}
        onOpenChange={setMarkSentOpen}
        title="Mark this outreach as sent?"
        description="This records the status in Accoya Outreach only. It does not deliver the email to the recipient. Sent is a final status in this interface."
        confirmLabel="Mark as sent"
        onConfirm={() => changeStatus("sent")}
        pending={statusMutation.isPending}
        variant="danger"
      />

      <ConfirmDialog
        open={blocker.state === "blocked"}
        onOpenChange={(open) => {
          if (!open) keepEditing();
        }}
        title="Discard unsaved changes?"
        description="Your edits have not been saved. Leaving now will permanently discard them."
        confirmLabel="Discard and leave"
        onConfirm={discardAndLeave}
        variant="danger"
      />
    </div>
  );
}
