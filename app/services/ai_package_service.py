import base64
import io
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import cv2
import numpy as np
from PIL import Image, ImageOps

from app.config import DATA_DIR
from app.utils.camera import decode_barcode_from_frame

logger = logging.getLogger(__name__)

UPLOADS_DIR = DATA_DIR / "uploads" / "products"
KEY_FILE = DATA_DIR / "gemini_key.txt"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

# Support both new google.genai and legacy google.generativeai SDKs
HAS_GENAI = False
HAS_LEGACY_GENAI = False

try:
    from google import genai
    from google.genai import types
    HAS_GENAI = True
except ImportError:
    HAS_GENAI = False

if not HAS_GENAI:
    try:
        import google.generativeai as legacy_genai
        HAS_LEGACY_GENAI = True
    except ImportError:
        HAS_LEGACY_GENAI = False


class AIPackageService:
    """Yapay Zeka (Gemini Vision) Ambalaj Tanıma ve Ürün Görsel Yöneticisi."""

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = (api_key or self._load_saved_key() or "").strip()
        self.client = None
        self.legacy_active = False
        self._init_client()

    def _load_saved_key(self) -> Optional[str]:
        env_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if env_key and env_key.strip():
            return env_key.strip()
            
        if KEY_FILE.exists():
            try:
                k = KEY_FILE.read_text(encoding="utf-8").strip()
                if k:
                    return k
            except Exception:
                pass
        return None

    def save_api_key(self, api_key: str) -> bool:
        clean_key = api_key.strip()
        if not clean_key:
            return False
        try:
            KEY_FILE.write_text(clean_key, encoding="utf-8")
            self.api_key = clean_key
            self._init_client()
            return True
        except Exception as exc:
            logger.error(f"Gemini API key kaydetme hatası: {exc}")
            return False

    def _init_client(self) -> None:
        if not self.api_key:
            self.client = None
            logger.warning("Gemini API Key bulunamadı. Yapay zeka ambalaj okuyucu pasif.")
            return

        if HAS_GENAI:
            try:
                from google import genai
                self.client = genai.Client(api_key=self.api_key)
                self.legacy_active = False
                logger.info("Google GenAI (Yeni SDK) Client başarıyla başlatıldı.")
                return
            except Exception as exc:
                logger.warning(f"GenAI Client başlatılamadı, legacy kütüphane deneniyor: {exc}")

        if HAS_LEGACY_GENAI:
            try:
                import google.generativeai as legacy_genai
                legacy_genai.configure(api_key=self.api_key)
                self.client = legacy_genai
                self.legacy_active = True
                logger.info("Google GenerativeAI (Legacy SDK) Client başarıyla başlatıldı.")
                return
            except Exception as exc:
                logger.warning(f"Legacy Gemini Client başlatılamadı: {exc}")

        self.client = None

    def analyze_packaging_and_save_image(
        self, image_bytes: bytes, file_extension: str = ".jpg"
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """Ürün ambalaj resmini sıkıştırıp Gemini Yapay Zekaya ve barkod motoruna gönderir."""

        timestamp = int(time.time() * 1000)
        saved_filename = f"prod_pkg_{timestamp}{file_extension}"
        saved_filepath = UPLOADS_DIR / saved_filename

        # 1. Resim Açma, EXIF düzeltme ve Küçültülmüş Fotoğraf Kaydı (Max 500px, ~40KB)
        pil_img = None
        ai_img = None
        try:
            pil_img = Image.open(io.BytesIO(image_bytes))
            pil_img = ImageOps.exif_transpose(pil_img).convert("RGB")

            # 1. Kayıt için ultra-küçük Base64 JPEG (Render konteynırı sıfırlansa bile görseller ASLA kaybolmaz, ~5KB)
            save_img = pil_img.copy()
            save_img.thumbnail((250, 250), Image.LANCZOS)
            buffered = io.BytesIO()
            save_img.save(buffered, format="JPEG", quality=60, optimize=True)
            b64_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
            image_path_str = f"data:image/jpeg;base64,{b64_str}"

            # Ek olarak yerel diske de kaydet (opsiyonel)
            try:
                save_img.save(saved_filepath, "JPEG", quality=60, optimize=True)
            except Exception:
                pass

            # AI analiz için ultra-hızlı küçültülmüş görsel (Max 350px - Sub-second yanıt için)
            ai_img = pil_img.copy()
            ai_img.thumbnail((350, 350), Image.LANCZOS)
        except Exception as exc:
            image_path_str = ""
            logger.warning(f"Resim işleme / kaydetme hatası: {exc}")

        # 2. Resim üzerinde basılı barkod var mı kontrol et (pyzbar + zxingcpp)
        scanned_barcode = None
        try:
            np_arr = np.frombuffer(image_bytes, np.uint8)
            frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
            if frame is not None:
                bc_results = decode_barcode_from_frame(frame)
                if bc_results:
                    scanned_barcode = bc_results[0][0]
        except Exception as exc:
            logger.warning(f"Resim içi barkod tarama hatası: {exc}")

        # 3. Gemini Client Kontrolü & Yeniden Başlatma
        if not self.client:
            self._init_client()

        # 4. Gemini Yapay Zeka Çağrısı (Yüksek Hızlı Flash-Lite ve Flash Modelleri)
        ai_extracted = None
        last_error = ""

        if self.client and ai_img:
            prompt = """
            You are an expert Turkish supermarket OCR scanner.
            Examine this product packaging photo.

            Instructions:
            1. Read BRAND NAME (e.g. Nutella, Ülker, Eti, Coca-Cola, Nestlé, Pınar, Sütaş, Ariel, Lays, Nescafé).
            2. Read PRODUCT DESCRIPTION (e.g. Kakaolu Fındık Kreması, Çikolatalı Gofret, Sütlü Çikolata, Çamaşır Deterjanı).
            3. Read WEIGHT / VOLUME if present (e.g. 400g, 1L, 500ml, 36g).

            Format: "[Brand] [Product Description] [Weight/Volume]"

            Return ONLY raw JSON object:
            {
              "name": "Full Professional Turkish Product Name",
              "category": "Best Turkish Category (Gıda, İçecek, Atıştırmalık, Temizlik, Kahvaltılık, Elektronik, Genel)",
              "barcode": "Numeric barcode digits if printed, else null",
              "sale_price": Estimated retail price in TRY float,
              "purchase_price": Estimated wholesale price in TRY float
            }
            """

            # Prioritize fast models for quick sub-second response
            candidate_models = ["gemini-3.5-flash-lite", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-2.5-flash", "gemini-2.0-flash"]

            for model_name in candidate_models:
                try:
                    response_text = ""
                    if not self.legacy_active:
                        # google.genai SDK
                        response = self.client.models.generate_content(
                            model=model_name,
                            contents=[ai_img, prompt],
                        )
                        if response and response.text:
                            response_text = response.text
                    else:
                        # google.generativeai SDK
                        model = self.client.GenerativeModel(model_name)
                        response = model.generate_content([ai_img, prompt])
                        if response and response.text:
                            response_text = response.text

                    if response_text:
                        response_clean = response_text.strip()
                        json_match = re.search(r"\{.*\}", response_clean, re.DOTALL)
                        if json_match:
                            cleaned_json = json_match.group(0)
                            ai_extracted = json.loads(cleaned_json)
                            logger.info(f"Gemini AI ({model_name}) başarıyla hızlı sonuç döndürdü: {ai_extracted.get('name')}")
                            break
                except Exception as exc:
                    last_error = str(exc)
                    logger.warning(f"Model {model_name} denemesi başarısız: {exc}")

        # 5. Sonuçları Derleme
        result_data = {
            "name": (ai_extracted.get("name") if ai_extracted else None) or "",
            "category": (ai_extracted.get("category") if ai_extracted else None) or "Genel",
            "barcode": scanned_barcode or (ai_extracted.get("barcode") if ai_extracted else None),
            "sale_price": (ai_extracted.get("sale_price") if ai_extracted else 0.0) or 0.0,
            "purchase_price": (ai_extracted.get("purchase_price") if ai_extracted else 0.0) or 0.0,
            "image_path": image_path_str,
            "ai_success": bool(ai_extracted and ai_extracted.get("name")),
            "last_error": last_error if not ai_extracted else "",
        }

        if result_data["ai_success"]:
            msg = f"✨ Yapay zeka '{result_data['name']}' ürününü anında teşhis etti!"
        elif not self.api_key:
            msg = "Ambalaj resmi kaydedildi. Yapay zeka için lütfen Gemini API Key tanımlayın."
        elif last_error:
            msg = f"Ambalaj resmi kaydedildi. Yapay zeka hatası: {last_error[:80]}"
        else:
            msg = "Ambalaj resmi kaydedildi."

        return True, msg, result_data
