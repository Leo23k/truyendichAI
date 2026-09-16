alter table public.chapters
  drop constraint if exists chapters_novel_id_chapter_number_key;

alter table public.chapters
  add constraint chapters_novel_source_url_key unique (novel_id, source_url);
