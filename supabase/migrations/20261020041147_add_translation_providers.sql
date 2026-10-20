create table public.translation_prompts (
  id uuid primary key default gen_random_uuid(),
  name text not null unique check (char_length(trim(name)) between 1 and 120),
  template text not null check (position('{{text}}' in template) > 0),
  is_default boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create unique index translation_prompts_one_default_idx
  on public.translation_prompts (is_default)
  where is_default;

alter table public.translation_prompts enable row level security;
revoke all on table public.translation_prompts from anon, authenticated;
grant all on table public.translation_prompts to service_role;

insert into public.translation_prompts (name, template, is_default)
values (
  'Tiên hiệp tự nhiên',
  'Bạn là dịch giả tiểu thuyết Trung-Việt. Dịch sát nghĩa nhưng tự nhiên, giữ nguyên tên riêng và các token dạng __GLOSSARY_0__. Chỉ trả về bản dịch, không giải thích.\n\n{{text}}',
  true
);

alter table public.translation_queue
  add column provider text not null default 'hachimi'
    check (provider in ('hachimi', 'gemini_api', 'gemini_web')),
  add column model text not null default 'gemini-3.5-flash-lite',
  add column web_profile text,
  add column prompt_id uuid references public.translation_prompts(id) on delete set null,
  add column prompt_name text not null default 'Tiên hiệp tự nhiên',
  add column prompt_template text not null default 'Bạn là dịch giả tiểu thuyết Trung-Việt. Dịch sát nghĩa nhưng tự nhiên. Chỉ trả về bản dịch.\n\n{{text}}';

alter table public.remote_crawl_jobs
  add column provider text not null default 'hachimi'
    check (provider in ('hachimi', 'gemini_api', 'gemini_web')),
  add column model text not null default 'gemini-3.5-flash-lite',
  add column web_profile text,
  add column prompt_id uuid references public.translation_prompts(id) on delete set null,
  add column prompt_name text not null default 'Tiên hiệp tự nhiên',
  add column prompt_template text not null default 'Bạn là dịch giả tiểu thuyết Trung-Việt. Dịch sát nghĩa nhưng tự nhiên. Chỉ trả về bản dịch.\n\n{{text}}';

create or replace function public.reset_retranslation_with_provider(
  p_novel_id bigint,
  p_chapter_ids bigint[] default null,
  p_provider text default 'hachimi',
  p_model text default 'gemini-3.5-flash-lite',
  p_web_profile text default null,
  p_prompt_id uuid default null,
  p_prompt_name text default 'Tiên hiệp tự nhiên',
  p_prompt_template text default 'Bạn là dịch giả tiểu thuyết Trung-Việt. Dịch sát nghĩa nhưng tự nhiên. Chỉ trả về bản dịch.\n\n{{text}}'
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

  perform 1
  from public.translation_queue as queue
  where queue.chapter_id = any(target_ids)
  for update;

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
  select chapter_id, 'queued', 100, 0, 0, 0,
         p_provider, p_model, nullif(trim(p_web_profile), ''), p_prompt_id,
         p_prompt_name, p_prompt_template
  from unnest(target_ids) as chapter_id
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
