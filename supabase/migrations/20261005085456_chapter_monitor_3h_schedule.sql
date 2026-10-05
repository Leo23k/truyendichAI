-- Replace the previous monitor cadence with a three-hour schedule.
-- Supabase Cron evaluates this in UTC: 00:00, 03:00, ..., 21:00.
do $schedule$
declare
  existing_job bigint;
begin
  for existing_job in
    select jobid
    from cron.job
    where jobname in (
      'chapter-monitor-15m',
      'chapter-monitor-6h',
      'chapter-monitor-3h'
    )
  loop
    perform cron.unschedule(existing_job);
  end loop;

  perform cron.schedule(
    'chapter-monitor-3h',
    '0 */3 * * *',
    'select private.invoke_chapter_monitor()'
  );
end
$schedule$;
