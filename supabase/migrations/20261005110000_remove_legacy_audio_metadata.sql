-- Reader TTS is generated on demand and cached on the application worker.
alter table public.chapters drop column if exists audio_available;
