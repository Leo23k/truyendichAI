-- Chunks are transient worker state.  The PC worker recreates them from the
-- chapter JSON raw content after this reset marks total_chunks as zero.
create or replace function public.reset_retranslation(
  p_novel_id bigint,
  p_chapter_ids bigint[] default null
)
returns table (chapter_count integer, blocked boolean)
language plpgsql
security invoker
set search_path = public
as $$
declare
  target_ids bigint[];
begin
  select array_agg(chapter.id order by chapter.id)
    into target_ids
  from public.chapters as chapter
  where chapter.novel_id = p_novel_id
    and (p_chapter_ids is null or chapter.id = any(p_chapter_ids));

  if coalesce(cardinality(target_ids), 0) = 0 then
    return query select 0, false;
    return;
  end if;

  perform 1
  from public.translation_queue as queue
  where queue.chapter_id = any(target_ids)
  for update;

  if exists (
    select 1
    from public.translation_queue as queue
    where queue.chapter_id = any(target_ids)
      and queue.status = 'translating'
  ) then
    return query select cardinality(target_ids), true;
    return;
  end if;

  delete from public.chapter_chunks
   where chapter_id = any(target_ids);

  update public.chapters
     set translated = false,
         updated_at = now()
   where id = any(target_ids);

  update public.translation_queue
     set status = 'queued',
         current_chunk = 0,
         total_chunks = 0,
         retry_count = 0,
         priority = 100,
         updated_at = now()
   where chapter_id = any(target_ids);

  return query select cardinality(target_ids), false;
end;
$$;

revoke all on function public.reset_retranslation(bigint, bigint[]) from public, anon, authenticated;
grant execute on function public.reset_retranslation(bigint, bigint[]) to service_role;
