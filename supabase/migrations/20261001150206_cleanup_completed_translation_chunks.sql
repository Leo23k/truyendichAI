-- Final raw and translated text are stored in chapter JSON.  Chunks only
-- exist while a queue item is actively being translated.
delete from public.chapter_chunks as chunk
using public.translation_queue as queue
where queue.chapter_id = chunk.chapter_id
  and queue.status = 'completed';
