-- Replace the initial 15-minute schedule with the Hobby-friendly cadence.
-- Supabase Cron interprets this expression in UTC: 00:00, 06:00, 12:00, 18:00.
do $schedule$
declare
  existing_job bigint;
begin
  for existing_job in
    select jobid
    from cron.job
    where jobname in ('chapter-monitor-15m', 'chapter-monitor-6h')
  loop
    perform cron.unschedule(existing_job);
  end loop;

  perform cron.schedule(
    'chapter-monitor-6h',
    '0 */6 * * *',
    'select private.invoke_chapter_monitor()'
  );
end
$schedule$;
