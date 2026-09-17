create unique index remote_crawl_jobs_one_active_source_idx
  on public.remote_crawl_jobs (source_url)
  where status in ('queued', 'crawling', 'syncing', 'translating');
