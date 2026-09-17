alter table public.novels
  add column if not exists source_url text,
  add column if not exists deleted_at timestamptz,
  add column if not exists delete_after timestamptz;

create index if not exists novels_active_updated_at_idx
  on public.novels (updated_at desc)
  where deleted_at is null;
