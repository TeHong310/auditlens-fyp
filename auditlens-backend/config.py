import os
from datetime import timedelta
from dotenv import load_dotenv

load_dotenv()

class Config:
    # JWT
    JWT_SECRET_KEY = os.environ.get('JWT_SECRET_KEY')
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(hours=8)

    # Database
    DB_NAME     = os.environ.get('DB_NAME', 'auditlens')
    DB_USER     = os.environ.get('DB_USER', 'postgres')
    DB_PASSWORD = os.environ.get('DB_PASSWORD')
    DB_HOST     = os.environ.get('DB_HOST', 'localhost')
    DB_PORT     = os.environ.get('DB_PORT', '5432')

    # Upload
    UPLOAD_FOLDER = 'uploads'
    POPPLER_PATH  = os.environ.get('POPPLER_PATH') or None
    ALLOWED_EXTENSIONS = {'pdf', 'jpg', 'jpeg', 'png'}

    # Render's free tier disk is ephemeral (wiped on every redeploy/
    # restart), so uploaded files are also persisted as bytes in
    # Postgres (see documents/purchase_orders/goods_receipts.file_bytes).
    # Invoices/POs/GRs are small scanned documents — 8MB is generous
    # headroom while still keeping row sizes sane. A file over this cap
    # still uploads and processes normally (OCR/Gemini/local disk all
    # still work for the current request); it just isn't persisted to
    # the DB, so it won't survive a restart.
    MAX_DB_FILE_BYTES = 8 * 1024 * 1024

    # Google Cloud Vision API
    GOOGLE_VISION_API_KEY = os.environ.get('GOOGLE_VISION_API_KEY')

    # Gemini API — single source of truth for every Gemini call in the
    # codebase (field extraction, authenticity, anomaly explanation). All
    # of them read Config.GEMINI_API_KEY / Config.GEMINI_MODEL only —
    # never a hardcoded key/model or a second env var.
    GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

    # Normalized so a common env-var mistake can't silently break the
    # request URL: the ListModels endpoint (see
    # gemini_extractor.log_available_gemini_models) returns names like
    # "models/gemini-2.5-flash" — pasting that whole string into
    # GEMINI_MODEL would build ".../models/models/gemini-2.5-flash" and
    # 404. Strip a leading "models/" and any surrounding whitespace here,
    # once, so every caller gets a clean bare model name regardless of
    # what's actually in the env var.
    _raw_gemini_model = os.environ.get('GEMINI_MODEL', 'gemini-2.5-flash')
    GEMINI_MODEL = _raw_gemini_model.strip()
    if GEMINI_MODEL.startswith('models/'):
        GEMINI_MODEL = GEMINI_MODEL[len('models/'):]

    # CORS - comma-separated list of allowed frontend origins
    FRONTEND_ORIGINS = [
        origin.strip()
        for origin in os.environ.get('FRONTEND_ORIGINS', 'http://localhost:4200').split(',')
        if origin.strip()
    ]

    # Temp diagnostic-endpoint guard (see routes/admin.py rerun-anomaly route)
    ADMIN_TOKEN = os.environ.get('ADMIN_TOKEN')


# Startup log (once per process) so it's possible to confirm in Render logs
# that every Gemini call in this process is using the same key and a
# correctly-formed model name. repr() on the raw vs. normalized model
# value makes a stray "models/" prefix or hidden whitespace visible
# immediately instead of only surfacing as a mysterious 404 later.
if Config.GEMINI_API_KEY:
    print(f"DEBUG Gemini key loaded: ...{Config.GEMINI_API_KEY[-4:]}")
else:
    print("DEBUG Gemini key loaded: none (GEMINI_API_KEY not set)")
print(f"DEBUG Gemini model loaded: raw_env={Config._raw_gemini_model!r} normalized={Config.GEMINI_MODEL!r}")
