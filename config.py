import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
    DEBUG = os.getenv("FLASK_DEBUG", "0") == "1"
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", "sqlite:///onedesk.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "static", "uploads")
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB

    # Session security
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "0") == "1"
    PERMANENT_SESSION_LIFETIME = 86400 * 14  # 14 days for remembered sessions

    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")
    ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "")

    # Company
    COMPANY_NAME = os.getenv("COMPANY_NAME", "My Company AB")
    COMPANY_ORG_NR = os.getenv("COMPANY_ORG_NR", "")
    COMPANY_ADDRESS = os.getenv("COMPANY_ADDRESS", "")
    COMPANY_EMAIL = os.getenv("COMPANY_EMAIL", "")
    COMPANY_PHONE = os.getenv("COMPANY_PHONE", "")
    COMPANY_BANKGIRO = os.getenv("COMPANY_BANKGIRO", "")
    COMPANY_VAT_NR = os.getenv("COMPANY_VAT_NR", "")
    COMPANY_LOGO_PATH = os.getenv("COMPANY_LOGO_PATH", "static/img/logo.png")
    COMPANY_REFERENCE = os.getenv("COMPANY_REFERENCE", "")  # "Vår referens" on invoices

    # Fortnox
    FORTNOX_CLIENT_ID = os.getenv("FORTNOX_CLIENT_ID", "")
    FORTNOX_CLIENT_SECRET = os.getenv("FORTNOX_CLIENT_SECRET", "")
    FORTNOX_REDIRECT_URI = os.getenv("FORTNOX_REDIRECT_URI", "http://localhost:5000/fortnox/callback")
    FORTNOX_BASE_URL = "https://api.fortnox.se/3"
    FORTNOX_INBOX_EMAIL = os.getenv("FORTNOX_INBOX_EMAIL", "")

    # OneDrive upload (Microsoft Graph)
    AZURE_TENANT_ID     = os.getenv("AZURE_TENANT_ID", "")
    AZURE_CLIENT_ID     = os.getenv("AZURE_CLIENT_ID", "")
    AZURE_CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET", "")
    ONEDRIVE_USER          = os.getenv("ONEDRIVE_USER", "")
    ONEDRIVE_UPLOAD_FOLDER = os.getenv("ONEDRIVE_UPLOAD_FOLDER", "Office Lens")

    # Anthropic (cloud OCR)
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

    # Ollama (local OCR — alternative to Anthropic)
    OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "")            # e.g. llama3.2-vision
    OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")

    # SMTP
    SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
    SMTP_USER = os.getenv("SMTP_USER", "")
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
    SMTP_FROM = os.getenv("SMTP_FROM", "")

    # Invoice
    INVOICE_PAYMENT_DAYS = int(os.getenv("INVOICE_PAYMENT_DAYS", 30))
    DEFAULT_HOURLY_RATE = float(os.getenv("DEFAULT_HOURLY_RATE", 1500))
    DEFAULT_CURRENCY = os.getenv("DEFAULT_CURRENCY", "SEK")

    # Fiscal year — FY starts on this month (1=Jan … 5=May)
    # FY 2025 = 2025-05-01 → 2026-04-30  →  FY_START_MONTH=5
    FY_START_MONTH = int(os.getenv("FY_START_MONTH", 5))
