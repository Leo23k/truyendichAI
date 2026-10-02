-- Existing imports can contain duplicate chapter numbers.  Do not delete
-- historical rows in a schema migration: a targeted repair can re-crawl them
-- safely.  Reject future duplicates instead, before two URLs can overwrite
-- the same numbered JSON chapter file.
create or replace function public.reject_duplicate_chapter_number()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  if exists (
    select 1
    from public.chapters as chapter
    where chapter.novel_id = new.novel_id
      and chapter.chapter_number = new.chapter_number
      and chapter.id is distinct from new.id
  ) then
    raise exception 'A novel can only contain one row for chapter number %.', new.chapter_number
      using errcode = 'unique_violation';
  end if;
  return new;
end;
$$;

revoke all on function public.reject_duplicate_chapter_number() from public, anon, authenticated;

drop trigger if exists chapters_reject_duplicate_number on public.chapters;
create trigger chapters_reject_duplicate_number
before insert or update of novel_id, chapter_number on public.chapters
for each row execute function public.reject_duplicate_chapter_number();
