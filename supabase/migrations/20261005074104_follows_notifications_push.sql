-- Follow, chapter-discovery, notification, settings, and Web Push persistence.
-- All access is mediated by the authenticated FastAPI backend using its
-- server-only Supabase key; browser roles receive no direct table privileges.

create table public.novel_followers (
  user_id uuid not null references auth.users(id) on delete cascade,
  novel_id bigint not null references public.novels(id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (user_id, novel_id)
);
create index novel_followers_novel_user_idx
  on public.novel_followers (novel_id, user_id);

create table public.chapter_monitor_state (
  novel_id bigint primary key references public.novels(id) on delete cascade,
  last_seen_source_url text,
  last_seen_chapter_number integer not null default 0 check (last_seen_chapter_number >= 0),
  last_checked_at timestamptz,
  last_error text,
  updated_at timestamptz not null default now()
);

-- Seed the discovery watermark from already-known chapter metadata. This lets
-- the first cloud run notify only chapters published since the last crawl.
insert into public.chapter_monitor_state (novel_id, last_seen_source_url, last_seen_chapter_number)
select n.id, latest.source_url, coalesce(latest.chapter_number, 0)
from public.novels n
left join lateral (
  select c.source_url, c.chapter_number
  from public.chapters c
  where c.novel_id = n.id
  order by c.chapter_number desc, c.id desc
  limit 1
) latest on true
where n.deleted_at is null
on conflict (novel_id) do nothing;

create table public.chapter_discoveries (
  id uuid primary key default gen_random_uuid(),
  novel_id bigint not null references public.novels(id) on delete cascade,
  source_identity text not null,
  chapter_number integer not null check (chapter_number > 0),
  chapter_title text not null,
  source_url text not null,
  discovered_at timestamptz not null default now(),
  crawled_at timestamptz,
  unique (novel_id, source_identity)
);
create index chapter_discoveries_novel_recent_idx
  on public.chapter_discoveries (novel_id, chapter_number desc, discovered_at desc);

create table public.notifications (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  event_key text not null unique,
  type text not null check (type in ('NEW_CHAPTER', 'CRAWLER_NEW_CHAPTER')),
  title text not null,
  message text not null,
  novel_id bigint references public.novels(id) on delete set null,
  discovery_id uuid references public.chapter_discoveries(id) on delete set null,
  chapter_number integer,
  url text not null,
  is_read boolean not null default false,
  created_at timestamptz not null default now()
);
create index notifications_user_recent_idx
  on public.notifications (user_id, created_at desc, id desc);
create index notifications_user_unread_idx
  on public.notifications (user_id, created_at desc)
  where is_read = false;

create table public.notification_settings (
  user_id uuid primary key references auth.users(id) on delete cascade,
  follow_notifications boolean not null default true,
  crawler_notifications boolean not null default true,
  web_notifications boolean not null default true,
  push_notifications boolean not null default false,
  updated_at timestamptz not null default now()
);

create table public.push_subscriptions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  endpoint text not null unique,
  p256dh text not null,
  auth text not null,
  user_agent text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index push_subscriptions_user_idx on public.push_subscriptions (user_id);

create table public.push_deliveries (
  id uuid primary key default gen_random_uuid(),
  notification_id uuid not null references public.notifications(id) on delete cascade,
  subscription_id uuid not null references public.push_subscriptions(id) on delete cascade,
  attempts integer not null default 0 check (attempts >= 0),
  sent_at timestamptz,
  last_error text,
  created_at timestamptz not null default now(),
  unique (notification_id, subscription_id)
);
create index push_deliveries_pending_idx
  on public.push_deliveries (created_at, id)
  where sent_at is null;

create or replace function public.list_followed_novels(
  p_user_id uuid,
  p_query text default '',
  p_filter text default 'all',
  p_limit integer default 20,
  p_offset integer default 0
)
returns table (
  novel_id bigint,
  follow_created_at timestamptz,
  slug text,
  title text,
  author text,
  cover_url text,
  latest_chapter integer,
  last_read_chapter integer,
  new_chapter_count bigint,
  updated_at timestamptz,
  total_count bigint
)
language sql
stable
security invoker
set search_path = ''
as $function$
  with followed as (
    select
      n.id as novel_id,
      f.created_at as follow_created_at,
      n.slug,
      n.title,
      n.author,
      n.cover_url,
      greatest(n.latest_chapter, coalesce(known.max_number, discoveries.max_number, 0))::integer as latest_chapter,
      coalesce(progress.chapter_number, 0)::integer as last_read_chapter,
      coalesce(discoveries.unread_count, 0)::bigint as new_chapter_count,
      n.updated_at
    from public.novel_followers f
    join public.novels n on n.id = f.novel_id and n.deleted_at is null
    left join public.reading_progress progress
      on progress.user_id = f.user_id and progress.novel_id = n.id
    left join lateral (
      select max(c.chapter_number) as max_number
      from public.chapters c
      where c.novel_id = n.id
    ) known on true
    left join lateral (
      select
        max(d.chapter_number) as max_number,
        count(*) filter (
          where d.discovered_at > f.created_at
            and (d.crawled_at is null or d.chapter_number > coalesce(progress.chapter_number, 0))
        ) as unread_count
      from public.chapter_discoveries d
      where d.novel_id = n.id
    ) discoveries on true
    where f.user_id = p_user_id
      and (
        coalesce(trim(p_query), '') = ''
        or n.title ilike '%' || trim(p_query) || '%'
        or coalesce(n.author, '') ilike '%' || trim(p_query) || '%'
      )
  ), filtered as (
    select *
    from followed
    where p_filter not in ('new', 'read')
       or (p_filter = 'new' and new_chapter_count > 0)
       or (p_filter = 'read' and new_chapter_count = 0)
  )
  select
    filtered.novel_id,
    filtered.follow_created_at,
    filtered.slug,
    filtered.title,
    filtered.author,
    filtered.cover_url,
    filtered.latest_chapter,
    filtered.last_read_chapter,
    filtered.new_chapter_count,
    filtered.updated_at,
    count(*) over ()::bigint as total_count
  from filtered
  order by (filtered.new_chapter_count > 0) desc, filtered.updated_at desc, filtered.follow_created_at desc
  limit greatest(1, least(coalesce(p_limit, 20), 50))
  offset greatest(coalesce(p_offset, 0), 0)
$function$;

revoke all on function public.list_followed_novels(uuid, text, text, integer, integer)
  from public, anon, authenticated;
grant execute on function public.list_followed_novels(uuid, text, text, integer, integer)
  to service_role;

alter table public.novel_followers enable row level security;
alter table public.chapter_monitor_state enable row level security;
alter table public.chapter_discoveries enable row level security;
alter table public.notifications enable row level security;
alter table public.notification_settings enable row level security;
alter table public.push_subscriptions enable row level security;
alter table public.push_deliveries enable row level security;

revoke all on table public.novel_followers, public.chapter_monitor_state,
  public.chapter_discoveries, public.notifications, public.notification_settings,
  public.push_subscriptions, public.push_deliveries from public, anon, authenticated;
grant all on table public.novel_followers, public.chapter_monitor_state,
  public.chapter_discoveries, public.notifications, public.notification_settings,
  public.push_subscriptions, public.push_deliveries to service_role;
