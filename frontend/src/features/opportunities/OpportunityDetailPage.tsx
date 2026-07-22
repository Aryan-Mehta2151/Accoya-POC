import { useMemo, useRef, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  CalendarDays,
  ExternalLink,
  Mail,
  MapPin,
  RotateCcw,
  Sparkles,
} from "lucide-react";
import { Link, useNavigate, useParams } from "react-router-dom";

import {
  EmptyState,
  ErrorState,
  GenerationOverlay,
  LoadingState,
  PageHeader,
} from "../../components/ui";
import { api, ApiError } from "../../lib/api";
import { queryKeys } from "../../lib/queryKeys";
import type { Email } from "../../types";
import styles from "./opportunities.module.css";

type GenerationIssue = {
  status: number | null;
  title: string;
  message: string;
  warnings: string[];
};

const scoreFormatter = new Intl.NumberFormat(undefined, {
  maximumFractionDigits: 1,
});

const dateFormatter = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
  year: "numeric",
});

const dateTimeFormatter = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
  year: "numeric",
  hour: "numeric",
  minute: "2-digit",
});

function formatDate(value: string | null, includeTime = false) {
  if (!value) return "Not provided";
  const dateOnly = /^([0-9]{4})-([0-9]{2})-([0-9]{2})$/.exec(value);
  const parsed = dateOnly
    ? new Date(Number(dateOnly[1]), Number(dateOnly[2]) - 1, Number(dateOnly[3]))
    : new Date(value);
  if (Number.isNaN(parsed.valueOf())) return value;
  return includeTime ? dateTimeFormatter.format(parsed) : dateFormatter.format(parsed);
}

function textValue(value: string | null | undefined) {
  return value?.trim() || "Not provided";
}

function safeExternalUrl(value: string | null) {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:" ? url.toString() : null;
  } catch {
    return null;
  }
}

function apiStatus(error: unknown) {
  return error instanceof ApiError ? error.status : null;
}

function apiWarnings(error: unknown) {
  if (!(error instanceof ApiError)) return [];
  const warnings = (error as ApiError & { warnings?: unknown }).warnings;
  return Array.isArray(warnings) ? warnings.filter((item): item is string => typeof item === "string") : [];
}

function generationIssueFor(error: unknown): GenerationIssue {
  const status = apiStatus(error);
  const warnings = apiWarnings(error);
  if (status === 422) {
    return {
      status,
      title: "More context is needed",
      message: "This opportunity does not contain enough information to create a useful outreach email yet.",
      warnings,
    };
  }
  if (status === 502) {
    return {
      status,
      title: "Email generation is temporarily unavailable",
      message: "The drafting service could not complete this request. Your opportunity is safe—please try again.",
      warnings,
    };
  }
  return {
    status,
    title: "We couldn’t generate this email",
    message: "Something interrupted the request. Please try again in a moment.",
    warnings,
  };
}

function statusLabel(value: Email["status"]) {
  return value.replace("_", " ");
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
  const tags = value?.split(",").map((tag) => tag.trim()).filter(Boolean) ?? [];
  if (!tags.length) return <>Not provided</>;
  return (
    <span className={styles.tagList}>
      {tags.map((tag) => <span className={styles.tag} key={tag}>{tag}</span>)}
    </span>
  );
}

export function OpportunityDetailPage() {
  const { leadId } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const generationLock = useRef(false);
  const [generationIssue, setGenerationIssue] = useState<GenerationIssue | null>(null);

  const leadsQuery = useQuery({
    queryKey: queryKeys.leads,
    queryFn: api.listLeads,
  });
  const emailsQuery = useQuery({
    queryKey: queryKeys.emails,
    queryFn: api.listEmails,
  });

  const lead = useMemo(
    () => leadsQuery.data?.find((candidate) => candidate.id === leadId),
    [leadId, leadsQuery.data],
  );
  const relatedEmails = useMemo(
    () => emailsQuery.data?.filter((email) => email.lead_id === leadId) ?? [],
    [emailsQuery.data, leadId],
  );

  const generationMutation = useMutation({
    mutationFn: () => {
      if (!leadId) throw new Error("Opportunity ID is missing");
      return api.generateEmail(leadId);
    },
    onMutate: () => setGenerationIssue(null),
    onSuccess: (email) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.emails });
      navigate(`/outreach/${email.id}`);
    },
    onError: (error) => setGenerationIssue(generationIssueFor(error)),
    onSettled: () => {
      generationLock.current = false;
    },
  });

  const generateOutreach = () => {
    if (generationLock.current || generationMutation.isPending) return;
    generationLock.current = true;
    generationMutation.mutate();
  };

  if (leadsQuery.isPending) {
    return (
      <div className={styles.page}>
        <LoadingState label="Loading opportunity…" />
      </div>
    );
  }

  if (leadsQuery.isError) {
    return (
      <div className={styles.page}>
        <ErrorState
          title="Opportunity could not be loaded"
          message="Check the backend connection and try again."
          onRetry={() => void leadsQuery.refetch()}
        />
      </div>
    );
  }

  if (!lead) {
    return (
      <div className={styles.page}>
        <EmptyState
          title="Opportunity not found"
          description="It may have been removed or the link may be out of date."
          action={<Link className={styles.primaryButton} to="/opportunities">Back to opportunities</Link>}
        />
      </div>
    );
  }

  const sourceUrl = safeExternalUrl(lead.url);
  const generateAction = (
    <button
      className={styles.primaryButton}
      type="button"
      disabled={generationMutation.isPending}
      onClick={generateOutreach}
    >
      <Sparkles aria-hidden="true" size={17} />
      {generationMutation.isPending ? "Generating…" : "Generate outreach"}
    </button>
  );

  return (
    <div className={styles.page}>
      <Link className={styles.backLink} to="/opportunities">
        <ArrowLeft aria-hidden="true" size={16} />
        All opportunities
      </Link>

      <PageHeader
        eyebrow={lead.section || "Opportunity intelligence"}
        title={lead.project || "Untitled opportunity"}
        description={lead.summary || "Review the available context before creating outreach."}
        actions={generateAction}
      />

      <div className={styles.detailHighlights}>
        <div>
          <MapPin aria-hidden="true" size={18} />
          <span>{[lead.location, lead.state].filter(Boolean).join(", ") || "Location not provided"}</span>
        </div>
        <div>
          <CalendarDays aria-hidden="true" size={18} />
          <span>{textValue(lead.timing)}</span>
        </div>
        <div>
          <span className={styles.highlightScore}>{lead.score === null ? "—" : scoreFormatter.format(lead.score)}</span>
          <span>Priority score</span>
        </div>
      </div>

      {generationIssue ? (
        <section className={styles.generationIssue} role="alert" aria-labelledby="generation-issue-title">
          <div>
            <p className={styles.sectionEyebrow}>Outreach draft</p>
            <h2 id="generation-issue-title">{generationIssue.title}</h2>
            <p>{generationIssue.message}</p>
            {generationIssue.warnings.length ? (
              <ul>
                {generationIssue.warnings.map((warning) => <li key={warning}>{warning}</li>)}
              </ul>
            ) : null}
          </div>
          {generationIssue.status === 422 ? null : (
            <button
              className={styles.secondaryButton}
              type="button"
              disabled={generationMutation.isPending}
              onClick={generateOutreach}
            >
              <RotateCcw aria-hidden="true" size={16} />
              Try again
            </button>
          )}
        </section>
      ) : null}

      <div className={styles.detailLayout}>
        <main className={styles.detailMain}>
          <section className={styles.detailSection} aria-labelledby="opportunity-overview">
            <div className={styles.sectionHeading}>
              <p className={styles.sectionEyebrow}>Project profile</p>
              <h2 id="opportunity-overview">Opportunity overview</h2>
            </div>
            <dl className={styles.detailGrid}>
              <DetailItem label="Project">{textValue(lead.project)}</DetailItem>
              <DetailItem label="Section">{textValue(lead.section)}</DetailItem>
              <DetailItem label="Location">{textValue(lead.location)}</DetailItem>
              <DetailItem label="State">{textValue(lead.state)}</DetailItem>
              <DetailItem label="Timing">{textValue(lead.timing)}</DetailItem>
              <DetailItem label="Meeting date">{formatDate(lead.meeting_date)}</DetailItem>
              <DetailItem label="Awarded to">{textValue(lead.awarded_to)}</DetailItem>
              <DetailItem label="Score">{lead.score === null ? "Not provided" : scoreFormatter.format(lead.score)}</DetailItem>
              <DetailItem label="Summary" wide>{textValue(lead.summary)}</DetailItem>
            </dl>
          </section>

          <section className={styles.detailSection} aria-labelledby="opportunity-intelligence">
            <div className={styles.sectionHeading}>
              <p className={styles.sectionEyebrow}>Signals</p>
              <h2 id="opportunity-intelligence">Opportunity intelligence</h2>
            </div>
            <dl className={styles.detailGrid}>
              <DetailItem label="Signal" wide>{textValue(lead.signal)}</DetailItem>
              <DetailItem label="Intelligence" wide>{textValue(lead.intelligence)}</DetailItem>
              <DetailItem label="Priority reasons" wide>{textValue(lead.priority_reasons)}</DetailItem>
              <DetailItem label="Tags" wide><Tags value={lead.tags} /></DetailItem>
            </dl>
          </section>

          <section className={styles.detailSection} aria-labelledby="opportunity-source">
            <div className={styles.sectionHeading}>
              <p className={styles.sectionEyebrow}>Record</p>
              <h2 id="opportunity-source">Source information</h2>
            </div>
            <dl className={styles.detailGrid}>
              <DetailItem label="External ID">{textValue(lead.external_id)}</DetailItem>
              <DetailItem label="Record ID"><span className={styles.monoValue}>{lead.id}</span></DetailItem>
              <DetailItem label="Source feed">{textValue(lead.source_feed)}</DetailItem>
              <DetailItem label="Created">{formatDate(lead.created_at, true)}</DetailItem>
              <DetailItem label="Source URL" wide>
                {sourceUrl ? (
                  <a className={styles.inlineLink} href={sourceUrl} target="_blank" rel="noreferrer">
                    Open original opportunity <ExternalLink aria-hidden="true" size={14} />
                  </a>
                ) : textValue(lead.url)}
              </DetailItem>
            </dl>
          </section>
        </main>

        <aside className={styles.detailSidebar} aria-label="Opportunity contact and outreach">
          <section className={styles.sideCard}>
            <p className={styles.sectionEyebrow}>Primary contact</p>
            <h2>{lead.contacts || "Contact not provided"}</h2>
            {lead.contact_email ? (
              <a className={styles.contactLink} href={`mailto:${lead.contact_email}`}>
                <Mail aria-hidden="true" size={16} />
                {lead.contact_email}
              </a>
            ) : <p className={styles.muted}>No email address is available.</p>}
            <dl className={styles.compactDetails}>
              <div>
                <dt>Contacts field</dt>
                <dd>{textValue(lead.contacts)}</dd>
              </div>
              <div>
                <dt>Contact email</dt>
                <dd>{textValue(lead.contact_email)}</dd>
              </div>
            </dl>
          </section>

          <section className={styles.sideCard}>
            <div className={styles.sideCardHeader}>
              <div>
                <p className={styles.sectionEyebrow}>Outreach</p>
                <h2>Related emails</h2>
              </div>
              <span className={styles.countBadge}>{relatedEmails.length}</span>
            </div>

            {emailsQuery.isPending ? (
              <p className={styles.muted}>Loading emails…</p>
            ) : emailsQuery.isError ? (
              <div className={styles.inlineError} role="alert">
                <p>Related emails could not be loaded.</p>
                <button type="button" onClick={() => void emailsQuery.refetch()}>Try again</button>
              </div>
            ) : relatedEmails.length ? (
              <ul className={styles.emailList}>
                {relatedEmails.map((email) => (
                  <li key={email.id}>
                    <Link to={`/outreach/${email.id}`}>
                      <span>{email.subject || "Untitled email"}</span>
                      <small>
                        <span className={styles.statusDot} data-status={email.status} />
                        {statusLabel(email.status)} · {formatDate(email.updated_at)}
                      </small>
                    </Link>
                  </li>
                ))}
              </ul>
            ) : (
              <p className={styles.muted}>No outreach has been generated for this opportunity.</p>
            )}

            <div className={styles.sideCardAction}>{generateAction}</div>
          </section>
        </aside>
      </div>

      <GenerationOverlay
        open={generationMutation.isPending}
        title="Generating outreach…"
        description="Creating a thoughtful draft from this opportunity and your strategy context."
      />
    </div>
  );
}
