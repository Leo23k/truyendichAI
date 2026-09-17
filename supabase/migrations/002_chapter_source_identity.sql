alter table public.chapters
  drop constraint if exists chapters_novel_id_chapter_number_key;

do $$
begin
  if not exists (
    select 1
    from pg_constraint
    where conrelid = 'public.chapters'::regclass
      and conname = 'chapters_novel_source_url_key'
  ) then
    alter table public.chapters
      add constraint chapters_novel_source_url_key unique (novel_id, source_url);
  end if;
end $$;
