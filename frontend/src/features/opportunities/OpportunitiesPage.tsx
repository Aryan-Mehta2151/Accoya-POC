import { useMemo, useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  FileUp,
  MapPin,
  RefreshCw,
  Search,
  Sparkles,
} from "lucide-react";
import { Link, useSearchParams } from "react-router-dom";
import { toast } from "sonner";

import { EmptyState, ErrorState, LoadingState, PageHeader } from "../../components/ui";
import { api, ApiError } from "../../lib/api";
import { queryKeys } from "../../lib/queryKeys";
import type { Lead } from "../../types";
import styles from "./opportunities.module.css";

type ScoreSort = "desc" | "asc";

const scoreFormatter = new Intl.NumberFormat(undefined, {
  maximumFractionDigits: 1,
});

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

function uniqueOptions(values: Array<string | null>) {
  return [...new Set(values.filter((value): value is string => Boolean(value?.trim())))]
    .sort((a, b) => a.localeCompare(b));
}

function OpportunityCard({ lead }: { lead: Lead }) {
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
      </dl>
      <Link className={styles.cardLink} to={`/opportunities/${lead.id}`}>
        View opportunity <span aria-hidden="true">→</span>
      </Link>
    </article>
  );
}

export function OpportunitiesPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const queryClient = useQueryClient();
  const fileInputRef = useRef<HTMLInputElement>(null);

  const search = searchParams.get("q") ?? "";
  const state = searchParams.get("state") ?? "";
  const timing = searchParams.get("timing") ?? "";
  const scoreSort: ScoreSort = searchParams.get("sort") === "asc" ? "asc" : "desc";

  const leadsQuery = useQuery({
    queryKey: queryKeys.leads,
    queryFn: api.listLeads,
  });

  const syncMutation = useMutation({
    mutationFn: () => api.syncLeads(),
    onSuccess: async (result) => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.leads });
      toast.success("EarlyBid feed synchronized", {
        description: `${result.created} created · ${result.updated} updated`,
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
        description: `${uploaded.length} ${uploaded.length === 1 ? "opportunity" : "opportunities"} imported.`,
      });
    },
    onError: (error) => {
      toast.error("Could not import CSV", {
        description: errorMessage(error, "Check the file and try again."),
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
      .sort((a, b) => compareScores(a, b, scoreSort));
  }, [leads, scoreSort, search, state, timing]);

  const updateParam = (name: string, value: string) => {
    const next = new URLSearchParams(searchParams);
    if (value) next.set(name, value);
    else next.delete(name);
    setSearchParams(next, { replace: true });
  };

  const clearFilters = () => setSearchParams({}, { replace: true });
  const hasFilters = Boolean(search || state || timing || searchParams.has("sort"));
  const isMutating = syncMutation.isPending || uploadMutation.isPending;

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
        disabled={isMutating}
        onClick={() => syncMutation.mutate()}
      >
        <RefreshCw aria-hidden="true" className={syncMutation.isPending ? styles.spin : undefined} size={17} />
        {syncMutation.isPending ? "Synchronizing…" : "Sync EarlyBid"}
      </button>
    </div>
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
              disabled={isMutating}
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

            <button
              className={styles.sortButton}
              type="button"
              onClick={() => updateParam("sort", scoreSort === "desc" ? "asc" : "")}
              aria-label={`Sort score ${scoreSort === "desc" ? "ascending" : "descending"}`}
            >
              {scoreSort === "desc" ? <ArrowDown aria-hidden="true" size={17} /> : <ArrowUp aria-hidden="true" size={17} />}
              Score {scoreSort === "desc" ? "high to low" : "low to high"}
            </button>

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
            <span>Sorted by score</span>
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
                      <th scope="col">Contact</th>
                      <th scope="col" className={styles.scoreColumn}>Score</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredLeads.map((lead) => (
                      <tr key={lead.id}>
                        <td>
                          <Link className={styles.projectLink} to={`/opportunities/${lead.id}`}>
                            {displayValue(lead.project)}
                          </Link>
                          <span className={styles.projectMeta}>
                            <Sparkles aria-hidden="true" size={13} />
                            {displayValue(lead.signal)}
                          </span>
                        </td>
                        <td>
                          <span className={styles.locationCell}>
                            <MapPin aria-hidden="true" size={15} />
                            <span>
                              {displayValue(lead.location)}
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
                        <td className={styles.scoreColumn}>
                          <span className={styles.scoreBadge}>{formatScore(lead.score)}</span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              <div className={styles.mobileList}>
                {filteredLeads.map((lead) => <OpportunityCard key={lead.id} lead={lead} />)}
              </div>
            </>
          )}
        </>
      )}
    </div>
  );
}
