# LeoTruyen

Ứng dụng đọc, crawl và dịch tiểu thuyết Trung Quốc.

## Kiến trúc hiện tại

```text
Browser → FastAPI web (app/main.py)
             ├─ Reader/Jinja2/HTMX/Tailwind
             ├─ Admin: truyện, chương, nội dung chương
             ├─ Supabase: metadata, queue, trạng thái dịch
             └─ Translator API + TTS API

PC worker (sync/service.py)
  ├─ nhận remote_crawl_jobs
  ├─ crawler trong app/crawlers/
  ├─ tạo translation_queue
  └─ xuất JSON chương vào Supabase Storage

Translator (infra/translator/service.py) ── HachimiMT-60-QT
TTS (infra/tts/) ── Edge TTS/cache audio
```

Web có thể chạy trên Vercel, nhưng Vercel không chạy crawler hoặc worker local. Các job crawl/dịch cần một PC worker đang chạy và truy cập được Supabase. Supabase là nguồn dữ liệu production cho truyện, chương, queue và nội dung đã xuất bản.

SQLite trong `data/` chỉ phục vụ dữ liệu/local flow tương thích; không dùng để thay thế queue production.

## Cấu trúc chính

```text
app/main.py                 FastAPI routes, reader và admin
app/templates/              Jinja2 UI
app/crawlers/               Registry và plugin crawler
app/translator/             Translator service client
app/tts/                    TTS và audio playlist/cache
sync/service.py             PC crawl/translation worker
infra/translator/           HTTP API cho HachimiMT
infra/tts/                  TTS service
supabase/migrations/        Schema và RPC production
data/                       SQLite local và cache audio
models/                     Model cache
tests/                      Pytest
```

Crawler được đăng ký trong `app/crawlers/bootstrap.py`, gồm các plugin cho 69shuba, fanqie, piaotian, uukanshu, trxs, xsw, hjwzw và tienhiep.

## Chạy local trên Windows

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Terminal 1 — web:

```powershell
.\venv\Scripts\uvicorn app.main:app --host 127.0.0.1 --port 8002
```

Terminal 2 — translator:

```powershell
.\venv\Scripts\uvicorn infra.translator.service:app --host 127.0.0.1 --port 7861
```

Terminal 3 — PC worker:

```powershell
$env:SYNC_AUTOSTART="true"
.\venv\Scripts\uvicorn sync.service:app --host 127.0.0.1 --port 7862
```

# Dừng worker 7862 và translator 7861
$ports = 7861,7862

Get-NetTCPConnection -State Listen -LocalPort $ports -ErrorAction SilentlyContinue |
  Select-Object -ExpandProperty OwningProcess -Unique |
  ForEach-Object {
    Stop-Process -Id $_ -Force
  }

Hoặc chạy script web có auto-reload:

```powershell
.\scripts\dev.ps1 -Port 8002
```

## Docker Compose

```powershell
Copy-Item .env.example .env
docker compose up -d --build
```

Compose gồm `web` (8000), `translator` (7861), `tts` (8002) và `sync` (8003). `sync` bật worker bằng `SYNC_AUTOSTART=true`.

Khởi động lại worker và translator:

```powershell
docker compose restart translator sync
```

## Luồng crawl và dịch

1. Admin nhập URL nguồn trong `/admin`.
2. Web tạo `remote_crawl_jobs` trên Supabase.
3. PC worker crawl metadata/chương và lưu raw content.
4. Worker tạo `translation_queue`; translator xử lý từng chương.
5. Bản dịch hoàn tất được xuất thành JSON trong Supabase Storage và reader đọc từ đó.

Nếu worker tắt, job vẫn nằm trong queue để xử lý sau. Trang `/jobs` hiển thị trạng thái crawl và dịch.

## Admin

- `/admin/novels`: sửa metadata và nguồn crawl.
- `/admin/novels/{id}/chapters`: CRUD chương.
- `/admin/novels/{id}/chapters/{chapter_id}/content`: sửa bản dịch và bản gốc.
- `/jobs`: theo dõi queue và worker.

Nút xem bản gốc trong reader chỉ dành cho tài khoản có role `admin`.

## Biến môi trường

Xem `.env.example`. Các biến thường dùng:

```text
SUPABASE_URL
SUPABASE_ANON_KEY
SUPABASE_SERVICE_KEY
MODEL_SERVICE_URL=http://127.0.0.1:7861
TTS_API_URL=http://127.0.0.1:8002
SYNC_AUTOSTART=true
SYNC_INTERVAL=60
```

Không commit `.env`, service key, model hoặc dữ liệu local.

## Kiểm thử

```powershell
.\venv\Scripts\python.exe -m pytest -q
```

Thay đổi crawler nằm trong `app/crawlers/`; thay đổi translator nằm trong `app/translator/`. Reader chỉ gọi service translator/TTS, không chứa logic crawler.
