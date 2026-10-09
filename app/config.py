import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Türkiye Saati (UTC+3) - Türkiye kalıcı olarak UTC+3 kullanmaktadır
TURKEY_TZ = timezone(timedelta(hours=3))


def get_turkey_now() -> datetime:
    """Türkiye yerel saatini (UTC+3) datetime nesnesi olarak döndürür."""
    return datetime.now(TURKEY_TZ)


def get_turkey_now_str(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Türkiye yerel saatini (UTC+3) string olarak döndürür."""
    return datetime.now(TURKEY_TZ).strftime(fmt)


APP_NAME = "Stok & Satış Takip"
APP_VERSION = "0.1.0"

if getattr(sys, "frozen", False):
    # PyInstaller executable mode: save persistent database in %APPDATA%\StokSatisTakip
    app_data_root = Path(os.getenv("APPDATA", Path.home())) / "StokSatisTakip"
    DATA_DIR = app_data_root / "data"
else:
    # Development mode
    BASE_DIR = Path(__file__).resolve().parent.parent
    DATA_DIR = BASE_DIR / "data"

DB_NAME = os.getenv("SQLITE_DB_NAME", "stok_satis.db")
DB_PATH = DATA_DIR / DB_NAME


DEFAULT_CRITICAL_STOCK = 5
CURRENCY_SYMBOL = "₺"
WINDOW_MIN_WIDTH = 1100
WINDOW_MIN_HEIGHT = 700
TOAST_DURATION_MS = 3000
