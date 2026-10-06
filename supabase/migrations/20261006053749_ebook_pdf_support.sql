alter table public.ebook_books
  drop constraint if exists ebook_books_source_format_check;

alter table public.ebook_books
  add constraint ebook_books_source_format_check
  check (source_format in ('epub', 'txt', 'html', 'pdf'));
