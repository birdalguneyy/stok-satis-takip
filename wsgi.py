import os
import sys
import time
import logging

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

# Türkiye Saat Dilimi Ayarı (UTC+3)
os.environ["TZ"] = "Europe/Istanbul"
if hasattr(time, "tzset"):
    try:
        time.tzset()
    except Exception:
        pass

from app.web.web_server import app

logger = logging.getLogger(__name__)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Render / Cloud Web Server başlatılıyor. Port: {port}")
    app.run(host="0.0.0.0", port=port)
