create table if not exists public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  email text not null, display_name text not null, avatar_url text,
  role text not null default 'user' check (role in ('admin','user')),
  created_at timestamptz not null default now(), updated_at timestamptz not null default now()
);
alter table public.profiles enable row level security;
revoke all on public.profiles from anon;
grant select on public.profiles to authenticated;
create policy "profiles_are_self_readable" on public.profiles for select to authenticated using ((select auth.uid()) = id);
