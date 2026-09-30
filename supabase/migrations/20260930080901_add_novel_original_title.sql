alter table public.novels add column if not exists original_title text;

update public.novels
set original_title = '普罗之主'
where id = 514 and coalesce(original_title, '') = '';
