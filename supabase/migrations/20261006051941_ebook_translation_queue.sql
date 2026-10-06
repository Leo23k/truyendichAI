alter table public.ebook_chapters
  add column if not exists translated_content_path text;

create table if not exists public.ebook_translation_queue (
  id uuid primary key default gen_random_uuid(),
  ebook_chapter_id bigint not null references public.ebook_chapters(id) on delete cascade,
  status text not null default 'queued'
    check (status in ('queued', 'translating', 'completed', 'failed')),
  current_chunk integer not null default 0 check (current_chunk >= 0),
  total_chunks integer not null default 0 check (total_chunks >= 0),
  retry_count integer not null default 0 check (retry_count >= 0),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (ebook_chapter_id)
);

create index if not exists ebook_translation_queue_status_idx
  on public.ebook_translation_queue (status, created_at asc);

create table if not exists public.ebook_translation_chunks (
  id uuid primary key default gen_random_uuid(),
  ebook_chapter_id bigint not null references public.ebook_chapters(id) on delete cascade,
  chunk_index integer not null check (chunk_index >= 0),
  original_text text not null,
  translated_text text,
  status text not null default 'queued'
    check (status in ('queued', 'completed', 'failed')),
  unique (ebook_chapter_id, chunk_index)
);

create index if not exists ebook_translation_chunks_order_idx
  on public.ebook_translation_chunks (ebook_chapter_id, chunk_index);

alter table public.ebook_translation_queue enable row level security;
alter table public.ebook_translation_chunks enable row level security;
revoke all on public.ebook_translation_queue, public.ebook_translation_chunks from anon, authenticated;
grant all on public.ebook_translation_queue, public.ebook_translation_chunks to service_role;
