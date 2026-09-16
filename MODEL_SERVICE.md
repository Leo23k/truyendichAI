# Model service

Website khong load model truc tiep. Hay chay HachimiMT service truoc:

```powershell
cd C:\Users\khanh\Downloads\hachimimt-local\hachimimt
.venv\Scripts\activate
pip install -r requirements.txt
pip install -r requirements-api.txt
python src\api.py
```

Health check: `http://127.0.0.1:7861/health`

Sau do chay website:

```powershell
cd D:\Data\Projects\truyen_dich_AI
uvicorn app.main:app --reload
```

Website goi `POST /v1/translate` voi model `ngocdang83/HachimiMT-60-QT` va CTranslate2 backend.
