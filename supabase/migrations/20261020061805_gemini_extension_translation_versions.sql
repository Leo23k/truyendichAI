-- Keep the existing queue as the source of truth while adding the metadata the
-- browser companion needs to resume work and preserve prior translations.
alter table public.translation_queue
  add column source_hash text,
  add column combined_hash text,
  add column input_chars integer not null default 0 check (input_chars >= 0),
  add column output_chars integer not null default 0 check (output_chars >= 0),
  add column error_code text,
  add column error_message text,
  add column started_at timestamptz,
  add column completed_at timestamptz;

alter table public.translation_queue
  drop constraint if exists translation_queue_status_check;
alter table public.translation_queue
  add constraint translation_queue_status_check
  check (status in (
    'queued', 'translating', 'backoff', 'paused', 'paused_quota',
    'paused_manual', 'review_required', 'completed', 'failed', 'cancelled'
  ));

create index translation_queue_gemini_extension_idx
  on public.translation_queue (status, created_at asc)
  where provider = 'gemini_web';

create table public.translation_versions (
  id uuid primary key default gen_random_uuid(),
  chapter_id bigint not null references public.chapters(id) on delete cascade,
  batch_job_id uuid references public.batch_translation_jobs(id) on delete set null,
  version_number integer not null check (version_number > 0),
  provider text not null check (provider in ('hachimi', 'gemini_api', 'gemini_web')),
  model text not null,
  prompt_id uuid references public.translation_prompts(id) on delete set null,
  prompt_name text not null,
  prompt_snapshot text not null,
  source_hash text not null,
  combined_hash text not null,
  translated_title text not null,
  translated_content text not null,
  is_active boolean not null default false,
  created_at timestamptz not null default now(),
  unique (chapter_id, version_number)
);

create unique index translation_versions_one_active_idx
  on public.translation_versions (chapter_id)
  where is_active;
create index translation_versions_chapter_history_idx
  on public.translation_versions (chapter_id, version_number desc);

alter table public.translation_versions enable row level security;
revoke all on public.translation_versions from anon, authenticated;
grant all on public.translation_versions to service_role;
