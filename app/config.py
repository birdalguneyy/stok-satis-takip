import os
import sys
from pathlib import Path

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

DB_PATH = DATA_DIR / "stok_satis.db"


DEFAULT_CRITICAL_STOCK = 5
CURRENCY_SYMBOL = "₺"
WINDOW_MIN_WIDTH = 1100
WINDOW_MIN_HEIGHT = 700
TOAST_DURATION_MS = 3000
