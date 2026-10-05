-- Read the decrypted Vault view's plaintext column, not its encrypted `secret`.
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
  select decrypted_secret into endpoint
  from vault.decrypted_secrets
  where name = 'chapter_monitor_url'
  limit 1;

  select decrypted_secret into cron_secret
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

  execute format(
    'select %I.http_post(url := $1, body := $2, headers := $3, timeout_milliseconds := $4)',
    net_schema
  ) using endpoint, '{}'::jsonb,
    jsonb_build_object(
      'Content-Type', 'application/json',
      'Authorization', 'Bearer ' || cron_secret
    ),
    2000;
end
$function$;

revoke all on function private.invoke_chapter_monitor() from public, anon, authenticated;
