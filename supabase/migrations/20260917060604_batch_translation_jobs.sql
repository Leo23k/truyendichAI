create table public.batch_translation_jobs (
  id uuid primary key default gen_random_uuid(),
  novel_id bigint not null references public.novels(id) on delete cascade,
  from_chapter integer not null check (from_chapter >= 1),
  to_chapter integer not null check (to_chapter >= from_chapter),
  total integer not null default 0 check (total >= 0),
  completed integer not null default 0 check (completed >= 0),
  failed integer not null default 0 check (failed >= 0),
  status text not null default 'queued'
    check (status in ('queued', 'running', 'paused', 'completed', 'failed')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.translation_queue
  add column batch_job_id uuid references public.batch_translation_jobs(id) on delete set null;

create index batch_translation_jobs_active_idx
  on public.batch_translation_jobs (status, created_at desc);

create index translation_queue_batch_job_idx
  on public.translation_queue (batch_job_id, status, priority desc);

alter table public.batch_translation_jobs enable row level security;
revoke all on public.batch_translation_jobs from anon, authenticated;
