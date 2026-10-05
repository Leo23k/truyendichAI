-- Crawl-originated notifications wait for translation. Source-monitor events
-- keep the default (false) and are announced as soon as a source chapter is seen.
alter table public.chapter_discoveries
  add column if not exists notification_after_translation boolean not null default false;

-- Push delivery is independent of the three-hour source scan. The Vault URL
-- already points to /api/cron/check-sources; derive the delivery endpoint from it.
create or replace function private.invoke_push_delivery()
returns void
language plpgsql
security definer
set search_path = ''
as $function$
declare
  monitor_endpoint text;
  endpoint text;
  cron_secret text;
  net_schema text;
begin
  select secret into monitor_endpoint
  from vault.decrypted_secrets
  where name = 'chapter_monitor_url'
  limit 1;
  select secret into cron_secret
  from vault.decrypted_secrets
  where name = 'chapter_monitor_secret'
  limit 1;

  if coalesce(monitor_endpoint, '') = '' or coalesce(cron_secret, '') = '' then
    raise warning 'Push delivery Vault secrets are not configured';
    return;
  end if;
  if monitor_endpoint !~ '/api/cron/check-sources/?$' then
    raise warning 'chapter_monitor_url does not end with /api/cron/check-sources';
    return;
  end if;
  endpoint := regexp_replace(monitor_endpoint, '/api/cron/check-sources/?$', '/api/cron/deliver-push');

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
revoke all on function private.invoke_push_delivery() from public, anon, authenticated;

do $schedule$
declare
  existing_job bigint;
begin
  select jobid into existing_job from cron.job where jobname = 'push-delivery-every-minute';
  if existing_job is not null then
    perform cron.unschedule(existing_job);
  end if;
  perform cron.schedule(
    'push-delivery-every-minute',
    '* * * * *',
    'select private.invoke_push_delivery()'
  );
end
$schedule$;
