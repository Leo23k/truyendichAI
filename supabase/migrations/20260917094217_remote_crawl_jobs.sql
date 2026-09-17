create table public.remote_crawl_jobs (
  id uuid primary key default gen_random_uuid(),
  source_url text not null,
  status text not null default 'queued'
    check (status in ('queued', 'crawling', 'syncing', 'translating', 'completed', 'failed')),
  error text,
  result jsonb,
  started_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index remote_crawl_jobs_queued_idx
  on public.remote_crawl_jobs (created_at)
  where status = 'queued';

alter table public.remote_crawl_jobs enable row level security;
revoke all on table public.remote_crawl_jobs from anon, authenticated;
