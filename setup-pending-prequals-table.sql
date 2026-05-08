-- Create pending_prequals_cloud table for automatic prequal status tracking
CREATE TABLE IF NOT EXISTS public.pending_prequals_cloud (
  thread_id       text PRIMARY KEY,
  agency_name     text NOT NULL,
  status          text NOT NULL,
  submitted_date  date,
  requested_by    text,
  last_message_at timestamptz,
  last_sender     text,
  gmail_thread_id text,
  notes           text,
  updated_at      timestamptz DEFAULT now()
);

-- Create email_briefing_cloud table for inbox summary
CREATE TABLE IF NOT EXISTS public.email_briefing_cloud (
  id              text PRIMARY KEY,
  generated_at    timestamptz NOT NULL DEFAULT now(),
  subject         text,
  sender_name     text,
  sender_email    text,
  received_at     timestamptz,
  snippet         text,
  summary         text,
  suggested_reply text,
  thread_id       text,
  label_ids       text[],
  is_dismissed    boolean DEFAULT false,
  created_at      timestamptz DEFAULT now(),
  updated_at      timestamptz DEFAULT now()
);

-- Indexes
CREATE INDEX idx_pending_prequals_last_message ON pending_prequals_cloud(last_message_at DESC);
CREATE INDEX idx_email_briefing_dismissed ON email_briefing_cloud(is_dismissed);
CREATE INDEX idx_email_briefing_received ON email_briefing_cloud(received_at DESC);

-- RLS
ALTER TABLE pending_prequals_cloud ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_briefing_cloud ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Allow anon read" ON pending_prequals_cloud FOR SELECT TO anon USING (true);
CREATE POLICY "Allow service read/write" ON pending_prequals_cloud USING (true) WITH CHECK (true);

CREATE POLICY "Allow anon read" ON email_briefing_cloud FOR SELECT TO anon USING (true);
CREATE POLICY "Allow service read/write" ON email_briefing_cloud USING (true) WITH CHECK (true);
