alter table public.novels
  add column original_author text,
  add column original_description text,
  add column metadata_translated boolean not null default true;
