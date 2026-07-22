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
  source_feed: string | null;
  created_at: string;
};

export type SyncResult = {
  created: number;
  updated: number;
  total: number;
  feed: string;
};

export type EmailStatus =
  | 'draft'
  | 'pending_review'
  | 'approved'
  | 'sent'
  | 'rejected';

export type Email = {
  id: string;
  lead_id: string;
  subject: string;
  body: string;
  status: EmailStatus;
  created_at: string;
  updated_at: string;
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
