# LeoTruyen

LeoTruyen can run locally, as Docker containers, or on an Oracle Cloud VM. It uses FastAPI, Jinja2, HTMX, SQLite, HachimiMT-60-QT, and Microsoft Edge TTS.

## GitHub, jsDelivr, and Vercel

Keep the application repository private. Publish only processed novel snapshots to the public `Leo23k/truyendichAI-data` repository. jsDelivr serves those snapshots from:

```text
https://cdn.jsdelivr.net/gh/Leo23k/truyendichAI-data@main/data/
```

On the local PC, run a one-time sync after crawling, translating, and generating audio:

```powershell
.venv\Scripts\python.exe sync\service.py sync
```

Or keep it running while the PC is on:

```powershell
.venv\Scripts\python.exe sync\service.py watch --interval 60
```

Set `GITHUB_DATA_TOKEN` only when the sync service runs in Docker. The token needs repository contents read/write access to `Leo23k/truyendichAI-data`; do not commit it. Vercel only needs `JSDELIVR_DATA_BASE_URL` to read published chapters and MP3 files.

## Docker

```bash
cp env.example .env
docker compose up -d --build
```

Compose runs the FastAPI web app on port `8000`, a HachimiMT-60-QT CTranslate2 translator, and an Edge TTS service. The model is downloaded only once into `./models`; SQLite, covers, translations, and audio caches live in `./data`.

## Vercel

Push the repository to GitHub and import it in Vercel. Add these Environment Variables:

```text
TRANSLATOR_API_URL=https://your-oracle-domain.example/translate
TTS_API_URL=https://your-oracle-domain.example/tts
DATABASE_PATH=/tmp/database.db
```

Vercel is appropriate for a UI demo. Its filesystem is temporary, so persistent reading history and SQLite data belong on Oracle.

## Oracle

On Ubuntu 24.04, after configuring SSH access to the private repository:

```bash
chmod +x infra/scripts/*.sh infra/ssl/*.sh
./infra/scripts/setup-oracle.sh
```

The script installs Docker and Nginx, creates `.env` when needed, starts Compose, enables restart after reboot, and installs a daily backup timer. After DNS points at the VM, enable HTTPS:

```bash
./infra/ssl/request-cert.sh your-domain.example you@example.com
```

## Structure

```text
app/                 FastAPI, crawler, reader, translator, and TTS
data/                SQLite, covers, translations, audio caches, backups
models/              HachimiMT cache
infra/               Docker services, Nginx, systemd, and Oracle scripts
tests/               Project tests
```

Ứng dụng cá nhân để quản lý, đọc và dịch truyện Trung Quốc.

## Chạy trên Windows

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
Copy-Item .env.example .env

.\scripts\dev.ps1
```

Script dev mặc định chạy website ở `http://127.0.0.1:8002` và bật auto reload cho thư mục `app/`. Nếu cổng `8002` đang bận, đổi sang cổng trống khác:

```powershell
.\scripts\dev.ps1 -Port 8003
```

Tránh dùng mặc định `8000` khi trên máy đang có service khác giữ cổng này.

Nếu PowerShell chặn chạy script, dùng:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\dev.ps1
```

Nếu auto reload gặp `PermissionError: [WinError 5] Access is denied` ở `multiprocessing.resource_sharer`, chạy không reload:

```powershell
.\scripts\dev.ps1 -NoReload
```

Trước đó hãy chạy model service ở terminal riêng:

```powershell
cd C:\Users\khanh\Downloads\hachimimt-local\hachimimt
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pip install -r requirements-api.txt
python src\api.py
```

Model service chạy tại `http://127.0.0.1:7861`; website chạy tại `http://127.0.0.1:8002` khi chạy theo lệnh Windows ở trên. Database nằm tại `data/app.db`, audio cache sẽ nằm tại `data/audio/`.

Model service dùng cố định `ngocdang83/HachimiMT-60-QT` bằng CTranslate2. Website chỉ gọi API qua `MODEL_SERVICE_URL`, không tự load PyTorch/Transformers. Nếu model service tắt, bản gốc vẫn được lưu và giao diện báo lỗi ở bước dịch.

Hãy sao lưu `data/app.db` và thư mục `data/audio/`. Plugin crawler đã triển khai: `69shuba.com`, `trxs.cc` và `m.xsw.tw`. Các nguồn `bookqq`, `fanqienovel`, `piaotian`, `uukanshu` chưa được đăng ký vì domain hoặc cấu trúc truy cập chưa đủ ổn định để xác minh an toàn.
