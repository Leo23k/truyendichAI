-- Ownership makes pause/resume/delete apply only to work created by this crawl.
alter table public.translation_queue
  add column crawl_job_id uuid references public.remote_crawl_jobs(id) on delete cascade;

alter table public.remote_crawl_jobs
  drop constraint if exists remote_crawl_jobs_status_check;

alter table public.remote_crawl_jobs
  add constraint remote_crawl_jobs_status_check
  check (status in ('queued', 'crawling', 'syncing', 'translating', 'paused', 'deleting', 'completed', 'failed'));

create index translation_queue_crawl_job_status_idx
  on public.translation_queue (crawl_job_id, status, priority desc, created_at asc)
  where crawl_job_id is not null;

create index remote_crawl_jobs_active_updated_idx
  on public.remote_crawl_jobs (updated_at desc)
  where status in ('queued', 'crawling', 'syncing', 'translating', 'paused', 'failed');
