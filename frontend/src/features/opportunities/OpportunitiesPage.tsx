import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  CheckCircle2,
  Clock3,
  FileUp,
  MapPin,
  RefreshCw,
  Search,
  Sparkles,
  Trash2,
} from "lucide-react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { toast } from "sonner";

import { EmptyState, ErrorState, LoadingState, PageHeader, StatusBadge } from "../../components/ui";
import { api, ApiError } from "../../lib/api";
import { queryKeys } from "../../lib/queryKeys";
import type { EarlyBidSyncRunStatus, EarlyBidSyncStatus, Lead } from "../../types";
import styles from "./opportunities.module.css";

type ScoreSort = "desc" | "asc";
type OpportunitySort = "score_desc" | "score_asc" | "contact_present" | "contact_missing";
type OutreachFilter =
  | ""
  | "pending_review"
  | "approved"
  | "sent"
  | "rejected"
  | "generating"
  | "generation_issues"
  | "no_email";

const outreachOptions: Array<{ value: OutreachFilter; label: string }> = [
  { value: "", label: "All outreach" },
  { value: "pending_review", label: "Needs review" },
  { value: "approved", label: "Approved" },
  { value: "sent", label: "Sent" },
  { value: "rejected", label: "Rejected" },
  { value: "generating", label: "Generating" },
  { value: "generation_issues", label: "Generation issues" },
  { value: "no_email", label: "No email" },
];

const sortSummaryLabels: Record<OpportunitySort, string> = {
  score_desc: "Sorted by score: high to low",
  score_asc: "Sorted by score: low to high",
  contact_present: "Sorted by contact: provided first",
  contact_missing: "Sorted by contact: missing first",
};

const scoreFormatter = new Intl.NumberFormat(undefined, {
  maximumFractionDigits: 1,
});

const automaticSyncActiveStatuses: ReadonlySet<EarlyBidSyncRunStatus> = new Set([
  "queued",
  "running",
  "retry_wait",
]);

const automaticSyncLabels: Record<EarlyBidSyncRunStatus, string> = {
  queued: "Queued",
  running: "In progress",
  retry_wait: "Retry scheduled",
  succeeded: "Completed",
  failed: "Failed",
};

const automaticSyncErrorLabels: Record<string, string> = {
  missing_configuration: "The scheduler is missing required EarlyBid configuration.",
  upstream_request_error: "The EarlyBid request could not be completed.",
  upstream_unavailable: "EarlyBid was unavailable.",
  upstream_rate_limited: "EarlyBid temporarily limited requests.",
  upstream_auth_error: "EarlyBid rejected the configured credentials.",
  invalid_feed: "EarlyBid returned an invalid feed.",
  persistence_error: "The synchronized data could not be saved.",
  superseded_schedule: "A newer daily schedule replaced this run.",
  worker_lease_expired: "The scheduler worker stopped responding.",
};

function automaticSyncIsActive(status: EarlyBidSyncRunStatus | undefined) {
  return status ? automaticSyncActiveStatuses.has(status) : false;
}

function formatPacificDateTime(value: string | null | undefined, timezone: string) {
  if (!value) return "Not scheduled";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Not available";

  const options: Intl.DateTimeFormatOptions = {
    timeZone: timezone,
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "short",
  };
  try {
    return new Intl.DateTimeFormat("en-US", options).format(date);
  } catch {
    return new Intl.DateTimeFormat("en-US", {
      ...options,
      timeZone: "America/Los_Angeles",
    }).format(date);
  }
}

function AutomaticSyncStatusPanel({
  status,
  pending,
  failed,
  onRetry,
}: {
  status: EarlyBidSyncStatus | undefined;
  pending: boolean;
  failed: boolean;
  onRetry: () => void;
}) {
  const run = status?.latest_run;
  const timezone = status?.timezone ?? "America/Los_Angeles";
  const isActive = automaticSyncIsActive(run?.status);

  let title = "No automatic sync yet";
  let description = "The scheduler will catch up today if midnight has already passed.";
  if (pending) {
    title = "Loading automatic sync status";
    description = "Checking the daily EarlyBid schedule.";
  } else if (failed) {
    title = "Automatic sync status unavailable";
    description = "Opportunities remain available, but the scheduler status could not be loaded.";
  } else if (run?.status === "queued") {
    title = "Automatic sync queued";
    description = "The daily sync is waiting for a scheduler worker.";
  } else if (run?.status === "running") {
    title = "Automatic sync in progress";
    description = `Attempt ${run.attempt_count} is synchronizing the latest EarlyBid feed.`;
  } else if (run?.status === "retry_wait") {
    title = "Automatic sync retry scheduled";
    description = `Attempt ${run.attempt_count} did not complete. The scheduler will retry automatically.`;
  } else if (run?.status === "succeeded") {
    title = "Last automatic sync completed";
    description = `${run.total} processed · ${run.created} created · ${run.updated} updated · ${run.generation_queued} drafts queued`;
  } else if (run?.status === "failed") {
    title = "Last automatic sync failed";
    description = `Automatic sync stopped after ${run.attempt_count} attempt${run.attempt_count === 1 ? "" : "s"}. Manual sync is available.`;
  }

  return (
    <section
      id="automatic-sync-status"
      className={styles.automaticSyncPanel}
      data-status={run?.status ?? (failed ? "unavailable" : "idle")}
      aria-labelledby="automatic-sync-title"
      aria-live="polite"
      aria-atomic="true"
    >
      <span className={styles.automaticSyncIcon} aria-hidden="true">
        {run?.status === "succeeded" ? <CheckCircle2 size={20} /> : <Clock3 size={20} />}
      </span>
      <div className={styles.automaticSyncCopy}>
        <div className={styles.automaticSyncTopline}>
          <p>Daily EarlyBid sync</p>
          {run ? (
            <span className={styles.automaticSyncBadge} data-status={run.status}>
              {automaticSyncLabels[run.status]}
            </span>
          ) : null}
          {status?.overdue ? <span className={styles.overdueBadge}>Overdue</span> : null}
        </div>
        <h2 id="automatic-sync-title">{title}</h2>
        <p>{description}</p>
        {run?.error_code ? (
          <p className={styles.automaticSyncError}>
            {automaticSyncErrorLabels[run.error_code] ?? "The scheduler reported a safe synchronization error."}
          </p>
        ) : null}
        {status?.overdue ? (
          <p className={styles.automaticSyncError}>The current daily run is overdue. Check the scheduler process.</p>
        ) : null}
        <dl className={styles.automaticSyncDetails}>
          <div>
            <dt>Next daily sync</dt>
            <dd>{status ? formatPacificDateTime(status.next_scheduled_at, timezone) : "Checking…"}</dd>
          </div>
          {run?.status === "retry_wait" ? (
            <div>
              <dt>Next retry</dt>
              <dd>{formatPacificDateTime(run.next_attempt_at, timezone)}</dd>
            </div>
          ) : null}
          {run?.completed_at ? (
            <div>
              <dt>Last completed</dt>
              <dd>{formatPacificDateTime(run.completed_at, timezone)}</dd>
            </div>
          ) : null}
          {isActive ? (
            <div>
              <dt>Feed</dt>
              <dd>{run?.feed}</dd>
            </div>
          ) : null}
        </dl>
      </div>
      {failed ? (
        <button className={styles.secondaryButton} type="button" onClick={onRetry}>
          Retry status
        </button>
      ) : null}
    </section>
  );
}

function displayValue(value: string | null | undefined) {
  return value?.trim() || "Not provided";
}

function formatScore(score: number | null) {
  return score === null ? "—" : scoreFormatter.format(score);
}

function errorMessage(error: unknown, fallback: string) {
  if (error instanceof ApiError && error.message) return error.message;
  if (error instanceof Error && error.message) return error.message;
  return fallback;
}

function leadSearchText(lead: Lead) {
  return [
    lead.project,
    lead.location,
    lead.contacts,
    lead.contact_email,
    lead.tags,
  ]
    .filter(Boolean)
    .join(" ")
    .toLocaleLowerCase();
}

function compareScores(a: Lead, b: Lead, direction: ScoreSort) {
  if (a.score === null && b.score === null) return 0;
  if (a.score === null) return 1;
  if (b.score === null) return -1;
  return direction === "desc" ? b.score - a.score : a.score - b.score;
}

function hasContact(lead: Lead) {
  return Boolean(lead.contacts?.trim() || lead.contact_email?.trim());
}

function compareLeads(a: Lead, b: Lead, sort: OpportunitySort) {
  if (sort === "score_asc") return compareScores(a, b, "asc");
  if (sort === "score_desc") return compareScores(a, b, "desc");

  const aHasContact = hasContact(a);
  const bHasContact = hasContact(b);
  if (aHasContact !== bHasContact) {
    const contactFirst = sort === "contact_present";
    return aHasContact === contactFirst ? -1 : 1;
  }
  return compareScores(a, b, "desc");
}

function parseOpportunitySort(value: string | null): OpportunitySort {
  if (value === "asc") return "score_asc";
  if (value === "contact_present" || value === "contact_missing") return value;
  return "score_desc";
}

function sortQueryValue(sort: OpportunitySort) {
  if (sort === "score_desc") return "";
  if (sort === "score_asc") return "asc";
  return sort;
}

function sortIcon(activeSort: OpportunitySort, column: "contact" | "score") {
  if (column === "contact") {
    if (activeSort === "contact_present") return <ArrowDown aria-hidden="true" size={14} />;
    if (activeSort === "contact_missing") return <ArrowUp aria-hidden="true" size={14} />;
  } else {
    if (activeSort === "score_desc") return <ArrowDown aria-hidden="true" size={14} />;
    if (activeSort === "score_asc") return <ArrowUp aria-hidden="true" size={14} />;
  }
  return <ArrowUpDown aria-hidden="true" size={14} />;
}

function uniqueOptions(values: Array<string | null>) {
  return [...new Set(values.filter((value): value is string => Boolean(value?.trim())))]
    .sort((a, b) => a.localeCompare(b));
}

function generationIsActive(lead: Lead) {
  return lead.latest_generation?.status === "queued" || lead.latest_generation?.status === "running";
}

function generationHasIssue(lead: Lead) {
  const status = lead.latest_generation?.status;
  return status === "insufficient_context" || status === "provider_error" || status === "system_error";
}

function matchesOutreachFilter(lead: Lead, filter: OutreachFilter) {
  if (!filter) return true;
  if (filter === "generating") return generationIsActive(lead);
  if (filter === "generation_issues") return generationHasIssue(lead);
  if (filter === "no_email") return !lead.current_email && !generationIsActive(lead);
  return lead.current_email?.status === filter;
}

function OpportunityOutreachBadges({ lead }: { lead: Lead }) {
  return (
    <span className={styles.outreachBadges}>
      {generationIsActive(lead) ? <StatusBadge status="generating" /> : null}
      {generationHasIssue(lead) ? <StatusBadge status="generation_issue" /> : null}
      {lead.current_email ? <StatusBadge status={lead.current_email.status} /> : null}
      {!lead.current_email && !generationIsActive(lead) && !generationHasIssue(lead)
        ? <StatusBadge status="no_email" />
        : null}
    </span>
  );
}

function OpportunityCard({
  lead,
  onDelete,
  deleting,
}: {
  lead: Lead;
  onDelete: (lead: Lead) => void;
  deleting: boolean;
}) {
  return (
    <article className={styles.mobileCard}>
      <div className={styles.mobileCardTopline}>
        <span className={styles.scoreBadge} aria-label={`Score ${formatScore(lead.score)}`}>
          {formatScore(lead.score)}
        </span>
        <span className={styles.muted}>{displayValue(lead.timing)}</span>
      </div>
      <div>
        <h2 className={styles.mobileCardTitle}>
          <Link to={`/opportunities/${lead.id}`}>{displayValue(lead.project)}</Link>
        </h2>
        <p className={styles.locationLine}>
          <MapPin aria-hidden="true" size={15} />
          {[lead.location, lead.state].filter(Boolean).join(", ") || "Location not provided"}
        </p>
      </div>
      <p className={styles.cardSummary}>{displayValue(lead.summary)}</p>
      <dl className={styles.cardMeta}>
        <div>
          <dt>Contact</dt>
          <dd>{lead.contacts || lead.contact_email || "Not provided"}</dd>
        </div>
        <div>
          <dt>Signal</dt>
          <dd>{displayValue(lead.signal)}</dd>
        </div>
        <div>
          <dt>Outreach</dt>
          <dd><OpportunityOutreachBadges lead={lead} /></dd>
        </div>
      </dl>
      <div className={styles.cardActions}>
        <Link className={styles.cardLink} to={`/opportunities/${lead.id}`}>
          View opportunity <span aria-hidden="true">→</span>
        </Link>
        <button
          type="button"
          className={styles.dangerButton}
          disabled={deleting}
          onClick={() => onDelete(lead)}
          aria-label={`Delete opportunity ${displayValue(lead.project)}`}
        >
          <Trash2 aria-hidden="true" size={15} />
          {deleting ? "Deleting…" : "Delete"}
        </button>
      </div>
    </article>
  );
}

export function OpportunitiesPage() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const queryClient = useQueryClient();
  const [pendingDeleteLead, setPendingDeleteLead] = useState<Lead | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const observedAutomaticSyncRef = useRef<
    { id: string; status: EarlyBidSyncRunStatus } | null | undefined
  >(undefined);
  const pendingAutomaticSyncRefreshRef = useRef<string | null>(null);

  const search = searchParams.get("q") ?? "";
  const state = searchParams.get("state") ?? "";
  const timing = searchParams.get("timing") ?? "";
  const outreachParam = searchParams.get("outreach") ?? "";
  const outreach: OutreachFilter = outreachOptions.some((option) => option.value === outreachParam)
    ? outreachParam as OutreachFilter
    : "";
  const opportunitySort = parseOpportunitySort(searchParams.get("sort"));

  const leadsQuery = useQuery({
    queryKey: queryKeys.leads,
    queryFn: api.listLeads,
  });

  const automaticSyncQuery = useQuery({
    queryKey: queryKeys.leadSyncStatus,
    queryFn: api.getLeadSyncStatus,
    refetchInterval: (query) => (
      automaticSyncIsActive(query.state.data?.latest_run?.status) ? 5_000 : 60_000
    ),
  });
  const latestAutomaticRun = automaticSyncQuery.data?.latest_run ?? null;

  useEffect(() => {
    if (!automaticSyncQuery.data) return;

    const previous = observedAutomaticSyncRef.current;
    const current = latestAutomaticRun
      ? { id: latestAutomaticRun.id, status: latestAutomaticRun.status }
      : null;
    if (
      current?.status === "succeeded"
      && (
        previous === undefined
        || previous === null
        || previous.id !== current.id
        || previous.status !== "succeeded"
      )
    ) {
      pendingAutomaticSyncRefreshRef.current = current.id;
    }
    observedAutomaticSyncRef.current = current;

    if (pendingAutomaticSyncRefreshRef.current && leadsQuery.fetchStatus === "idle") {
      pendingAutomaticSyncRefreshRef.current = null;
      void queryClient.invalidateQueries({ queryKey: queryKeys.leads });
    }
  }, [automaticSyncQuery.data, latestAutomaticRun, leadsQuery.fetchStatus, queryClient]);

  const syncMutation = useMutation({
    mutationFn: () => api.syncLeads(),
    onSuccess: async (result) => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.leads });
      toast.success("EarlyBid feed synchronized", {
        description: `${result.created} created · ${result.updated} updated · ${result.generation_queued} drafts queued`,
      });
    },
    onError: (error) => {
      toast.error("Could not synchronize EarlyBid", {
        description: errorMessage(error, "Please try again in a moment."),
      });
    },
  });

  const uploadMutation = useMutation({
    mutationFn: (file: File) => api.uploadLeadsCsv(file),
    onSuccess: async (uploaded) => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.leads });
      toast.success("CSV import complete", {
        description: `${uploaded.created} created · ${uploaded.updated} updated · ${uploaded.generation_queued} drafts queued`,
      });
    },
    onError: (error) => {
      toast.error("Could not import CSV", {
        description: errorMessage(error, "Check the file and try again."),
      });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (leadId: string) => api.deleteLead(leadId),
    onSuccess: async () => {
      setPendingDeleteLead(null);
      await queryClient.invalidateQueries({ queryKey: queryKeys.leads });
      toast.success("Opportunity deleted", {
        description: "The opportunity has been removed from this list.",
      });
    },
    onError: (error) => {
      toast.error("Could not delete opportunity", {
        description: errorMessage(error, "Please try again in a moment."),
      });
    },
  });

  const leads = useMemo(() => leadsQuery.data ?? [], [leadsQuery.data]);
  const stateOptions = useMemo(() => uniqueOptions(leads.map((lead) => lead.state)), [leads]);
  const timingOptions = useMemo(() => uniqueOptions(leads.map((lead) => lead.timing)), [leads]);

  const filteredLeads = useMemo(() => {
    const needle = search.trim().toLocaleLowerCase();
    return leads
      .filter((lead) => !needle || leadSearchText(lead).includes(needle))
      .filter((lead) => !state || lead.state === state)
      .filter((lead) => !timing || lead.timing === timing)
      .filter((lead) => matchesOutreachFilter(lead, outreach))
      .sort((a, b) => compareLeads(a, b, opportunitySort));
  }, [leads, opportunitySort, outreach, search, state, timing]);

  const updateParam = (name: string, value: string) => {
    const next = new URLSearchParams(searchParams);
    if (value) next.set(name, value);
    else next.delete(name);
    setSearchParams(next, { replace: true });
  };

  const clearFilters = () => setSearchParams({}, { replace: true });
  const hasFilters = Boolean(search || state || timing || outreach || searchParams.has("sort"));
  const isMutating = syncMutation.isPending || uploadMutation.isPending;
  const automaticSyncActive = automaticSyncIsActive(latestAutomaticRun?.status);
  const manualSyncDisabled = isMutating || automaticSyncActive;

  const handleDeleteLead = (lead: Lead) => {
    setPendingDeleteLead(lead);
  };

  const closeDeleteModal = () => {
    if (deleteMutation.isPending) return;
    setPendingDeleteLead(null);
  };

  const confirmDeleteLead = () => {
    if (!pendingDeleteLead) return;
    deleteMutation.mutate(pendingDeleteLead.id);
  };

  const actions = (
    <div className={styles.headerActions}>
      <input
        ref={fileInputRef}
        className={styles.fileInput}
        type="file"
        accept=".csv,text/csv"
        aria-label="Choose an EarlyBid CSV file"
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) uploadMutation.mutate(file);
          event.target.value = "";
        }}
      />
      <button
        className={styles.secondaryButton}
        type="button"
        disabled={isMutating}
        onClick={() => fileInputRef.current?.click()}
      >
        <FileUp aria-hidden="true" size={17} />
        {uploadMutation.isPending ? "Importing…" : "Import CSV"}
      </button>
      <button
        className={styles.primaryButton}
        type="button"
        disabled={manualSyncDisabled}
        aria-describedby="automatic-sync-status"
        onClick={() => syncMutation.mutate()}
      >
        <RefreshCw aria-hidden="true" className={syncMutation.isPending ? styles.spin : undefined} size={17} />
        {syncMutation.isPending ? "Synchronizing…" : "Sync EarlyBid"}
      </button>
    </div>
  );

  const automaticSyncPanel = (
    <AutomaticSyncStatusPanel
      status={automaticSyncQuery.data}
      pending={automaticSyncQuery.isPending}
      failed={automaticSyncQuery.isError}
      onRetry={() => void automaticSyncQuery.refetch()}
    />
  );

  if (leadsQuery.isPending) {
    return (
      <div className={styles.page}>
        <PageHeader
          eyebrow="Pipeline intelligence"
          title="Opportunities"
          description="Prioritized construction opportunities, ready for thoughtful outreach."
          actions={actions}
        />
        {automaticSyncPanel}
        <LoadingState label="Loading opportunities…" />
      </div>
    );
  }

  if (leadsQuery.isError) {
    return (
      <div className={styles.page}>
        <PageHeader
          eyebrow="Pipeline intelligence"
          title="Opportunities"
          description="Prioritized construction opportunities, ready for thoughtful outreach."
          actions={actions}
        />
        {automaticSyncPanel}
        <ErrorState
          title="Opportunities could not be loaded"
          message={errorMessage(leadsQuery.error, "Check the backend connection and try again.")}
          onRetry={() => void leadsQuery.refetch()}
        />
      </div>
    );
  }

  return (
    <div className={styles.page}>
      <PageHeader
        eyebrow="Pipeline intelligence"
        title="Opportunities"
        description="Prioritized construction opportunities, ready for thoughtful outreach."
        actions={actions}
      />

      {automaticSyncPanel}

      <p className={styles.srOnly} aria-live="polite">
        {syncMutation.isPending
          ? "Synchronizing the EarlyBid feed."
          : uploadMutation.isPending
            ? "Importing the selected CSV file."
            : ""}
      </p>

      {leads.length === 0 ? (
        <EmptyState
          title="No opportunities yet"
          description="Synchronize EarlyBid or import a CSV to create your first opportunity."
          action={(
            <button
              className={styles.primaryButton}
              type="button"
              disabled={manualSyncDisabled}
              aria-describedby="automatic-sync-status"
              onClick={() => syncMutation.mutate()}
            >
              <RefreshCw aria-hidden="true" size={17} />
              Sync EarlyBid
            </button>
          )}
        />
      ) : (
        <>
          <section className={styles.filterPanel} aria-label="Opportunity filters">
            <label className={styles.searchField}>
              <span className={styles.srOnly}>Search opportunities</span>
              <Search aria-hidden="true" size={18} />
              <input
                type="search"
                value={search}
                placeholder="Search project, location, contact or tag"
                onChange={(event) => updateParam("q", event.target.value)}
              />
            </label>

            <label className={styles.selectField}>
              <span>State</span>
              <select value={state} onChange={(event) => updateParam("state", event.target.value)}>
                <option value="">All states</option>
                {stateOptions.map((option) => (
                  <option key={option} value={option}>{option}</option>
                ))}
              </select>
            </label>

            <label className={styles.selectField}>
              <span>Timing</span>
              <select value={timing} onChange={(event) => updateParam("timing", event.target.value)}>
                <option value="">All timing</option>
                {timingOptions.map((option) => (
                  <option key={option} value={option}>{option}</option>
                ))}
              </select>
            </label>

            <label className={styles.selectField}>
              <span>Outreach</span>
              <select value={outreach} onChange={(event) => updateParam("outreach", event.target.value)}>
                {outreachOptions.map((option) => (
                  <option key={option.value || "all"} value={option.value}>{option.label}</option>
                ))}
              </select>
            </label>

            {hasFilters ? (
              <button className={styles.clearButton} type="button" onClick={clearFilters}>
                Clear
              </button>
            ) : null}
          </section>

          <div className={styles.resultSummary} aria-live="polite">
            <p>
              <strong>{filteredLeads.length}</strong> of {leads.length} opportunities
            </p>
            <ArrowUpDown aria-hidden="true" size={15} />
            <span>{sortSummaryLabels[opportunitySort]}</span>
          </div>

          {filteredLeads.length === 0 ? (
            <EmptyState
              title="No opportunities match"
              description="Try broadening your search or clearing one of the filters."
              action={(
                <button className={styles.secondaryButton} type="button" onClick={clearFilters}>
                  Clear filters
                </button>
              )}
            />
          ) : (
            <>
              <div className={styles.tableShell}>
                <table className={styles.table}>
                  <caption className={styles.srOnly}>EarlyBid opportunities</caption>
                  <thead>
                    <tr>
                      <th scope="col">Project</th>
                      <th scope="col">Location</th>
                      <th scope="col">Timing</th>
                      <th
                        scope="col"
                        className={styles.sortableColumn}
                        aria-sort={opportunitySort === "contact_present"
                          ? "descending"
                          : opportunitySort === "contact_missing"
                            ? "ascending"
                            : undefined}
                      >
                        <button
                          type="button"
                          className={styles.columnSortButton}
                          aria-label={opportunitySort === "contact_present"
                            ? "Sort contacts with missing first"
                            : "Sort contacts with provided first"}
                          onClick={() => updateParam(
                            "sort",
                            sortQueryValue(
                              opportunitySort === "contact_present"
                                ? "contact_missing"
                                : "contact_present",
                            ),
                          )}
                        >
                          Contact
                          {sortIcon(opportunitySort, "contact")}
                        </button>
                      </th>
                      <th scope="col">Outreach</th>
                      <th
                        scope="col"
                        className={`${styles.scoreColumn} ${styles.sortableColumn}`}
                        aria-sort={opportunitySort === "score_desc"
                          ? "descending"
                          : opportunitySort === "score_asc"
                            ? "ascending"
                            : undefined}
                      >
                        <button
                          type="button"
                          className={`${styles.columnSortButton} ${styles.scoreSortButton}`}
                          aria-label={opportunitySort === "score_desc"
                            ? "Sort score low to high"
                            : "Sort score high to low"}
                          onClick={() => updateParam(
                            "sort",
                            sortQueryValue(
                              opportunitySort === "score_desc" ? "score_asc" : "score_desc",
                            ),
                          )}
                        >
                          Score
                          {sortIcon(opportunitySort, "score")}
                        </button>
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredLeads.map((lead) => (
                      <tr
                        key={lead.id}
                        className={styles.clickableRow}
                        tabIndex={0}
                        aria-label={`Open opportunity ${displayValue(lead.project)}`}
                        onClick={() => navigate(`/opportunities/${lead.id}`)}
                        onKeyDown={(event) => {
                          if (event.key === "Enter" || event.key === " ") {
                            event.preventDefault();
                            navigate(`/opportunities/${lead.id}`);
                          }
                        }}
                      >
                        <td>
                          <span className={styles.projectLink}>
                            {displayValue(lead.project)}
                          </span>
                          <span className={styles.projectMeta}>
                            <Sparkles aria-hidden="true" size={13} />
                            {displayValue(lead.signal)}
                          </span>
                          <div className={styles.rowActions}>
                            <button
                              type="button"
                              className={styles.dangerButton}
                              disabled={deleteMutation.isPending && deleteMutation.variables === lead.id}
                              onClick={(event) => {
                                event.stopPropagation();
                                handleDeleteLead(lead);
                              }}
                              aria-label={`Delete opportunity ${displayValue(lead.project)}`}
                            >
                              <Trash2 aria-hidden="true" size={14} />
                              {deleteMutation.isPending && deleteMutation.variables === lead.id
                                ? "Deleting…"
                                : "Delete"}
                            </button>
                          </div>
                        </td>
                        <td>
                          <span className={styles.locationCell}>
                            <MapPin aria-hidden="true" size={15} />
                            <span>
                              {displayValue(lead.location)}
                              {lead.state ? " " : null}
                              {lead.state ? <small>{lead.state}</small> : null}
                            </span>
                          </span>
                        </td>
                        <td>{displayValue(lead.timing)}</td>
                        <td>
                          <span className={styles.contactCell}>
                            {lead.contacts || "Not provided"}
                            {lead.contact_email ? <small>{lead.contact_email}</small> : null}
                          </span>
                        </td>
                        <td><OpportunityOutreachBadges lead={lead} /></td>
                        <td className={styles.scoreColumn}>
                          <span className={styles.scoreBadge}>{formatScore(lead.score)}</span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              <div className={styles.mobileList}>
                {filteredLeads.map((lead) => (
                  <OpportunityCard
                    key={lead.id}
                    lead={lead}
                    onDelete={handleDeleteLead}
                    deleting={deleteMutation.isPending && deleteMutation.variables === lead.id}
                  />
                ))}
              </div>
            </>
          )}
        </>
      )}

      {pendingDeleteLead ? (
        <div className={styles.confirmationOverlay} onClick={closeDeleteModal}>
          <div
            className={styles.confirmationModal}
            role="dialog"
            aria-modal="true"
            aria-labelledby="delete-opportunity-title"
            onClick={(event) => event.stopPropagation()}
          >
            <h3 id="delete-opportunity-title">Delete opportunity?</h3>
            <p>
              <strong>{displayValue(pendingDeleteLead.project)}</strong>
              <br />
              {[pendingDeleteLead.location, pendingDeleteLead.state].filter(Boolean).join(", ") || "Location not provided"}
            </p>
            <p>
              This removes it from your current list. If it appears in a future feed sync, it may show up again.
            </p>
            <div className={styles.confirmationButtons}>
              <button
                type="button"
                className={styles.secondaryButton}
                onClick={closeDeleteModal}
                disabled={deleteMutation.isPending}
              >
                Cancel
              </button>
              <button
                type="button"
                className={styles.dangerButton}
                onClick={confirmDeleteLead}
                disabled={deleteMutation.isPending}
              >
                {deleteMutation.isPending ? "Deleting…" : "Delete opportunity"}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
