import os
import logging
from app.web.web_server import app

logger = logging.getLogger(__name__)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Render / Cloud Web Server başlatılıyor. Port: {port}")
    app.run(host="0.0.0.0", port=port)
