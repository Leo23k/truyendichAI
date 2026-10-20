alter table public.translation_queue
  add column attempts integer not null default 0 check (attempts >= 0);
