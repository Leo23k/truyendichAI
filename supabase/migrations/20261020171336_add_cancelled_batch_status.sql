alter table public.batch_translation_jobs
  drop constraint if exists batch_translation_jobs_status_check;

alter table public.batch_translation_jobs
  add constraint batch_translation_jobs_status_check
  check (status in ('queued', 'running', 'paused', 'completed', 'failed', 'cancelled'));
