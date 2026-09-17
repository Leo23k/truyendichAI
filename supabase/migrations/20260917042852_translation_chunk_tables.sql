create table public.translation_queue (
  id uuid primary key default gen_random_uuid(),
  chapter_id bigint not null references public.chapters(id) on delete cascade,
  status text not null default 'queued'
    check (status in ('queued', 'translating', 'paused', 'completed', 'failed')),
  priority integer not null default 0,
  current_chunk integer not null default 0 check (current_chunk >= 0),
  total_chunks integer not null default 0 check (total_chunks >= 0),
  retry_count integer not null default 0 check (retry_count >= 0),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (chapter_id)
);

create table public.chapter_chunks (
  id uuid primary key default gen_random_uuid(),
  chapter_id bigint not null references public.chapters(id) on delete cascade,
  chunk_index integer not null check (chunk_index >= 0),
  original_text text not null,
  translated_text text,
  status text not null default 'queued'
    check (status in ('queued', 'translating', 'completed', 'failed')),
  unique (chapter_id, chunk_index)
);

create index translation_queue_status_priority_created_idx
  on public.translation_queue (status, priority desc, created_at asc);

alter table public.translation_queue enable row level security;
alter table public.chapter_chunks enable row level security;

revoke all on public.translation_queue from anon, authenticated;
revoke all on public.chapter_chunks from anon, authenticated;
