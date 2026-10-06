# Ebook library

The private ebook library keeps metadata and chapter indexes in Supabase and original/chapter/translation files in the private B2 bucket. Access is scoped to the authenticated owner; file access uses short-lived signed URLs.

## Supported operations

- Upload EPUB, TXT, HTML, and PDF (25 MB maximum upload).
- EPUB/TXT/HTML are normalized into chapters and can be read in the ebook reader.
- PDF is preserved as a fixed-layout original and opened with the browser's PDF viewer. It is not text-extracted or translated.
- Chinese ebook chapters can be queued to the existing translator worker. Translations are stored separately; the Chinese source is never overwritten.
- Reader supports original/translated modes and uses the existing `/api/tts` service.
- Original download and EPUB/TXT/HTML exports are owner-authorized for private ebooks. Crawled novels also expose EPUB/TXT/HTML export from their detail page. Export languages: Vietnamese, source language, or bilingual.
- Deletion removes the owner's B2 object versions and cascades ebook metadata/chapter/queue rows.

## Supabase migrations

Apply migrations in timestamp order:

1. `20261006045457_private_ebook_library.sql` — private books and chapter index.
2. `20261006051941_ebook_translation_queue.sql` — separate ebook translation queue/chunks and translated-object path.
3. `20261006053749_ebook_pdf_support.sql` — permits `pdf` as a source format.

4. `20261006061127_ebook_cover_path.sql` — stores the private B2 object key for an EPUB cover.

The translation worker runs in the existing sync worker process. It checks the existing novel translation queue first, then ebook jobs. Keep the worker running for translation to progress; Vercel request handlers do not perform long-running translations.

## Storage keys

```text
ebooks/{sha256(owner_id)[:32]}/{ebook_uuid}/original.{ext}
ebooks/{sha256(owner_id)[:32]}/{ebook_uuid}/chapters/{shard:04d}.json
ebooks/{sha256(owner_id)[:32]}/{ebook_uuid}/translations/{chapter:06d}.json
ebooks/{sha256(owner_id)[:32]}/{ebook_uuid}/assets/cover.{jpg|png|gif|webp}
```

## Tests

```powershell
.\venv\Scripts\python.exe -m pytest -q tests/test_ebooks.py tests/test_ebooks_service.py
```

## Current scope limits

- PDF remains fixed-layout; no text extraction, translation, or generated PDF export.
- EPUB cover art is extracted (up to 5 MB) and stored privately in B2; the UI uses a short-lived signed URL. Other embedded chapter images are not extracted. Active HTML and external embedded content are stripped by the parser.
- Export generation buffers its result in process memory; very large books should be capped or moved to a streaming/background export path before broad use.
- MOBI/AZW/FB2/CBZ/DOCX and similar formats are not supported yet.
