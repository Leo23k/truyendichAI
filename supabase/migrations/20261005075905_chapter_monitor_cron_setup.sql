-- Seed known chapter URLs so the first cloud check only emits genuinely new
-- source entries, not one notification per existing chapter.
insert into public.chapter_discoveries (
  novel_id, source_identity, chapter_number, chapter_title, source_url, crawled_at
)
select
  c.novel_id,
  'url:' || rtrim(c.source_url, '/'),
  greatest(c.chapter_number, 1),
  coalesce(nullif(c.title, ''), 'Chương ' || c.chapter_number),
  c.source_url,
  case when c.content_path is not null then now() else null end
from public.chapters c
where c.source_url is not null and c.chapter_number > 0
on conflict (novel_id, source_identity) do nothing;

create index if not exists chapter_monitor_state_due_idx
  on public.chapter_monitor_state (last_checked_at nulls first, novel_id);

-- Supabase Cron + pg_net call the serverless endpoint. Store the deployment
-- URL and a random CRON_SECRET in Vault as chapter_monitor_url and
-- chapter_monitor_secret before the scheduled job is expected to run.
create extension if not exists pg_cron;
create extension if not exists pg_net with schema extensions;

create schema if not exists private;
revoke all on schema private from public, anon, authenticated;

create or replace function private.invoke_chapter_monitor()
returns void
language plpgsql
security definer
set search_path = ''
as $function$
declare
  endpoint text;
  cron_secret text;
  net_schema text;
begin
  select secret into endpoint
  from vault.decrypted_secrets
  where name = 'chapter_monitor_url'
  limit 1;
  select secret into cron_secret
  from vault.decrypted_secrets
  where name = 'chapter_monitor_secret'
  limit 1;

  if coalesce(endpoint, '') = '' or coalesce(cron_secret, '') = '' then
    raise warning 'Chapter monitor Vault secrets are not configured';
    return;
  end if;

  select n.nspname into net_schema
  from pg_catalog.pg_proc p
  join pg_catalog.pg_namespace n on n.oid = p.pronamespace
  where p.proname = 'http_post' and n.nspname in ('net', 'extensions')
  order by case n.nspname when 'net' then 0 else 1 end
  limit 1;
  if net_schema is null then
    raise warning 'pg_net http_post is not available';
    return;
  end if;
  execute format('select %I.http_post(url := $1, body := $2, headers := $3, timeout_milliseconds := $4)', net_schema)
    using endpoint, '{}'::jsonb,
      jsonb_build_object('Content-Type', 'application/json', 'Authorization', 'Bearer ' || cron_secret),
      2000;
end
$function$;
revoke all on function private.invoke_chapter_monitor() from public, anon, authenticated;

do $schedule$
declare
  existing_job bigint;
begin
  select jobid into existing_job from cron.job where jobname = 'chapter-monitor-15m';
  if existing_job is not null then
    perform cron.unschedule(existing_job);
  end if;
  perform cron.schedule('chapter-monitor-15m', '*/15 * * * *',
    'select private.invoke_chapter_monitor()');
end
$schedule$;
