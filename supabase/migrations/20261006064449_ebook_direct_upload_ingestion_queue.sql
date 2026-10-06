create table if not exists public.ebook_ingestion_queue (
  id uuid primary key,
  owner_id uuid not null references auth.users(id) on delete cascade,
  file_name text not null,
  source_format text not null check (source_format in ('epub', 'txt', 'html', 'pdf')),
  original_file_path text not null unique,
  expected_size bigint not null check (expected_size > 0 and expected_size <= 104857600),
  translate_if_chinese boolean not null default true,
  source_language text check (source_language in ('zh', 'vi', 'unknown')),
  status text not null default 'awaiting_upload'
    check (status in ('awaiting_upload', 'queued', 'processing', 'completed', 'failed')),
  translation_queued integer not null default 0 check (translation_queued >= 0),
  error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists ebook_ingestion_queue_status_created_idx
  on public.ebook_ingestion_queue (status, created_at)
  where status in ('queued', 'processing');

alter table public.ebook_ingestion_queue enable row level security;
revoke all on public.ebook_ingestion_queue from public, anon, authenticated;
grant all on public.ebook_ingestion_queue to service_role;
