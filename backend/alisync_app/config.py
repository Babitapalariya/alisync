from functools import lru_cache
from pathlib import Path
import os

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


class Settings:
    BILLING_ENABLED = os.getenv("BILLING_ENABLED", "false").lower() == "true"
    MYSQL_URL = os.getenv("MYSQL_URL", "mysql+pymysql://root@localhost/alisync_app")
    APP_SECRET = os.getenv("APP_SECRET", "")
    FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3002")
    ALIEXPRESS_APP_KEY = os.getenv("ALIEXPRESS_APP_KEY", "")
    ALIEXPRESS_APP_SECRET = os.getenv("ALIEXPRESS_APP_SECRET", "")
    ALIEXPRESS_API_URL = os.getenv("ALIEXPRESS_API_URL", "https://api-sg.aliexpress.com/rest")
    ALIEXPRESS_REDIRECT_URI = os.getenv("ALIEXPRESS_REDIRECT_URI", "http://localhost:8002/app/aliexpress/callback")
    SHOPIFY_CLIENT_ID = os.getenv("SHOPIFY_CLIENT_ID", "")
    SHOPIFY_CLIENT_SECRET = os.getenv("SHOPIFY_CLIENT_SECRET", "")
    SHOPIFY_REDIRECT_URI = os.getenv("SHOPIFY_REDIRECT_URI", "http://localhost:8002/app/shopify/callback")
    SHOPIFY_SCOPES = os.getenv("SHOPIFY_SCOPES", "read_products,write_products")
    SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2026-01")
    SMTP_HOST = os.getenv("SMTP_HOST", "")
    SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
    SMTP_FROM_EMAIL = os.getenv("SMTP_FROM_EMAIL", "")
    SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() == "true"
    GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
    GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "http://127.0.0.1:8002/app/auth/google/callback")


@lru_cache
def get_settings() -> Settings:
    return Settings()
