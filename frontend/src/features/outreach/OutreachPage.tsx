import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  ArrowRight,
  Clock3,
  Inbox,
  Mail,
  MapPin,
  Search,
  UserRound,
} from "lucide-react";
import { Link } from "react-router-dom";

import { api, ApiError } from "../../lib/api";
import { queryKeys } from "../../lib/queryKeys";
import type { Email, EmailStatus, Lead } from "../../types";
import {
  EmptyState,
  ErrorState,
  LoadingState,
  PageHeader,
  StatusBadge,
} from "../../components/ui";
import styles from "./Outreach.module.css";

type StatusFilter = "all" | EmailStatus;

const statusOptions: Array<{ value: StatusFilter; label: string }> = [
  { value: "pending_review", label: "Needs review" },
  { value: "draft", label: "Drafts" },
  { value: "approved", label: "Approved" },
  { value: "sent", label: "Sent" },
  { value: "rejected", label: "Rejected" },
  { value: "all", label: "All" },
];

const dateFormatter = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
  year: "numeric",
});

function formatDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Date unavailable" : dateFormatter.format(date);
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError || error instanceof Error) {
    return error.message;
  }
  return "We couldn't load the outreach queue. Please try again.";
}

function leadLabel(lead: Lead | undefined): string {
  return lead?.project?.trim() || "Untitled opportunity";
}

export function OutreachPage() {
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("pending_review");
  const [search, setSearch] = useState("");

  const emailsQuery = useQuery<Email[], ApiError>({
    queryKey: queryKeys.emails,
    queryFn: api.listEmails,
  });
  const leadsQuery = useQuery<Lead[], ApiError>({
    queryKey: queryKeys.leads,
    queryFn: api.listLeads,
  });

  const leadsById = useMemo(
    () => new Map((leadsQuery.data ?? []).map((lead) => [lead.id, lead])),
    [leadsQuery.data],
  );

  const counts = useMemo(() => {
    const next: Record<StatusFilter, number> = {
      all: 0,
      draft: 0,
      pending_review: 0,
      approved: 0,
      sent: 0,
      rejected: 0,
    };

    for (const email of emailsQuery.data ?? []) {
      next.all += 1;
      next[email.status] += 1;
    }
    return next;
  }, [emailsQuery.data]);

  const visibleEmails = useMemo(() => {
    const needle = search.trim().toLocaleLowerCase();

    return (emailsQuery.data ?? []).filter((email) => {
      if (statusFilter !== "all" && email.status !== statusFilter) {
        return false;
      }
      if (!needle) {
        return true;
      }

      const lead = leadsById.get(email.lead_id);
      return [
        email.subject,
        email.body,
        lead?.project,
        lead?.location,
        lead?.state,
        lead?.contacts,
        lead?.contact_email,
      ].some((value) => value?.toLocaleLowerCase().includes(needle));
    });
  }, [emailsQuery.data, leadsById, search, statusFilter]);

  const retry = () => {
    void Promise.all([emailsQuery.refetch(), leadsQuery.refetch()]);
  };

  if (emailsQuery.isLoading || leadsQuery.isLoading) {
    return <LoadingState label="Preparing your outreach queue…" />;
  }

  const queryError = emailsQuery.error ?? leadsQuery.error;
  if (queryError) {
    return (
      <ErrorState
        title="Outreach is temporarily unavailable"
        message={errorMessage(queryError)}
        onRetry={retry}
      />
    );
  }

  return (
    <div className={styles.page}>
      <PageHeader
        eyebrow="Client communication"
        title="Outreach review"
        description="Shape every message with care, then move it through review when it is ready."
        actions={
          <Link className={styles.primaryAction} to="/opportunities">
            Find an opportunity
            <ArrowRight aria-hidden="true" size={17} />
          </Link>
        }
      />

      <section className={styles.queueSummary} aria-label="Review queue summary">
        <div className={styles.summaryIcon} aria-hidden="true">
          <Inbox size={21} />
        </div>
        <div>
          <span className={styles.summaryValue}>{counts.pending_review}</span>
          <span className={styles.summaryLabel}> awaiting review</span>
        </div>
        <p>
          {counts.pending_review === 0
            ? "Everything has been reviewed."
            : "Review the message, refine the language, and approve when it feels right."}
        </p>
      </section>

      <section className={styles.queue} aria-labelledby="outreach-queue-heading">
        <div className={styles.queueHeader}>
          <div>
            <p className={styles.sectionEyebrow}>Workspace</p>
            <h2 id="outreach-queue-heading">Messages</h2>
          </div>
          <label className={styles.searchField}>
            <Search aria-hidden="true" size={18} />
            <span className="sr-only">Search outreach</span>
            <input
              type="search"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Search project, contact, or subject"
            />
          </label>
        </div>

        <div className={styles.filters} aria-label="Filter messages by status">
          {statusOptions.map((option) => (
            <button
              className={styles.filter}
              data-active={statusFilter === option.value}
              type="button"
              key={option.value}
              onClick={() => setStatusFilter(option.value)}
              aria-pressed={statusFilter === option.value}
            >
              <span>{option.label}</span>
              <span className={styles.filterCount}>{counts[option.value]}</span>
            </button>
          ))}
        </div>

        {visibleEmails.length === 0 ? (
          <EmptyState
            icon={<Mail aria-hidden="true" />}
            title={search ? "No matching messages" : "No messages in this view"}
            description={
              search
                ? "Try a different project, contact, or subject."
                : statusFilter === "pending_review"
                  ? "New generated outreach will appear here when it is ready for review."
                  : "Choose another status or generate outreach from an opportunity."
            }
            action={
              search ? (
                <button className={styles.secondaryAction} type="button" onClick={() => setSearch("")}>
                  Clear search
                </button>
              ) : (
                <Link className={styles.secondaryAction} to="/opportunities">
                  Browse opportunities
                </Link>
              )
            }
          />
        ) : (
          <div className={styles.messageList}>
            {visibleEmails.map((email) => {
              const lead = leadsById.get(email.lead_id);
              return (
                <article className={styles.messageRow} key={email.id}>
                  <div className={styles.messageIdentity}>
                    <div className={styles.mailIcon} aria-hidden="true">
                      <Mail size={18} />
                    </div>
                    <div className={styles.messageTitleGroup}>
                      <h3>{email.subject?.trim() || "Untitled outreach"}</h3>
                      <p>{leadLabel(lead)}</p>
                    </div>
                  </div>

                  <div className={styles.messageContext}>
                    {(lead?.contacts || lead?.contact_email) && (
                      <span>
                        <UserRound aria-hidden="true" size={15} />
                        {lead.contacts || lead.contact_email}
                      </span>
                    )}
                    {(lead?.location || lead?.state) && (
                      <span>
                        <MapPin aria-hidden="true" size={15} />
                        {[lead.location, lead.state].filter(Boolean).join(", ")}
                      </span>
                    )}
                    <span>
                      <Clock3 aria-hidden="true" size={15} />
                      Updated {formatDate(email.updated_at)}
                    </span>
                  </div>

                  <div className={styles.messageActions}>
                    <StatusBadge status={email.status} />
                    <Link
                      className={styles.openMessage}
                      to={`/outreach/${encodeURIComponent(email.id)}`}
                      aria-label={`Open ${email.subject?.trim() || "untitled outreach"}`}
                    >
                      Open
                      <ArrowRight aria-hidden="true" size={16} />
                    </Link>
                  </div>
                </article>
              );
            })}
          </div>
        )}
      </section>
    </div>
  );
}
