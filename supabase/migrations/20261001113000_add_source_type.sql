alter table public.novels
  add column if not exists source_type text not null default 'zh'
  check (source_type in ('zh', 'vi'));

alter table public.remote_crawl_jobs
  add column if not exists source_type text not null default 'zh'
  check (source_type in ('zh', 'vi'));
