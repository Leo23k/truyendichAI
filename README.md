# My Novel Reader

Ứng dụng cá nhân để quản lý, đọc và dịch truyện Trung Quốc.

## Chạy trên Windows

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
Copy-Item .env.example .env

uvicorn app.main:app --reload
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

Model service chạy tại `http://127.0.0.1:7861`; website chạy tại `http://127.0.0.1:8000`. Database nằm tại `data/app.db`, audio cache sẽ nằm tại `data/audio/`.

Model service dùng cố định `ngocdang83/HachimiMT-60-QT` bằng CTranslate2. Website chỉ gọi API qua `MODEL_SERVICE_URL`, không tự load PyTorch/Transformers. Nếu model service tắt, bản gốc vẫn được lưu và giao diện báo lỗi ở bước dịch.

Hãy sao lưu `data/app.db` và thư mục `data/audio/`. Plugin crawler đã triển khai: `69shuba.com`, `trxs.cc` và `m.xsw.tw`. Các nguồn `bookqq`, `fanqienovel`, `piaotian`, `uukanshu` chưa được đăng ký vì domain hoặc cấu trúc truy cập chưa đủ ổn định để xác minh an toàn.
