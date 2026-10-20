alter table public.translation_queue
  alter column provider set default 'hachimi',
  alter column model set default 'gemini-3.5-flash-lite';

alter table public.remote_crawl_jobs
  alter column provider set default 'hachimi',
  alter column model set default 'gemini-3.5-flash-lite';

update public.translation_prompts
set template = replace(template, E'\\n', E'\n')
where position(E'\\n' in template) > 0;

alter table public.translation_queue
  alter column prompt_template set default E'Bạn là dịch giả tiểu thuyết Trung-Việt. Dịch sát nghĩa nhưng tự nhiên. Chỉ trả về bản dịch.\\n\\n{{text}}';

alter table public.remote_crawl_jobs
  alter column prompt_template set default E'Bạn là dịch giả tiểu thuyết Trung-Việt. Dịch sát nghĩa nhưng tự nhiên. Chỉ trả về bản dịch.\\n\\n{{text}}';

create or replace function public.reset_retranslation_with_provider(
  p_novel_id bigint,
  p_chapter_ids bigint[] default null,
  p_provider text default 'hachimi',
  p_model text default 'gemini-3.5-flash-lite',
  p_web_profile text default null,
  p_prompt_id uuid default null,
  p_prompt_name text default 'Tiên hiệp tự nhiên',
  p_prompt_template text default E'Bạn là dịch giả tiểu thuyết Trung-Việt. Dịch sát nghĩa nhưng tự nhiên. Chỉ trả về bản dịch.\\n\\n{{text}}'
)
returns table (chapter_count integer, blocked boolean)
language plpgsql
security invoker
set search_path = public
as $$
declare
  target_ids bigint[];
begin
  if p_provider not in ('hachimi', 'gemini_api', 'gemini_web') then
    raise exception 'Invalid translation provider';
  end if;
  if position('{{text}}' in p_prompt_template) = 0 then
    raise exception 'Translation prompt must contain {{text}}';
  end if;

  select array_agg(chapter.id order by chapter.id)
    into target_ids
  from public.chapters as chapter
  where chapter.novel_id = p_novel_id
    and (p_chapter_ids is null or chapter.id = any(p_chapter_ids));

  if coalesce(cardinality(target_ids), 0) = 0 then
    return query select 0, false;
    return;
  end if;

  perform 1 from public.translation_queue as queue
  where queue.chapter_id = any(target_ids) for update;

  if exists (
    select 1 from public.translation_queue as queue
    where queue.chapter_id = any(target_ids) and queue.status = 'translating'
  ) then
    return query select cardinality(target_ids), true;
    return;
  end if;

  delete from public.chapter_chunks where chapter_id = any(target_ids);
  update public.chapters set translated = false, updated_at = now() where id = any(target_ids);

  insert into public.translation_queue (
    chapter_id, status, priority, current_chunk, total_chunks, retry_count,
    provider, model, web_profile, prompt_id, prompt_name, prompt_template
  )
  select target.chapter_id, 'queued', 100, 0, 0, 0,
         p_provider, p_model, nullif(trim(p_web_profile), ''), p_prompt_id,
         p_prompt_name, p_prompt_template
  from unnest(target_ids) as target(chapter_id)
  on conflict (chapter_id) do update
    set status = 'queued', priority = 100, current_chunk = 0, total_chunks = 0,
        retry_count = 0, provider = excluded.provider, model = excluded.model,
        web_profile = excluded.web_profile, prompt_id = excluded.prompt_id,
        prompt_name = excluded.prompt_name, prompt_template = excluded.prompt_template,
        updated_at = now();

  return query select cardinality(target_ids), false;
end;
$$;

revoke all on function public.reset_retranslation_with_provider(bigint, bigint[], text, text, text, uuid, text, text)
  from public, anon, authenticated;
grant execute on function public.reset_retranslation_with_provider(bigint, bigint[], text, text, text, uuid, text, text)
  to service_role;
