export type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };

export type LeadReviewStatus = 'active' | 'deleted';

export type Lead = {
  id: string;
  external_id: string;
  section: string | null;
  project: string | null;
  location: string | null;
  state: string | null;
  signal: string | null;
  intelligence: string | null;
  score: number | null;
  timing: string | null;
  awarded_to: string | null;
  priority_reasons: string | null;
  summary: string | null;
  contacts: string | null;
  contact_email: string | null;
  meeting_date: string | null;
  tags: string | null;
  url: string | null;
  reported: JsonValue | null;
  due_date: string | null;
  award_date: string | null;
  start_date: string | null;
  response_deadline_evidence: JsonValue | null;
  keywords_matched: string[];
  review_status: LeadReviewStatus | null;
  deleted_by: 'client' | 'ai' | 'operator' | null;
  deleted_reasons: string[];
  source_feed: string | null;
  created_at: string;
  current_email?: EmailSummary | null;
  latest_generation?: EmailGenerationJob | null;
};

export type SyncResult = {
  created: number;
  updated: number;
  total: number;
  feed: string;
  generation_queued: number;
};

export type CsvUploadResult = {
  items: Lead[];
  created: number;
  updated: number;
  total: number;
  generation_queued: number;
};

export type EarlyBidSyncRunStatus =
  | 'queued'
  | 'running'
  | 'retry_wait'
  | 'succeeded'
  | 'failed';

export type EarlyBidSyncRun = {
  id: string;
  feed: string;
  schedule_date: string;
  scheduled_for: string;
  status: EarlyBidSyncRunStatus;
  attempt_count: number;
  error_code: string | null;
  next_attempt_at: string | null;
  created: number;
  updated: number;
  total: number;
  generation_queued: number;
  claimed_at: string | null;
  completed_at: string | null;
};

export type EarlyBidSyncStatus = {
  timezone: string;
  next_scheduled_at: string;
  overdue: boolean;
  latest_run: EarlyBidSyncRun | null;
};

export type EmailStatus =
  | 'draft'
  | 'pending_review'
  | 'approved'
  | 'sent'
  | 'rejected';

export type EmailDeliveryJobStatus =
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'delivery_unknown';

export type EmailDeliveryJob = {
  id: string;
  email_id: string;
  retry_of_job_id: string | null;
  status: EmailDeliveryJobStatus;
  requested_by: string;
  idempotency_key: string;
  content_hash: string;
  message_id: string;
  sender_email: string;
  recipient_email: string;
  subject: string;
  body_snapshot: string;
  error_code: string | null;
  attempt_count: number;
  queued_at: string;
  claimed_at: string | null;
  heartbeat_at: string | null;
  send_started_at: string | null;
  accepted_at: string | null;
  completed_at: string | null;
};

export type Email = {
  id: string;
  lead_id: string;
  recipient_email: string | null;
  subject: string;
  body: string;
  signature: string | null;
  rendered_body: string;
  status: EmailStatus;
  latest_delivery: EmailDeliveryJob | null;
  has_unknown_delivery: boolean;
  delivery_content_hash: string;
  created_at: string;
  updated_at: string;
};

export type EmailSummary = {
  id: string;
  status: EmailStatus;
  recipient_email: string | null;
  created_at: string;
  updated_at: string;
};

export type EmailGenerationJobStatus =
  | 'queued'
  | 'running'
  | 'generated'
  | 'insufficient_context'
  | 'provider_error'
  | 'system_error';

export type EmailGenerationTrigger =
  | 'earlybid_sync'
  | 'csv_upload'
  | 'manual'
  | 'retry';

export type EmailGenerationJob = {
  id: string;
  lead_id: string;
  retry_of_job_id: string | null;
  agent_run_id: string | null;
  trigger: EmailGenerationTrigger;
  status: EmailGenerationJobStatus;
  requested_input_hash: string;
  idempotency_key: string;
  error_code: string | null;
  attempt_count: number;
  queued_at: string;
  claimed_at: string | null;
  heartbeat_at: string | null;
  completed_at: string | null;
};

export type LeadWorkspace = {
  lead: Lead;
  emails: Email[];
  default_email_signature: string;
  current_email_id: string | null;
  current_email_is_stale: boolean;
  latest_generation: EmailGenerationJob | null;
};

export type StrategyDocument = {
  id: string;
  s3_key: string;
  filename: string;
  size?: number;
  last_modified?: string;
  url?: string | null;
};

export type ChatMessage = {
  role: 'user' | 'assistant';
  content: string;
  created_at?: string;
  sources?: string[];
};

export type ChatResponse = {
  session_id: string;
  answer: string;
  sources: string[];
};

export type ChatSession = {
  session_id: string;
  message_count: number;
  last_message_at: string;
};
