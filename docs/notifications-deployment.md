# Follow, Web Push, and cloud chapter monitor deployment

## What runs where

- Supabase PostgreSQL stores follows, discovery watermarks/events, notifications, push subscriptions, and delivery retries.
- Supabase Cron calls `POST /api/cron/check-sources` every 3 hours (00:00, 03:00, ..., 21:00 UTC). The endpoint inspects a small batch of source indexes only; the PC worker still downloads chapter text and translates it through the existing job queue.
- Vercel Hobby Cron is not used because its schedule limit is too low for periodic chapter polling. Supabase Cron is the scheduler.

## Required deployment setup

1. Apply the new SQL migrations in `supabase/migrations` to the linked Supabase project.
2. Generate a random value of at least 32 characters and add it to Vercel as `CRON_SECRET` for Production. Redeploy after changing environment variables.
3. In Supabase Dashboard → Vault, create two secrets with these exact names:
   - `chapter_monitor_url` = `https://<your-production-domain>/api/cron/check-sources`
   - `chapter_monitor_secret` = the same value as Vercel `CRON_SECRET`
4. The migrations replace the initial 15-minute schedule with `chapter-monitor-3h`. Confirm only that job is active under Database → Cron Jobs and inspect run history after the first interval.

Do not commit secrets or put them in source code. If the Vault entries are absent, the scheduled database function logs a warning and safely skips its request.

## Web Push setup

Set these Vercel Production environment variables, then redeploy:

- `VAPID_PRIVATE_KEY`: VAPID private key in PEM form (keep it server-side only).
- `VAPID_PUBLIC_KEY`: the matching URL-safe public application-server key, without quotes.
- `VAPID_SUBJECT`: a contact URI such as `mailto:admin@example.com`.

The `py_vapid` utility installed with `pywebpush` can generate PEM key files with `vapid --gen`; run it in a private temporary directory, then get the browser key with `vapid --applicationServerKey`. Keep the private key out of the repository. Existing users enable browser permission and Push from `/notification-settings`.

## Verification

- Confirm the authenticated site shows `/followed`, `/notifications`, and `/notification-settings`.
- In Supabase Cron run history, confirm `chapter-monitor-3h` runs every 3 hours (UTC). `chapter_monitor_state.last_error` records per-novel source failures.
- A newly discovered source URL is inserted once by `(novel_id, source_identity)`. Its notification uses a separate unique event key per user; the PC worker marks the discovery crawled after it persists chapter content.
- Test Web Push with a real browser subscription on the deployed HTTPS origin. A local HTTP origin may not support Push except for localhost.

The migrations have been created in the repository but must be applied to the remote Supabase database, and the Vercel/Vault secrets must be configured before scheduled monitoring or push delivery is active.
