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

    # Google Cloud Vision API
    GOOGLE_VISION_API_KEY = os.environ.get('GOOGLE_VISION_API_KEY')

    # Gemini API (semantic fallback for OCR field extraction)
    GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

    # CORS - comma-separated list of allowed frontend origins
    FRONTEND_ORIGINS = [
        origin.strip()
        for origin in os.environ.get('FRONTEND_ORIGINS', 'http://localhost:4200').split(',')
        if origin.strip()
    ]
