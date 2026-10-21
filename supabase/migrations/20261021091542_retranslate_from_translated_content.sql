alter table public.translation_queue
  add column input_mode text not null default 'original'
  check (input_mode in ('original', 'translated'));

create function public.set_translation_queue_input_mode()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  if new.input_mode = 'translated'
     and new.prompt_name = 'Biên tập bản dịch hiện có'
     and new.prompt_template like '%biên tập viên tiểu thuyết tiếng Việt%' then
    return new;
  end if;
  new.input_mode := 'original';
  return new;
end;
$$;

create trigger translation_queue_input_mode
before insert or update of input_mode, prompt_name, prompt_template
on public.translation_queue
for each row execute function public.set_translation_queue_input_mode();

create or replace function public.queue_translated_retranslation(
  p_novel_id bigint,
  p_chapter_ids bigint[] default null,
  p_prompt_name text default 'Biên tập bản dịch hiện có',
  p_prompt_template text default E'Bạn là biên tập viên tiểu thuyết tiếng Việt. Hãy biên tập bản dịch sau cho tự nhiên, mạch lạc và dễ đọc; giữ nguyên đầy đủ nội dung, tình tiết, lời thoại, tên riêng và cách xuống đoạn. Không thêm lời dẫn, nhận xét hoặc tóm tắt. Chỉ trả về bản đã biên tập.\n\n{{text}}'
)
returns table (chapter_count integer, blocked boolean)
language plpgsql
security invoker
set search_path = public
as $$
declare
  target_ids bigint[];
begin
  if position('{{text}}' in p_prompt_template) = 0 then
    raise exception 'Editing prompt must contain {{text}}';
  end if;

  perform pg_advisory_xact_lock(p_novel_id);

  select array_agg(chapter.id order by chapter.id)
    into target_ids
  from public.chapters as chapter
  where chapter.novel_id = p_novel_id
    and chapter.translated
    and (p_chapter_ids is null or chapter.id = any(p_chapter_ids));

  if coalesce(cardinality(target_ids), 0) = 0 then
    return query select 0, false;
    return;
  end if;

  perform 1 from public.translation_queue as queue
  where queue.chapter_id = any(target_ids) for update;

  if exists (
    select 1 from public.translation_queue as queue
    where queue.chapter_id = any(target_ids)
      and queue.status in ('queued', 'translating', 'backoff', 'paused', 'paused_quota', 'paused_manual', 'review_required')
  ) then
    return query select cardinality(target_ids), true;
    return;
  end if;

  insert into public.translation_queue (
    chapter_id, status, priority, current_chunk, total_chunks, retry_count,
    provider, model, web_profile, prompt_id, prompt_name, prompt_template,
    input_mode, attempts, error_code, error_message, completed_at
  )
  select target.chapter_id, 'queued', 100, 0, 0, 0,
         'gemini_web', 'gemini-web', null, null, p_prompt_name, p_prompt_template,
         'translated', 0, null, null, null
  from unnest(target_ids) as target(chapter_id)
  on conflict (chapter_id) do update
    set status = 'queued', priority = 100, current_chunk = 0, total_chunks = 0,
        retry_count = 0, provider = 'gemini_web', model = 'gemini-web',
        web_profile = null, prompt_id = null, prompt_name = excluded.prompt_name,
        prompt_template = excluded.prompt_template, input_mode = 'translated',
        attempts = 0, error_code = null, error_message = null, completed_at = null,
        batch_job_id = null, updated_at = now();

  return query select cardinality(target_ids), false;
end;
$$;

revoke all on function public.queue_translated_retranslation(bigint, bigint[], text, text)
  from public, anon, authenticated;
grant execute on function public.queue_translated_retranslation(bigint, bigint[], text, text)
  to service_role;
