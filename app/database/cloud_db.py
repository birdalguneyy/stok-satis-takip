import base64
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

# gRPC / Firestore HTTP/2 bağlantı kopmalarını (Render, NAT, Wi-Fi uyku) önlemek için
# firebase_admin / grpc ilk kez yüklenmeden önce keepalive ayarlarını yapılandır.
os.environ.setdefault("GRPC_KEEPALIVE_TIME_MS", "30000")
os.environ.setdefault("GRPC_KEEPALIVE_TIMEOUT_MS", "10000")
os.environ.setdefault("GRPC_KEEPALIVE_PERMIT_WITHOUT_CALLS", "1")
os.environ.setdefault("GRPC_HTTP2_MAX_PINGS_WITHOUT_DATA", "0")
os.environ.setdefault("GRPC_ENABLE_FORK_SUPPORT", "1")
os.environ.setdefault("GRPC_POLL_STRATEGY", "poll")

from app.config import DATA_DIR, DB_PATH, TURKEY_TZ, get_turkey_now, get_turkey_now_str
from app.database.connection import Database
from app.database.migrations import run_migrations
from app.models.product import Product
from app.services.cash_reconciliation_service import (
    analyze_discrepancy_history,
    calculate_day_reconciliation,
    resolve_day_cashier,
)
from app.services.credit_service import from_kurus, to_kurus
from app.services.security_service import (
    CASH_MOVEMENT_TYPES,
    log_audit,
    normalize_role,
    validate_cancel_reason,
)

logger = logging.getLogger(__name__)

# Cloud storage & data directory setup
UPLOADS_DIR = DATA_DIR / "uploads" / "products"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


def normalize_datetime_str(val: Any) -> str:
    """ISO formatındaki (örn. 2026-10-09T14:30:00) veya timezone içeren tarihleri
    standart SQLite 'YYYY-MM-DD HH:MM:SS' formatına dönüştürür."""
    if not val:
        return get_turkey_now_str()
    if isinstance(val, datetime):
        return val.astimezone(TURKEY_TZ).strftime("%Y-%m-%d %H:%M:%S") if val.tzinfo else val.strftime("%Y-%m-%d %H:%M:%S")
    s = str(val).strip().replace("T", " ")
    if "." in s:
        s = s.split(".")[0]
    if len(s) == 10:
        s += " 00:00:00"
    return s[:19]


def _save_base64_to_disk(image_str: Optional[str], uid: int, pid: int) -> str:
    """Base64 data URL formatındaki görseli disk üzerindeki uploads/products klasörüne kaydeder.
    Depolama alanından maksimum tasarruf sağlamak ve cihaz hafızasını şişirmemek için
    görseller kompakt boyuta (max 320x320) ve düşük/optimum kaliteye (JPEG q=50) sıkıştırılarak kaydedilir (~10-20 KB).
    """
    if not image_str:
        return ""
    if image_str == "__REMOVE__":
        return ""
    if image_str.startswith("data:image/"):
        try:
            if "," in image_str:
                _, encoded = image_str.split(",", 1)
            else:
                encoded = image_str
            img_bytes = base64.b64decode(encoded)
            
            # Depolama tasarrufu: PIL ile boyut küçültme ve düşük kalite sıkıştırma
            try:
                from PIL import Image, ImageOps
                import io
                pil_img = Image.open(io.BytesIO(img_bytes))
                pil_img = ImageOps.exif_transpose(pil_img).convert("RGB")
                pil_img.thumbnail((320, 320), Image.LANCZOS)
                buf = io.BytesIO()
                pil_img.save(buf, format="JPEG", quality=50, optimize=True)
                final_bytes = buf.getvalue()
            except Exception as pil_err:
                logger.warning(f"Görsel sıkıştırma uyarısı: {pil_err}")
                final_bytes = img_bytes

            filename = f"prod_{uid}_{pid}.jpg"
            target_file = UPLOADS_DIR / filename
            target_file.write_bytes(final_bytes)
            return f"/uploads/products/{filename}?v={int(time.time())}"
        except Exception as e:
            logger.warning(f"Görsel diske kaydedilemedi: {e}")
            return image_str
    return image_str


class CloudDatabase:
    """7/24 Bulut (Firebase Firestore / SQLite Hibrit) Veritabanı Yöneticisi."""

    _instance: Optional["CloudDatabase"] = None

    def __new__(cls) -> "CloudDatabase":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self.db = Database()
        try:
            run_migrations()
        except Exception as exc:
            logger.warning(f"Migration otomatik çalıştırma uyarısı: {exc}")
        self.firestore_db = None
        self.firebase_last_error = None
        self.firebase_cred_source = None
        self.firebase_project_id = None
        self._firebase_lock = threading.RLock()
        self._firebase_last_ok_ts = 0.0
        self._firebase_last_health_check_ts = 0.0
        self._firebase_consecutive_errors = 0
        self._firebase_last_reconnect_ts = 0.0
        self._firebase_quota_blocked_until = 0.0
        # Clean orphan rows from older single-user versions by attaching them to primary user 1
        try:
            with self.db.get_connection() as conn:
                conn.execute("UPDATE products SET user_id = 1 WHERE user_id IS NULL")
                conn.execute("UPDATE categories SET user_id = 1 WHERE user_id IS NULL")
                conn.execute("UPDATE sales SET user_id = 1 WHERE user_id IS NULL")
                conn.execute("UPDATE expenses SET user_id = 1 WHERE user_id IS NULL")
                conn.execute("UPDATE store_settings SET user_id = 1 WHERE user_id IS NULL")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_sales_user_date ON sales(user_id, sold_at)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_expenses_user_date ON expenses(user_id, expense_date)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_sale_items_sale_prod ON sale_items(sale_id, product_name)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_farm_egg_user_date ON farm_egg_sales(user_id, sale_date)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_farm_feed_user_date ON farm_feed_purchases(user_id, purchase_date)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_farm_cust_user_name ON farm_customers(user_id, name)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_products_user_active ON products(user_id, is_active)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_products_barcode ON products(barcode)")
                try:
                    conn.execute("ALTER TABLE products ADD COLUMN unit TEXT NOT NULL DEFAULT 'adet'")
                except Exception:
                    pass
                try:
                    conn.execute("ALTER TABLE sale_items ADD COLUMN unit TEXT NOT NULL DEFAULT 'adet'")
                except Exception:
                    pass
        except Exception:
            pass

        self._hydration_completed = False
        self._hydration_lock = threading.RLock()
        self._last_pull_timestamp = 0.0
        self._farm_sale_lock = threading.Lock()
        self._recent_farm_sale_hashes: Dict[str, tuple[float, int, str]] = {}

        self._init_firebase_optional()
        try:
            self._cleanup_duplicate_farm_egg_sales()
            self.sync_shop_egg_sales_to_farm()
        except Exception:
            pass
        self._initialized = True


    @staticmethod
    def _parse_cred_dict(raw: Any) -> Optional[dict]:
        """Verilen nesneyi (dict, JSON string, Base64 vb.) Firebase Service Account sözlüğüne çevirir."""
        if not raw:
            return None
        c_dict = None
        if isinstance(raw, dict):
            c_dict = dict(raw)
        elif isinstance(raw, str):
            s = raw.strip()
            # Tırnak temizliği (örnek: '"{"type": ...}"' veya ''{"type": ...}'')
            while (s.startswith("'") and s.endswith("'")) or (s.startswith('"') and s.endswith('"')):
                s = s[1:-1].strip()
            # Base64 kontrolü
            if not s.startswith("{"):
                try:
                    dec = base64.b64decode(s).decode("utf-8", errors="ignore").strip()
                    if dec.startswith("{"):
                        s = dec
                except Exception:
                    pass
            try:
                c_dict = json.loads(s, strict=False)
                if isinstance(c_dict, str):
                    c_dict = json.loads(c_dict, strict=False)
            except Exception:
                return None

        if isinstance(c_dict, dict) and "private_key" in c_dict:
            pk = str(c_dict.get("private_key", ""))
            c_dict["private_key"] = pk.replace("\\n", "\n").replace("\\\\n", "\n")
            return c_dict
        return None

    def _init_firebase_optional(self, force: bool = False) -> None:
        """Firebase Admin / Firestore bağlantısını thread-safe şekilde başlatır veya yeniler."""
        with self._firebase_lock:
            try:
                import firebase_admin
                from firebase_admin import credentials, firestore

                if force and self.firestore_db is not None:
                    try:
                        close_fn = getattr(self.firestore_db, "close", None)
                        if callable(close_fn):
                            close_fn()
                    except Exception:
                        pass
                    self.firestore_db = None

                if force and firebase_admin._apps:
                    for app_name in list(firebase_admin._apps.keys()):
                        try:
                            firebase_admin.delete_app(firebase_admin._apps[app_name])
                        except Exception:
                            pass
                    self.firestore_db = None

                if not force and firebase_admin._apps and self.firestore_db is not None:
                    return

                if not force and firebase_admin._apps and self.firestore_db is None:
                    try:
                        self.firestore_db = firestore.client()
                        self.firebase_last_error = None
                        self._firebase_consecutive_errors = 0
                        self._firebase_last_ok_ts = time.time()
                        logger.info("Firebase Firestore mevcut uygulamadan bağlandı.")
                        return
                    except Exception as e:
                        logger.warning(f"Firebase Firestore istemci alma hatası: {e}")
                        self.firebase_last_error = str(e)

                cred = None
                source_name = None
                detected_project_id = None

                # 1. Ortam Değişkenleri Kontrolü (Render, Cloud veya Yerel)
                env_var_names = [
                    "FIREBASE_CREDENTIALS_JSON",
                    "FIREBASE_CREDENTIALS",
                    "GOOGLE_APPLICATION_CREDENTIALS",
                    "FIREBASE_SERVICE_ACCOUNT",
                    "FIREBASE_SERVICE_ACCOUNT_KEY",
                    "FIREBASE_KEY",
                    "FIREBASE_CONFIG",
                    "FIREBASE_ADMIN_CREDENTIALS",
                    "FIREBASE_CREDENTIALS_PATH",
                ]
                for var_name in env_var_names:
                    val = os.environ.get(var_name)
                    if not val or not val.strip():
                        continue
                    val = val.strip()
                    # Değer bir dosya yolu mu?
                    try:
                        p = Path(val)
                        if p.exists() and p.is_file():
                            try:
                                file_dict = self._parse_cred_dict(p.read_text(encoding="utf-8"))
                                if file_dict:
                                    cred = credentials.Certificate(file_dict)
                                    detected_project_id = file_dict.get("project_id")
                                else:
                                    cred = credentials.Certificate(str(p))
                                source_name = f"Ortam Değişkeni Dosya Yolu ({var_name}={p.name})"
                                logger.info(f"Firebase kimlik bilgileri {source_name} üzerinden yüklendi.")
                                break
                            except Exception as e_path:
                                logger.warning(f"{var_name} dosyasından sertifika alma uyarısı: {e_path}")
                    except Exception:
                        pass

                    # Değer doğrudan JSON / Base64 içeriği mi?
                    c_dict = self._parse_cred_dict(val)
                    if c_dict:
                        try:
                            cred = credentials.Certificate(c_dict)
                            detected_project_id = c_dict.get("project_id")
                            source_name = f"Ortam Değişkeni JSON ({var_name})"
                            logger.info(f"Firebase kimlik bilgileri {source_name} üzerinden yüklendi.")
                            break
                        except Exception as e_cert:
                            logger.warning(f"{var_name} JSON sertifika alma uyarısı: {e_cert}")

                # 2. Disk Üzerindeki Aday Dosyalar Kontrolü
                if not cred:
                    candidate_paths = [
                        DATA_DIR / "firebase_credentials.json",
                        DATA_DIR / "firebase_key.json",
                        DATA_DIR.parent / "firebase_credentials.json",
                        DATA_DIR.parent / "firebase_key.json",
                        Path(__file__).resolve().parent.parent.parent / "firebase_credentials.json",
                        Path(__file__).resolve().parent.parent.parent / "firebase-credentials.json",
                        Path(__file__).resolve().parent.parent.parent / "serviceAccountKey.json",
                        Path(__file__).resolve().parent.parent.parent / "firebase-key.json",
                        Path.cwd() / "firebase_credentials.json",
                        Path.cwd() / "firebase-credentials.json",
                        Path.cwd() / "serviceAccountKey.json",
                        Path.cwd() / "firebase-key.json",
                        Path("/etc/secrets/firebase_credentials.json"),
                        Path("/etc/secrets/firebase-credentials.json"),
                        Path("/etc/secrets/serviceAccountKey.json"),
                        Path("/etc/secrets/firebase-key.json"),
                        Path("/etc/secrets/service_account.json"),
                        Path("/etc/secrets/google-services.json"),
                    ]
                    for p in candidate_paths:
                        try:
                            if p.exists() and p.is_file():
                                c_dict = self._parse_cred_dict(p.read_text(encoding="utf-8"))
                                if c_dict:
                                    cred = credentials.Certificate(c_dict)
                                    detected_project_id = c_dict.get("project_id")
                                else:
                                    cred = credentials.Certificate(str(p))
                                source_name = f"Yerel Dosya ({p})"
                                logger.info(f"Firebase kimlik bilgileri {source_name} üzerinden yüklendi.")
                                break
                        except Exception as e_cand:
                            logger.warning(f"Aday dosya ({p}) okuma uyarısı: {e_cand}")

                # 3. Google Application Default Credentials Fallback
                if not cred and any(k in os.environ for k in ("GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT", "GCP_PROJECT")):
                    try:
                        cred = credentials.ApplicationDefault()
                        source_name = "Application Default Credentials"
                    except Exception:
                        pass

                if cred:
                    try:
                        firebase_admin.initialize_app(cred)
                    except ValueError:
                        pass
                    self.firestore_db = firestore.client()
                    self.firebase_cred_source = source_name
                    self.firebase_project_id = detected_project_id or getattr(cred, "project_id", None)
                    self.firebase_last_error = None
                    self._firebase_consecutive_errors = 0
                    self._firebase_last_ok_ts = time.time()
                    self._firebase_last_reconnect_ts = time.time()
                    logger.info(f"Firebase Firestore başarıyla aktifleştirildi ({source_name}). Proje: {self.firebase_project_id}")
                else:
                    self.firestore_db = None
                    self.firebase_cred_source = None
                    self.firebase_last_error = "Firebase kimlik bilgisi (Service Account JSON) bulunamadı. Lütfen Firebase ayarlarını yapılandırınız."
                    logger.info("Firebase kimlik bilgileri bulunamadı, SQLite yerel mod aktif.")
            except Exception as exc:
                self.firestore_db = None
                self.firebase_last_error = f"Firebase istemci başlatma hatası: {exc}"
                logger.warning(f"Firebase Firestore istemcisi çevrimdışı modda başlatıldı: {exc}")

    def configure_firebase(self, cred_input: Union[str, dict]) -> tuple[bool, str]:
        """save_firebase_credentials için geriye dönük uyumlu takma ad."""
        return self.save_firebase_credentials(cred_input)

    @staticmethod
    def _is_quota_error(exc: Exception) -> bool:
        msg = f"{type(exc).__name__}: {exc}".lower()
        return any(k in msg for k in ("quota exceeded", "resourceexhausted", "resource_exhausted", "429"))

    @staticmethod
    def _is_transient_firebase_error(exc: Exception) -> bool:
        """gRPC kanal kopması, zaman aşımı, DNS/ağ kesintisi veya token yenileme hatası mı?"""
        if CloudDatabase._is_quota_error(exc):
            return False
        msg = f"{type(exc).__name__}: {exc}".lower()
        transient_markers = (
            "unavailable",
            "deadlineexceeded",
            "deadline exceeded",
            "503",
            "504",
            "goaway",
            "rst_stream",
            "broken pipe",
            "connection reset",
            "connection aborted",
            "connection refused",
            "failed to connect",
            "socket closed",
            "transport closed",
            "inactiverpcerror",
            "internalservererror",
            "serviceunavailable",
            "aborted",
            "eof",
            "ssl",
            "tls",
            "dns",
            "timed out",
            "timeout",
            "refresherror",
            "transport",
            "channel",
            "stream removed",
        )
        return any(m in msg for m in transient_markers)

    def _reset_firebase_client(self, reason: str = "") -> bool:
        """Bozulmuş/kopmuş gRPC kanalını güvenle kapatıp taze Firestore istemcisi açar."""
        with self._firebase_lock:
            now_ts = time.time()
            # Çok sık yeniden başlatmayı önle (en az 3 sn ara)
            if self.firestore_db is not None and (now_ts - self._firebase_last_reconnect_ts) < 3.0:
                return self.firestore_db is not None
            logger.warning(f"Firebase gRPC istemcisi yeniden başlatılıyor ({reason or 'bağlantı kopması'})...")
            self._init_firebase_optional(force=True)
            return self.firestore_db is not None

    def _firestore_call_with_retry(
        self,
        fn: Callable[[Any], Any],
        op_name: str = "firestore_op",
        max_retries: int = 3,
    ) -> Any:
        """Firestore çağrısını geçici bağlantı kopmalarında otomatik yeniden bağlanarak çalıştırır."""
        if time.time() < self._firebase_quota_blocked_until:
            raise RuntimeError("Firestore günlük kota sınırı bekleme süresinde.")

        last_exc: Optional[Exception] = None
        for attempt in range(max_retries):
            if not self.ensure_firebase():
                raise RuntimeError(self.firebase_last_error or "Firebase istemcisi hazır değil.")
            client = self.firestore_db
            try:
                res = fn(client)
                self._firebase_last_ok_ts = time.time()
                self._firebase_consecutive_errors = 0
                if self.firebase_last_error and "kimlik" not in str(self.firebase_last_error).lower():
                    self.firebase_last_error = None
                return res
            except Exception as exc:
                last_exc = exc
                self._firebase_consecutive_errors += 1
                if self._is_quota_error(exc):
                    self.firebase_last_error = "Firestore günlük kota sınırı aşıldı."
                    self._firebase_quota_blocked_until = time.time() + 300.0
                    logger.warning(f"[{op_name}] Firestore kota uyarısı (5 dk bekleme): {exc}")
                    raise
                if not self._is_transient_firebase_error(exc) or attempt >= (max_retries - 1):
                    self.firebase_last_error = f"{op_name} hatası: {exc}"
                    raise
                backoff = min(0.5 * (2 ** attempt), 3.0)
                logger.warning(
                    f"[{op_name}] Geçici Firebase bağlantı hatası (deneme {attempt + 1}/{max_retries}): {exc} "
                    f"-> {backoff:.1f}sn sonra yeniden bağlanılıyor..."
                )
                self._reset_firebase_client(reason=f"{op_name}: {type(exc).__name__}")
                time.sleep(backoff)

        if last_exc:
            raise last_exc
        return None

    def check_firebase_health(self, force: bool = False) -> bool:
        """Firebase bağlantısının sağlıklı olduğundan emin olur; kopmuşsa veya ardışık hata varsa otomatik yeniler.
        Sağlıklı bağlantıda gereksiz Firestore READ harcamaz.
        """
        now_ts = time.time()
        if not self.ensure_firebase():
            return False

        # Son 2 dakika içinde başarılı bir işlem olduysa ve hata yoksa ekstra okuma yapma (0 READ)
        if not force and self._firebase_consecutive_errors == 0 and (now_ts - self._firebase_last_ok_ts) < 120.0:
            return True

        # Sağlık kontrolünü en fazla 60 saniyede bir çalıştır (force değilse)
        if not force and (now_ts - self._firebase_last_health_check_ts) < 60.0:
            return self.firestore_db is not None

        self._firebase_last_health_check_ts = now_ts
        if self._firebase_consecutive_errors >= 2:
            return self._reset_firebase_client(reason="ardışık bağlantı hatası")

        return self.firestore_db is not None

    def get_firebase_health_info(self) -> Dict[str, Any]:
        """API durum endpointleri için Firebase bağlantı sağlık özetini döndürür."""
        return {
            "connected": bool(self.firestore_db is not None),
            "healthy": bool(self.firestore_db is not None and self._firebase_consecutive_errors < 3),
            "consecutive_errors": int(self._firebase_consecutive_errors),
            "last_ok_ts": float(self._firebase_last_ok_ts),
            "quota_blocked": bool(time.time() < self._firebase_quota_blocked_until),
        }

    def save_firebase_credentials(self, cred_input: Union[str, dict]) -> tuple[bool, str]:
        """Arayüzden veya API'den gelen Firebase Service Account JSON kimlik bilgilerini doğrular,
        DATA_DIR / 'firebase_credentials.json' dosyasına kaydeder ve bağlantıyı anında kurar.
        """
        try:
            c_dict = self._parse_cred_dict(cred_input)
            if not c_dict:
                return False, "Geçersiz kimlik formatı! Lütfen geçerli bir Firebase Service Account JSON metni giriniz."

            if "private_key" not in c_dict or ("project_id" not in c_dict and "client_email" not in c_dict):
                return False, "Eksik alan: JSON anahtarında 'private_key' ve 'project_id' veya 'client_email' bulunmalıdır."

            target_file = DATA_DIR / "firebase_credentials.json"
            target_file.parent.mkdir(parents=True, exist_ok=True)
            target_file.write_text(json.dumps(c_dict, indent=2, ensure_ascii=False), encoding="utf-8")

            self._init_firebase_optional(force=True)

            if not self.firestore_db:
                err_msg = self.firebase_last_error or "Firestore istemcisi başlatılamadı."
                return False, f"Kimlik kaydedildi ancak Firebase bağlantısı kurulamadı: {err_msg}"

            # Hızlı bağlantı testi
            try:
                list(self.firestore_db.collection("users").limit(1).stream())
            except Exception as test_err:
                logger.warning(f"Firestore test sorgusu uyarısı: {test_err}")
                err_str = str(test_err)
                if "Quota exceeded" in err_str or "ResourceExhausted" in err_str:
                    self.firebase_last_error = "Firestore günlük kota sınırı aşıldı."
                    return False, "Firebase bağlantısı sağlandı ancak günlük okuma kotası (50.000 limit) aşıldığı için Firestore sorguları reddediliyor."

            # İlk veri çekimini (initial hydration) derhal gerçekleştir
            try:
                self.ensure_initial_hydration(force=True)
            except Exception as hyd_err:
                logger.warning(f"save_firebase_credentials hydration uyarısı: {hyd_err}")

            proj = self.firebase_project_id or c_dict.get("project_id", "")
            pulled_info = f" ({self._last_pulled_count} kayıt aktarıldı)" if getattr(self, "_last_pulled_count", 0) > 0 else ""
            return True, f"Firebase bağlantısı başarıyla kuruldu! (Proje: {proj}){pulled_info}"
        except Exception as exc:
            logger.exception("save_firebase_credentials hatası")
            return False, f"Kimlik kaydetme hatası: {exc}"

    def ensure_firebase(self) -> bool:
        """Firebase bağlantısının hazır olduğunu doğrular; kopmuşsa thread-safe şekilde yeniden bağlanmayı dener."""
        if self.firestore_db is not None:
            return True
        with self._firebase_lock:
            if self.firestore_db is not None:
                return True
            self._init_firebase_optional()
            return self.firestore_db is not None

    def ensure_initial_hydration(self, force: bool = False) -> bool:
        """Render konteyneri yeni dağıtıldığında veya SQLite sıfırlandığında,
        buluttaki tüm verileri SQLite'a aktarır.
        SQLite'ta gerçek veri varsa (demo harici ürün ve satışlar) Firestore'u boş yere okumaz (READ harcamaz).
        """
        if not self.ensure_firebase():
            return False

        with self._hydration_lock:
            if self._hydration_completed and not force:
                return True

            with self.db.get_connection() as conn:
                real_p_cnt = conn.execute("SELECT COUNT(*) FROM products WHERE barcode NOT LIKE '86900000000%'").fetchone()[0]
                s_cnt = conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0]
                es_cnt = conn.execute("SELECT COUNT(*) FROM farm_egg_sales").fetchone()[0]

            # Sadece hem gerçek ürünler hem de satışlar (mağaza veya çiftlik) zaten yerelde mevcutsa atla
            if not force and real_p_cnt > 0 and (s_cnt > 0 or es_cnt > 0):
                self._hydration_completed = True
                return True

            now_ts = time.time()
            if not force and (now_ts - self._last_pull_timestamp < 30):
                return True

            logger.info("Firestore ilk veri yüklemesi (initial hydration) başlatılıyor...")
            try:
                pulled = self.pull_all_from_firebase()
                self._last_pulled_count = pulled
                self._hydration_completed = True
                self._last_pull_timestamp = time.time()
                logger.info(f"Firestore ilk veri yüklemesi tamamlandı: {pulled} kayıt yerel SQLite'a aktarıldı.")
                return True
            except Exception as e:
                logger.warning(f"Firestore initial hydration hatası: {e}")
                return False

    # ════════════════════════════════════════════════════════════════════
    # BULUTTAN YEREL VERİTABANINA TAM VERİ AKTARIMI (HYDRATION)
    # ════════════════════════════════════════════════════════════════════

    def pull_all_from_firebase(self, user_id: Optional[int] = None) -> int:
        """Firestore üzerindeki verileri yerel SQLite veritabanına aktarır.
        Eğer user_id belirtilmişse SADECE o kullanıcıya ait verileri çeker (Kullanıcılar Arası Tam İzolasyon!).
        """
        if not self.firestore_db:
            return 0

        pulled_count = 0
        now = get_turkey_now_str()

        try:
            with self.db.get_connection() as conn:
                conn.execute("PRAGMA foreign_keys = OFF")

                # 1. PULL USERS
                try:
                    user_docs = self.firestore_db.collection("users").stream()
                    for doc in user_docs:
                        d = doc.to_dict()
                        uid = d.get("id") or (int(doc.id) if doc.id.isdigit() else None)
                        if uid:
                            conn.execute(
                                """
                                INSERT OR REPLACE INTO users (
                                    id, company_name, full_name, phone, email, password_hash, auth_token,
                                    synced_to_cloud, created_at, updated_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                                """,
                                (
                                    uid,
                                    d.get("company_name", ""),
                                    d.get("full_name", ""),
                                    d.get("phone", ""),
                                    d.get("email", ""),
                                    d.get("password_hash", ""),
                                    d.get("auth_token", ""),
                                    d.get("created_at", now),
                                    d.get("updated_at", now),
                                ),
                            )
                            pulled_count += 1
                except Exception as e:
                    logger.warning(f"Kullanıcıları buluttan çekme hatası: {e}")

                # 2. PULL CATEGORIES
                try:
                    cat_docs = self.firestore_db.collection("categories").stream()
                    for doc in cat_docs:
                        d = doc.to_dict()
                        cid = d.get("id")
                        if cid is None:
                            if doc.id.isdigit():
                                cid = int(doc.id)
                            elif "_c" in doc.id:
                                try:
                                    cid = int(doc.id.split("_c")[-1])
                                except Exception:
                                    pass
                            if cid is None:
                                nums = re.findall(r"\d+", doc.id)
                                if nums:
                                    cid = int(nums[-1])
                            if cid is None:
                                max_c = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM categories").fetchone()[0]
                                cid = int(max_c)
                        else:
                            try:
                                cid = int(cid)
                            except Exception:
                                pass

                        doc_user_id = d.get("user_id")
                        if doc_user_id is None and doc.id.startswith("u") and "_" in doc.id:
                            try:
                                doc_user_id = int(doc.id.split("_")[0][1:])
                            except Exception:
                                pass
                        try:
                            final_uid = int(doc_user_id) if doc_user_id is not None else (int(user_id) if user_id is not None else 1)
                        except Exception:
                            final_uid = 1

                        if cid and d.get("name"):
                            conn.execute(
                                """
                                INSERT OR REPLACE INTO categories (
                                    id, name, user_id, synced_to_cloud
                                ) VALUES (?, ?, ?, 1)
                                """,
                                (cid, d.get("name"), final_uid),
                            )
                            pulled_count += 1
                except Exception as e:
                    logger.warning(f"Kategorileri buluttan çekme hatası: {e}")

                # 3. PULL PRODUCTS
                try:
                    p_docs = self.firestore_db.collection("products").stream()
                    for doc in p_docs:
                        try:
                            d = doc.to_dict()
                            pid = d.get("id")
                            if pid is None:
                                if doc.id.isdigit():
                                    pid = int(doc.id)
                                elif "_p" in doc.id:
                                    try:
                                        pid = int(doc.id.split("_p")[-1])
                                    except Exception:
                                        pass
                                if pid is None:
                                    nums = re.findall(r"\d+", doc.id)
                                    if nums:
                                        pid = int(nums[-1])
                                if pid is None:
                                    max_p = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM products").fetchone()[0]
                                    pid = int(max_p)
                            else:
                                try:
                                    pid = int(pid)
                                except Exception:
                                    pass

                            doc_user_id = d.get("user_id")
                            if doc_user_id is None and doc.id.startswith("u") and "_" in doc.id:
                                try:
                                    doc_user_id = int(doc.id.split("_")[0][1:])
                                except Exception:
                                    pass
                            try:
                                final_uid = int(doc_user_id) if doc_user_id is not None else (int(user_id) if user_id is not None else 1)
                            except Exception:
                                final_uid = 1

                            if pid and d.get("name"):
                                existing_p = conn.execute(
                                    "SELECT synced_to_cloud FROM products WHERE id = ?",
                                    (pid,),
                                ).fetchone()
                                if existing_p and int(existing_p["synced_to_cloud"] or 0) == 0:
                                    continue

                                cat_id = d.get("category_id") or 1
                                try:
                                    cat_id = int(cat_id)
                                except Exception:
                                    cat_id = 1
                                # Ensure category exists for foreign key
                                conn.execute(
                                    "INSERT OR IGNORE INTO categories (id, name, user_id, synced_to_cloud) VALUES (?, 'Genel', ?, 1)",
                                    (cat_id, final_uid),
                                )
                                pur_price = max(0.0, float(d.get("purchase_price") or 0.0))
                                sal_price = max(0.0, float(d.get("sale_price") or 0.0))
                                stk_qty = max(0.0, float(d.get("stock_quantity") or 0.0))
                                crit_level = max(0.0, float(d.get("critical_stock_level") or 5.0))
                                conn.execute(
                                    """
                                    INSERT OR REPLACE INTO products (
                                        id, user_id, category_id, name, barcode, purchase_price, sale_price,
                                        stock_quantity, critical_stock_level, unit, image_path, is_active, synced_to_cloud, created_at, updated_at
                                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                                    """,
                                    (
                                        pid,
                                        final_uid,
                                        cat_id,
                                        str(d.get("name", "Ürün")),
                                        str(d.get("barcode") or f"KOD{pid}"),
                                        pur_price,
                                        sal_price,
                                        stk_qty,
                                        crit_level,
                                        str(d.get("unit") or "adet"),
                                        str(d.get("image_path") or ""),
                                        int(d.get("is_active", 1)),
                                        normalize_datetime_str(d.get("created_at")),
                                        normalize_datetime_str(d.get("updated_at")),
                                    ),
                                )
                                pulled_count += 1
                        except Exception as p_item_err:
                            logger.warning(f"Ürün satırı çekme hatası (doc {doc.id}): {p_item_err}")

                    try:
                        max_p = conn.execute("SELECT COALESCE(MAX(id), 0) FROM products").fetchone()[0]
                        if max_p:
                            conn.execute("INSERT OR REPLACE INTO sqlite_sequence (name, seq) VALUES ('products', ?)", (max_p,))
                    except Exception:
                        pass
                except Exception as e:
                    logger.warning(f"Ürünleri buluttan çekme hatası: {e}")

                # 4. PULL SALES & SALE ITEMS
                try:
                    sales_docs = self.firestore_db.collection("sales").stream()
                    for doc in sales_docs:
                        try:
                            d = doc.to_dict() or {}
                            sid = d.get("id")
                            if sid is None:
                                if doc.id.isdigit():
                                    sid = int(doc.id)
                                elif "_s" in doc.id:
                                    try:
                                        sid = int(doc.id.split("_s")[-1])
                                    except Exception:
                                        pass
                                if sid is None:
                                    nums = re.findall(r"\d+", doc.id)
                                    if nums:
                                        sid = int(nums[-1])
                                if sid is None:
                                    max_s = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM sales").fetchone()[0]
                                    sid = int(max_s)
                            else:
                                try:
                                    sid = int(sid)
                                except Exception:
                                    pass

                            doc_user_id = d.get("user_id")
                            if doc_user_id is None and doc.id.startswith("u") and "_" in doc.id:
                                try:
                                    doc_user_id = int(doc.id.split("_")[0][1:])
                                except Exception:
                                    pass
                            try:
                                final_uid = int(doc_user_id) if doc_user_id is not None else (int(user_id) if user_id is not None else 1)
                            except Exception:
                                final_uid = 1

                            if sid:
                                s_at = normalize_datetime_str(d.get("sold_at") or d.get("created_at"))
                                # ID çakışma önleme: Farklı bir satışın üzerine yazılmasını engelle
                                existing_sale = conn.execute("SELECT sold_at, total_amount, synced_to_cloud FROM sales WHERE id = ?", (sid,)).fetchone()
                                if existing_sale:
                                    ex_sold = existing_sale["sold_at"]
                                    ex_tot = float(existing_sale["total_amount"] or 0)
                                    ex_synced = int(existing_sale["synced_to_cloud"] if existing_sale["synced_to_cloud"] is not None else 1)
                                    cur_tot = float(d.get("total_amount", 0))
                                    if ex_sold != s_at or abs(ex_tot - cur_tot) > 0.05:
                                        max_s = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM sales").fetchone()[0]
                                        new_sid = max(int(max_s), int(sid) + 1)
                                        if ex_synced == 0:
                                            conn.execute("UPDATE sales SET id = ? WHERE id = ?", (new_sid, sid))
                                            conn.execute("UPDATE sale_items SET sale_id = ? WHERE sale_id = ?", (new_sid, sid))
                                        else:
                                            sid = new_sid

                                try:
                                    raw_item_cnt = int(d.get("item_count") or 0)
                                except Exception:
                                    raw_item_cnt = 0
                                items_len = len(d.get("items") or [])
                                safe_item_count = max(1, raw_item_cnt, items_len)

                                conn.execute(
                                    """
                                    INSERT OR REPLACE INTO sales (
                                        id, user_id, total_amount, item_count, sold_at, note, channel, customer_name, synced_to_cloud
                                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                                    """,
                                    (
                                        sid,
                                        final_uid,
                                        float(d.get("total_amount", 0)),
                                        safe_item_count,
                                        s_at,
                                        d.get("note", "Satış"),
                                        d.get("channel", "magaza"),
                                        d.get("customer_name"),
                                    ),
                                )
                                # Sale items
                                items = d.get("items", [])
                                conn.execute("DELETE FROM sale_items WHERE sale_id = ?", (sid,))
                                for it in items:
                                    it_pid = it.get("product_id")
                                    if it_pid is not None:
                                        try:
                                            it_pid = int(it_pid)
                                        except Exception:
                                            it_pid = None
                                    if it_pid is not None:
                                        p_chk = conn.execute("SELECT id FROM products WHERE id = ?", (it_pid,)).fetchone()
                                        if not p_chk:
                                            it_pid = None
                                    if it_pid is None:
                                        fb_row = conn.execute("SELECT id FROM products ORDER BY id ASC LIMIT 1").fetchone()
                                        it_pid = int(fb_row[0]) if fb_row else 1
                                    conn.execute(
                                        """
                                        INSERT INTO sale_items (sale_id, product_id, product_name, barcode, unit_price, quantity, subtotal, unit)
                                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                        """,
                                        (
                                            sid,
                                            it_pid,
                                            it.get("product_name") or it.get("name") or "",
                                            it.get("barcode", ""),
                                            float(it.get("unit_price") or it.get("unitPrice") or 0),
                                            float(it.get("quantity") or it.get("qty") or 1),
                                            float(it.get("subtotal") or it.get("totalPrice") or 0),
                                            str(it.get("unit", "adet")),
                                        ),
                                    )
                                pulled_count += 1
                        except Exception as s_item_err:
                            logger.warning(f"Satış satırı çekme uyarısı (doc {doc.id}): {s_item_err}")
                except Exception as e:
                    logger.warning(f"Satışları buluttan çekme hatası: {e}")

                # 5. PULL EXPENSES
                try:
                    exp_docs = self.firestore_db.collection("expenses").stream()
                    for doc in exp_docs:
                        d = doc.to_dict()
                        eid = d.get("id")
                        if eid is None:
                            if doc.id.isdigit():
                                eid = int(doc.id)
                            elif "_e" in doc.id:
                                try:
                                    eid = int(doc.id.split("_e")[-1])
                                except Exception:
                                    pass
                            if eid is None:
                                nums = re.findall(r"\d+", doc.id)
                                if nums:
                                    eid = int(nums[-1])
                            if eid is None:
                                max_e = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM expenses").fetchone()[0]
                                eid = int(max_e)
                        else:
                            try:
                                eid = int(eid)
                            except Exception:
                                pass

                        doc_user_id = d.get("user_id")
                        if doc_user_id is None and doc.id.startswith("u") and "_" in doc.id:
                            try:
                                doc_user_id = int(doc.id.split("_")[0][1:])
                            except Exception:
                                pass
                        try:
                            final_uid = int(doc_user_id) if doc_user_id is not None else (int(user_id) if user_id is not None else 1)
                        except Exception:
                            final_uid = 1

                        if eid and d.get("title"):
                            raw_e_date = str(d.get("expense_date") or "").strip()
                            e_date = raw_e_date[:10] if len(raw_e_date) >= 10 else now[:10]
                            conn.execute(
                                """
                                INSERT OR REPLACE INTO expenses (
                                    id, user_id, title, amount, category, expense_date, note, synced_to_cloud, created_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                                """,
                                (
                                    eid,
                                    final_uid,
                                    d.get("title"),
                                    float(d.get("amount", 0)),
                                    d.get("category", "Fatura"),
                                    e_date,
                                    d.get("note", ""),
                                    normalize_datetime_str(d.get("created_at")),
                                ),
                            )
                            pulled_count += 1
                except Exception as e:
                    logger.warning(f"Giderleri buluttan çekme hatası: {e}")

                # 6. PULL STORE SETTINGS & GEMINI API KEY
                try:
                    settings_docs = self.firestore_db.collection("store_settings").stream()
                    for doc in settings_docs:
                        d = doc.to_dict()
                        suid = d.get("user_id") or (int(doc.id.replace("u", "")) if doc.id.replace("u", "").isdigit() else 1)
                        try:
                            suid = int(suid)
                        except Exception:
                            suid = 1
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO store_settings (user_id, weekday_hours, weekend_hours)
                            VALUES (?, ?, ?)
                            """,
                            (suid, d.get("weekday_hours", "08:00 - 22:00"), d.get("weekend_hours", "09:00 - 23:00")),
                        )
                except Exception as e:
                    logger.warning(f"İşletme ayarlarını buluttan çekme hatası: {e}")

                # 6.1 PULL GEMINI API KEY FROM FIRESTORE
                try:
                    g_doc = self.firestore_db.collection("system_settings").document("gemini").get()
                    if g_doc.exists:
                        g_data = g_doc.to_dict()
                        remote_key = (g_data.get("api_key") or "").strip()
                        if remote_key:
                            key_file = DATA_DIR / "gemini_key.txt"
                            key_file.write_text(remote_key, encoding="utf-8")
                            logger.info("Gemini API Key Firebase Firestore'dan başarıyla yerel ortama aktarıldı.")
                except Exception as e:
                    logger.warning(f"Firestore Gemini Key çekme uyarısı: {e}")

                # 6.2 PULL FARM CUSTOMERS
                try:
                    fc_docs = self.firestore_db.collection("farm_customers").stream()
                    for doc in fc_docs:
                        d = doc.to_dict()
                        fc_id = d.get("id")
                        if fc_id is None:
                            if doc.id.isdigit():
                                fc_id = int(doc.id)
                            elif "_fc" in doc.id:
                                try:
                                    fc_id = int(doc.id.split("_fc")[-1])
                                except Exception:
                                    pass
                            if fc_id is None:
                                nums = re.findall(r"\d+", doc.id)
                                if nums:
                                    fc_id = int(nums[-1])
                            if fc_id is None:
                                max_fc = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM farm_customers").fetchone()[0]
                                fc_id = int(max_fc)
                        else:
                            try:
                                fc_id = int(fc_id)
                            except Exception:
                                pass

                        doc_user_id = d.get("user_id")
                        if doc_user_id is None and doc.id.startswith("u") and "_" in doc.id:
                            try:
                                doc_user_id = int(doc.id.split("_")[0][1:])
                            except Exception:
                                pass
                        try:
                            final_uid = int(doc_user_id) if doc_user_id is not None else (int(user_id) if user_id is not None else 1)
                        except Exception:
                            final_uid = 1

                        if d.get("name"):
                            conn.execute(
                                """
                                INSERT OR REPLACE INTO farm_customers (
                                    id, user_id, name, phone, synced_to_cloud, created_at
                                ) VALUES (?, ?, ?, ?, 1, ?)
                                """,
                                (fc_id, final_uid, d.get("name"), d.get("phone", ""), normalize_datetime_str(d.get("created_at"))),
                            )
                            pulled_count += 1
                except Exception as e:
                    logger.warning(f"Çiftlik müşterilerini buluttan çekme hatası: {e}")

                # 6.3 PULL FARM EGG SALES
                try:
                    es_docs = self.firestore_db.collection("farm_egg_sales").stream()
                    for doc in es_docs:
                        d = doc.to_dict()
                        es_id = d.get("id")
                        if es_id is None:
                            if doc.id.isdigit():
                                es_id = int(doc.id)
                            elif "_es" in doc.id:
                                try:
                                    es_id = int(doc.id.split("_es")[-1])
                                except Exception:
                                    pass
                            if es_id is None:
                                nums = re.findall(r"\d+", doc.id)
                                if nums:
                                    es_id = int(nums[-1])
                            if es_id is None:
                                max_es = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM farm_egg_sales").fetchone()[0]
                                es_id = int(max_es)
                        else:
                            try:
                                es_id = int(es_id)
                            except Exception:
                                pass

                        doc_user_id = d.get("user_id")
                        if doc_user_id is None and doc.id.startswith("u") and "_" in doc.id:
                            try:
                                doc_user_id = int(doc.id.split("_")[0][1:])
                            except Exception:
                                pass
                        try:
                            final_uid = int(doc_user_id) if doc_user_id is not None else (int(user_id) if user_id is not None else 1)
                        except Exception:
                            final_uid = 1

                        if es_id and d.get("customer_name"):
                            raw_es_date = str(d.get("sale_date") or "").strip()
                            es_sdate = raw_es_date if len(raw_es_date) == 10 else normalize_datetime_str(raw_es_date)
                            existing_es = conn.execute("SELECT customer_name, sale_date FROM farm_egg_sales WHERE id = ?", (es_id,)).fetchone()
                            if existing_es:
                                ex_date_prefix = str(existing_es["sale_date"] or "")[:10]
                                new_date_prefix = str(es_sdate or "")[:10]
                                if existing_es["customer_name"] != d.get("customer_name") or ex_date_prefix != new_date_prefix:
                                    # Aynı içeriğe sahip başka bir kayıt zaten var mı kontrol et
                                    dup_es = conn.execute(
                                        """
                                        SELECT id FROM farm_egg_sales
                                        WHERE user_id = ? AND customer_name = ? AND ABS(box_count - ?) < 0.001
                                          AND ABS(total_amount - ?) < 0.05 AND substr(sale_date, 1, 10) = ? AND source = ?
                                        LIMIT 1
                                        """,
                                        (
                                            final_uid,
                                            d.get("customer_name"),
                                            float(d.get("box_count", 1)),
                                            float(d.get("total_amount", 0)),
                                            new_date_prefix,
                                            d.get("source", "Ciftlik"),
                                        ),
                                    ).fetchone()
                                    if dup_es:
                                        es_id = int(dup_es["id"])
                                    else:
                                        max_es = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM farm_egg_sales").fetchone()[0]
                                        es_id = int(max_es)
                            else:
                                # Bu ID yerelde yoksa bile aynı satış başka bir ID ile zaten kaydedilmiş mi kontrol et
                                new_date_prefix = str(es_sdate or "")[:10]
                                dup_es = conn.execute(
                                    """
                                    SELECT id FROM farm_egg_sales
                                    WHERE user_id = ? AND customer_name = ? AND ABS(box_count - ?) < 0.001
                                      AND ABS(total_amount - ?) < 0.05 AND substr(sale_date, 1, 10) = ?
                                      AND source = ? AND COALESCE(note, '') = ?
                                    LIMIT 1
                                    """,
                                    (
                                        final_uid,
                                        d.get("customer_name"),
                                        float(d.get("box_count", 1)),
                                        float(d.get("total_amount", 0)),
                                        new_date_prefix,
                                        d.get("source", "Ciftlik"),
                                        d.get("note", ""),
                                    ),
                                ).fetchone()
                                if dup_es:
                                    es_id = int(dup_es["id"])

                            conn.execute(
                                """
                                INSERT OR REPLACE INTO farm_egg_sales (
                                    id, user_id, customer_name, box_count, unit_type, piece_count, unit_price, total_amount,
                                    source, sale_date, note, synced_to_cloud, created_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                                """,
                                (
                                    es_id,
                                    final_uid,
                                    d.get("customer_name"),
                                    float(d.get("box_count", 1)),
                                    d.get("unit_type", "koli"),
                                    float(d.get("piece_count", 0)),
                                    float(d.get("unit_price", 0)),
                                    float(d.get("total_amount", 0)),
                                    d.get("source", "Ciftlik"),
                                    es_sdate,
                                    d.get("note", ""),
                                    normalize_datetime_str(d.get("created_at")),
                                ),
                            )
                            pulled_count += 1
                except Exception as e:
                    logger.warning(f"Çiftlik yumurta satışlarını buluttan çekme hatası: {e}")

                # 6.4 PULL FARM FEED PURCHASES
                try:
                    fp_docs = self.firestore_db.collection("farm_feed_purchases").stream()
                    for doc in fp_docs:
                        d = doc.to_dict()
                        fp_id = d.get("id")
                        if fp_id is None:
                            if doc.id.isdigit():
                                fp_id = int(doc.id)
                            elif "_fp" in doc.id:
                                try:
                                    fp_id = int(doc.id.split("_fp")[-1])
                                except Exception:
                                    pass
                            if fp_id is None:
                                nums = re.findall(r"\d+", doc.id)
                                if nums:
                                    fp_id = int(nums[-1])
                            if fp_id is None:
                                max_fp = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM farm_feed_purchases").fetchone()[0]
                                fp_id = int(max_fp)
                        else:
                            try:
                                fp_id = int(fp_id)
                            except Exception:
                                pass

                        doc_user_id = d.get("user_id")
                        if doc_user_id is None and doc.id.startswith("u") and "_" in doc.id:
                            try:
                                doc_user_id = int(doc.id.split("_")[0][1:])
                            except Exception:
                                pass
                        try:
                            final_uid = int(doc_user_id) if doc_user_id is not None else (int(user_id) if user_id is not None else 1)
                        except Exception:
                            final_uid = 1

                        if fp_id and d.get("bag_count"):
                            bag_count = float(d.get("bag_count", 0))
                            bag_weight = float(d.get("bag_weight_kg", 50.0))
                            tot_kg = float(d.get("total_weight_kg", bag_count * bag_weight))
                            tot_ton = float(d.get("total_weight_ton", tot_kg / 1000.0))
                            raw_pdate = str(d.get("purchase_date") or "").strip()
                            p_date = raw_pdate[:10] if len(raw_pdate) >= 10 else now[:10]

                            existing_fp = conn.execute("SELECT supplier, purchase_date FROM farm_feed_purchases WHERE id = ?", (fp_id,)).fetchone()
                            if existing_fp:
                                if existing_fp["supplier"] != d.get("supplier", "") or existing_fp["purchase_date"] != p_date:
                                    max_fp = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM farm_feed_purchases").fetchone()[0]
                                    fp_id = int(max_fp)

                            conn.execute(
                                """
                                INSERT OR REPLACE INTO farm_feed_purchases (
                                    id, user_id, bag_count, bag_weight_kg, total_weight_kg, total_weight_ton,
                                    unit_price, total_amount, purchase_date, supplier, note, synced_to_cloud, created_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                                """,
                                (
                                    fp_id,
                                    final_uid,
                                    bag_count,
                                    bag_weight,
                                    tot_kg,
                                    tot_ton,
                                    float(d.get("unit_price", 0)),
                                    float(d.get("total_amount", 0)),
                                    p_date,
                                    d.get("supplier", ""),
                                    d.get("note", ""),
                                    normalize_datetime_str(d.get("created_at")),
                                ),
                            )
                            pulled_count += 1
                except Exception as e:
                    logger.warning(f"Çiftlik yem alımlarını buluttan çekme hatası: {e}")

                # 7. UPDATE SQLITE AUTOINCREMENT SEQUENCES
                for tbl in ["users", "categories", "products", "sales", "expenses", "farm_customers", "farm_egg_sales", "farm_feed_purchases"]:
                    try:
                        max_row = conn.execute(f"SELECT MAX(id) as max_id FROM {tbl}").fetchone()
                        if max_row and max_row["max_id"] is not None:
                            conn.execute(
                                "INSERT OR REPLACE INTO sqlite_sequence (name, seq) VALUES (?, ?)",
                                (tbl, max_row["max_id"]),
                            )
                    except Exception:
                        pass

                conn.execute("PRAGMA foreign_keys = ON")

            logger.info(f"Firestore'dan {pulled_count} adet kayıt yerel SQLite veritabanına başarıyla senkronize edildi.")
        except Exception as exc:
            logger.error(f"Firestore tam indirme (pull) hatası: {exc}")

        return pulled_count

    # ════════════════════════════════════════════════════════════════════
    # FİREBASE OTOMATİK SENKRONİZASYON (2-WAY OFFLINE-FIRST SYNC)
    # ════════════════════════════════════════════════════════════════════
    def push_offline_data_to_firebase(self, user_id: Optional[int] = None) -> Dict[str, Any]:
        """Arka plan servisi için SADECE gönderilmemiş verileri Firestore'a aktarır (0 READ)."""
        return self.sync_offline_data_with_firebase(user_id=user_id, allow_pull=False)

    def sync_offline_data_with_firebase(self, user_id: Optional[int] = None, allow_pull: bool = False) -> Dict[str, Any]:
        """Yerel SQLite veritabanında henüz buluta gönderilmemiş (synced_to_cloud = 0) kayıtları
        Firebase Firestore'a aktarır (PUSH).
        SQLite bağlantısı ağ çağrıları sırasında kilitli tutulmaz; geçici kopmalarda otomatik yeniden bağlanır.
        allow_pull True ve 60sn cooldown dolmuşsa buluttan güncellemeleri de çeker (PULL).
        """
        if not self.ensure_firebase():
            err_msg = self.firebase_last_error or "Kimlik bilgisi bulunamadı."
            return {
                "synced": False,
                "reason": f"Firebase çevrimdışı ({err_msg}). Veriler yerel SQLite üzerinde saklanmaktadır.",
                "pushed": 0,
                "pulled": 0,
            }

        if time.time() < self._firebase_quota_blocked_until:
            return {
                "synced": False,
                "reason": "Firestore günlük kota sınırı nedeniyle bekleme modunda. Veriler yerel SQLite'ta güvende.",
                "pushed": 0,
                "pulled": 0,
            }

        pushed_count = 0
        failed_count = 0

        try:
            # 0. Önce SQLite'tan gönderilmemiş kayıtları hızlıca oku ve SQLite kilidini hemen bırak
            to_push: List[tuple[str, str, str, int, Dict[str, Any]]] = []
            with self.db.get_connection() as conn:
                unsynced_exists = False
                for tbl in ["users", "categories", "products", "sales", "expenses", "farm_customers", "farm_egg_sales", "farm_feed_purchases"]:
                    try:
                        q = f"SELECT 1 FROM {tbl} WHERE synced_to_cloud = 0"
                        params = []
                        if user_id is not None and tbl != "users":
                            q += " AND (user_id = ? OR user_id IS NULL)"
                            params.append(user_id)
                        q += " LIMIT 1"
                        if conn.execute(q, params).fetchone():
                            unsynced_exists = True
                            break
                    except Exception:
                        pass

                if not unsynced_exists and not allow_pull:
                    return {
                        "synced": True,
                        "reason": "✅ Tüm verileriniz bulut ile eşitlendi ve güncel.",
                        "pushed": 0,
                        "pulled": 0,
                    }

                # 1. USERS
                for u in conn.execute("SELECT * FROM users WHERE synced_to_cloud = 0").fetchall():
                    u_dict = dict(u)
                    u_id = u_dict["id"]
                    u_dict["synced_to_cloud"] = 1
                    to_push.append(("users", "users", str(u_id), u_id, u_dict))

                # 2. CATEGORIES
                cat_query = "SELECT * FROM categories WHERE synced_to_cloud = 0"
                cat_params = [user_id] if user_id is not None else []
                if user_id is not None:
                    cat_query += " AND user_id = ?"
                for c in conn.execute(cat_query, cat_params).fetchall():
                    c_dict = dict(c)
                    c_id = c_dict["id"]
                    c_uid = c_dict.get("user_id") or 1
                    c_dict["synced_to_cloud"] = 1
                    to_push.append(("categories", "categories", f"u{c_uid}_c{c_id}", c_id, c_dict))

                # 3. PRODUCTS
                prod_query = """
                    SELECT p.*, c.name as category_name
                    FROM products p
                    LEFT JOIN categories c ON p.category_id = c.id
                    WHERE p.synced_to_cloud = 0
                """
                prod_params = [user_id] if user_id is not None else []
                if user_id is not None:
                    prod_query += " AND p.user_id = ?"
                for p in conn.execute(prod_query, prod_params).fetchall():
                    p_dict = dict(p)
                    p_id = p_dict["id"]
                    p_uid = p_dict.get("user_id") or 1
                    p_dict["synced_to_cloud"] = 1
                    to_push.append(("products", "products", f"u{p_uid}_p{p_id}", p_id, p_dict))

                # 4. SALES
                sales_query = "SELECT * FROM sales WHERE synced_to_cloud = 0"
                sales_params = [user_id] if user_id is not None else []
                if user_id is not None:
                    sales_query += " AND user_id = ?"
                for s in conn.execute(sales_query, sales_params).fetchall():
                    s_dict = dict(s)
                    s_id = s_dict["id"]
                    s_uid = s_dict.get("user_id") or 1
                    items_rows = conn.execute("SELECT * FROM sale_items WHERE sale_id = ?", (s_id,)).fetchall()
                    s_dict["items"] = [dict(i) for i in items_rows]
                    s_dict["synced_to_cloud"] = 1
                    to_push.append(("sales", "sales", f"u{s_uid}_s{s_id}", s_id, s_dict))

                # 5. EXPENSES
                exp_query = "SELECT * FROM expenses WHERE synced_to_cloud = 0"
                exp_params = [user_id] if user_id is not None else []
                if user_id is not None:
                    exp_query += " AND user_id = ?"
                for e in conn.execute(exp_query, exp_params).fetchall():
                    e_dict = dict(e)
                    e_id = e_dict["id"]
                    e_uid = e_dict.get("user_id") or 1
                    e_dict["synced_to_cloud"] = 1
                    to_push.append(("expenses", "expenses", f"u{e_uid}_e{e_id}", e_id, e_dict))

                # 5.1 FARM CUSTOMERS
                fc_query = "SELECT * FROM farm_customers WHERE synced_to_cloud = 0"
                fc_params = [user_id] if user_id is not None else []
                if user_id is not None:
                    fc_query += " AND user_id = ?"
                for fc in conn.execute(fc_query, fc_params).fetchall():
                    fc_dict = dict(fc)
                    fc_id = fc_dict["id"]
                    fc_uid = fc_dict.get("user_id") or 1
                    fc_dict["synced_to_cloud"] = 1
                    to_push.append(("farm_customers", "farm_customers", f"u{fc_uid}_fc{fc_id}", fc_id, fc_dict))

                # 5.2 FARM EGG SALES
                es_query = "SELECT * FROM farm_egg_sales WHERE synced_to_cloud = 0"
                es_params = [user_id] if user_id is not None else []
                if user_id is not None:
                    es_query += " AND user_id = ?"
                for es in conn.execute(es_query, es_params).fetchall():
                    es_dict = dict(es)
                    es_id = es_dict["id"]
                    es_uid = es_dict.get("user_id") or 1
                    es_dict["synced_to_cloud"] = 1
                    to_push.append(("farm_egg_sales", "farm_egg_sales", f"u{es_uid}_es{es_id}", es_id, es_dict))

                # 5.3 FARM FEED PURCHASES
                fp_query = "SELECT * FROM farm_feed_purchases WHERE synced_to_cloud = 0"
                fp_params = [user_id] if user_id is not None else []
                if user_id is not None:
                    fp_query += " AND user_id = ?"
                for fp in conn.execute(fp_query, fp_params).fetchall():
                    fp_dict = dict(fp)
                    fp_id = fp_dict["id"]
                    fp_uid = fp_dict.get("user_id") or 1
                    fp_dict["synced_to_cloud"] = 1
                    to_push.append(("farm_feed_purchases", "farm_feed_purchases", f"u{fp_uid}_fp{fp_id}", fp_id, fp_dict))

            # Ağ çağrıları SQLite kilidi dışında yapılır, her yazma otomatik retry + reconnect korumalıdır
            synced_rows: List[tuple[str, int]] = []
            for table_name, collection_name, doc_id, row_id, payload in to_push:
                try:
                    def _do_set(db, _col=collection_name, _doc=doc_id, _pay=payload):
                        doc_ref = db.collection(_col).document(_doc)
                        try:
                            return doc_ref.set(_pay, timeout=15.0)
                        except TypeError:
                            return doc_ref.set(_pay)

                    self._firestore_call_with_retry(
                        _do_set,
                        op_name=f"sync_{collection_name}_{doc_id}",
                        max_retries=3,
                    )
                    synced_rows.append((table_name, row_id))
                    pushed_count += 1
                except Exception as ex:
                    failed_count += 1
                    logger.warning(f"{collection_name} ({doc_id}) bulut senkronizasyon hatası: {ex}")
                    if self._is_quota_error(ex):
                        break

            # Başarılı kayıtları tek bir hızlı SQLite işleminde işaretle
            if synced_rows:
                with self.db.get_connection() as conn:
                    for table_name, row_id in synced_rows:
                        conn.execute(f"UPDATE {table_name} SET synced_to_cloud = 1 WHERE id = ?", (row_id,))

            # 6. PUSH GEMINI API KEY IF LOCAL EXISTS AND RECORDS CHANGED
            key_file = DATA_DIR / "gemini_key.txt"
            if unsynced_exists and key_file.exists() and failed_count == 0:
                try:
                    k = key_file.read_text(encoding="utf-8").strip()
                    if k:
                        self._firestore_call_with_retry(
                            lambda db: db.collection("system_settings").document("gemini").set(
                                {"api_key": k, "updated_at": get_turkey_now_str()},
                                merge=True,
                            ),
                            op_name="sync_gemini_key",
                            max_retries=2,
                        )
                except Exception as ex:
                    logger.warning(f"Gemini API key Firestore aktarma uyarısı: {ex}")

            # 7. PULL REMOTE CHANGES (Sadece allow_pull True ise ve 60sn cooldown dolmuşsa)
            pulled_count = 0
            now_ts = time.time()
            if allow_pull and (now_ts - self._last_pull_timestamp >= 60):
                try:
                    pulled_count = self.pull_all_from_firebase(user_id=user_id)
                    self._last_pull_timestamp = now_ts
                except Exception as ex:
                    logger.warning(f"Firestore senkronizasyon çekme uyarısı: {ex}")

            if pushed_count == 0 and pulled_count == 0:
                reason = "✅ Tüm verileriniz bulut ile eşitlendi ve güncel."
            elif pushed_count > 0 and pulled_count > 0:
                reason = f"☁️ Firebase eşitlendi: {pushed_count} veri buluta aktarıldı, {pulled_count} veri güncellendi."
            elif pushed_count > 0:
                reason = f"☁️ Firebase eşitlendi: {pushed_count} yeni veri buluta aktarıldı."
            else:
                reason = f"☁️ Firebase eşitlendi: {pulled_count} veri buluttan indirildi."

            return {
                "synced": failed_count == 0,
                "reason": reason if failed_count == 0 else f"⚠️ {pushed_count} kayıt aktarıldı, {failed_count} kayıt yeniden denenecek.",
                "pushed": pushed_count,
                "pulled": pulled_count,
                "failed": failed_count,
            }
        except Exception as exc:
            logger.error(f"Senkronizasyon hatası: {exc}")
            return {"synced": False, "reason": str(exc), "pushed": pushed_count, "pulled": 0}

    def _resolve_user_id(self, user_id: Optional[Union[int, str]] = None) -> int:
        if user_id is not None:
            try:
                return int(user_id)
            except (ValueError, TypeError):
                pass
        try:
            with self.db.get_connection() as conn:
                # 1. En çok aktif ürünü olan ana işletmeyi bul (örn: GALLUS2)
                row = conn.execute("""
                    SELECT u.id 
                    FROM users u
                    JOIN products p ON p.user_id = u.id AND p.is_active = 1
                    GROUP BY u.id
                    ORDER BY COUNT(p.id) DESC
                    LIMIT 1
                """).fetchone()
                if row and row["id"] is not None:
                    return int(row["id"])
                # 2. Ürün bulunamazsa ilk kullanıcıyı al
                row = conn.execute("SELECT id FROM users ORDER BY id ASC LIMIT 1").fetchone()
                if row and row["id"] is not None:
                    return int(row["id"])
        except Exception:
            pass
        return 1

    def force_full_sync_with_firebase(self, user_id: Optional[int] = None) -> Dict[str, Any]:
        """Kullanıcının TÜM ürünlerini, kategorilerini, satışlarını ve kullanıcı kaydını
        synced_to_cloud değerine bakılmaksızın Firestore'a tam olarak eşitler."""
        if not self.firestore_db:
            self._init_firebase_optional()

        if not self.firestore_db:
            err_msg = self.firebase_last_error or "Kimlik bilgisi bulunamadı."
            return {
                "synced": False,
                "reason": f"Firebase bağlantısı kurulamadı ({err_msg}). Lütfen Firebase ayarlarını kontrol ediniz.",
                "pushed": 0,
                "pulled": 0,
            }

        uid = self._resolve_user_id(user_id) or 1
        pushed_count = 0
        now = get_turkey_now_str()

        try:
            with self.db.get_connection() as conn:
                # 1. Kullanıcıyı eşitle
                u_row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
                if u_row:
                    u_dict = dict(u_row)
                    u_dict["synced_to_cloud"] = 1
                    try:
                        self.firestore_db.collection("users").document(str(uid)).set(u_dict)
                        conn.execute("UPDATE users SET synced_to_cloud = 1 WHERE id = ?", (uid,))
                        pushed_count += 1
                    except Exception as e:
                        logger.warning(f"Kullanıcı senkronizasyon hatası: {e}")

                # 2. Tüm kategorileri eşitle
                cats = conn.execute("SELECT * FROM categories WHERE user_id = ?", (uid,)).fetchall()
                for c in cats:
                    c_dict = dict(c)
                    c_id = c_dict["id"]
                    c_dict["synced_to_cloud"] = 1
                    doc_id = f"u{uid}_c{c_id}"
                    try:
                        self.firestore_db.collection("categories").document(doc_id).set(c_dict)
                        conn.execute("UPDATE categories SET synced_to_cloud = 1 WHERE id = ?", (c_id,))
                        pushed_count += 1
                    except Exception as e:
                        logger.warning(f"Kategori senkronizasyon hatası: {e}")

                # 3. Tüm ürünleri (Aktif olanlar) eşitle
                prods = conn.execute("""
                    SELECT p.*, c.name as category_name
                    FROM products p
                    LEFT JOIN categories c ON p.category_id = c.id
                    WHERE p.user_id = ? AND p.is_active = 1
                """, (uid,)).fetchall()
                for p in prods:
                    p_dict = dict(p)
                    p_id = p_dict["id"]
                    p_dict["synced_to_cloud"] = 1
                    p_dict["stock_quantity"] = float(p_dict.get("stock_quantity", 0))
                    p_dict["critical_stock_level"] = float(p_dict.get("critical_stock_level", 5))
                    p_dict["unit"] = str(p_dict.get("unit", "adet"))
                    doc_id = f"u{uid}_p{p_id}"
                    try:
                        self.firestore_db.collection("products").document(doc_id).set(p_dict)
                        conn.execute("UPDATE products SET synced_to_cloud = 1 WHERE id = ?", (p_id,))
                        pushed_count += 1
                    except Exception as e:
                        logger.warning(f"Ürün senkronizasyon hatası: {e}")

                # 4. Tüm satışları ve kalemleri eşitle
                sales = conn.execute("SELECT * FROM sales WHERE user_id = ? OR user_id IS NULL", (uid,)).fetchall()
                for s in sales:
                    s_dict = dict(s)
                    s_id = s_dict["id"]
                    s_uid = s_dict.get("user_id") or uid
                    items_rows = conn.execute("SELECT * FROM sale_items WHERE sale_id = ?", (s_id,)).fetchall()
                    s_dict["items"] = [dict(i) for i in items_rows]
                    s_dict["synced_to_cloud"] = 1
                    doc_id = f"u{s_uid}_s{s_id}"
                    try:
                        self.firestore_db.collection("sales").document(doc_id).set(s_dict)
                        conn.execute("UPDATE sales SET synced_to_cloud = 1 WHERE id = ?", (s_id,))
                        pushed_count += 1
                    except Exception as e:
                        logger.warning(f"Satış {s_id} tam senkronizasyon hatası: {e}")

                # 5. Tüm çiftlik yumurta satışlarını eşitle
                egg_sales = conn.execute("SELECT * FROM farm_egg_sales WHERE user_id = ? OR user_id IS NULL", (uid,)).fetchall()
                for es in egg_sales:
                    es_dict = dict(es)
                    es_id = es_dict["id"]
                    es_uid = es_dict.get("user_id") or uid
                    es_dict["synced_to_cloud"] = 1
                    doc_id = f"u{es_uid}_es{es_id}"
                    try:
                        self.firestore_db.collection("farm_egg_sales").document(doc_id).set(es_dict)
                        conn.execute("UPDATE farm_egg_sales SET synced_to_cloud = 1 WHERE id = ?", (es_id,))
                        pushed_count += 1
                    except Exception as ex:
                        logger.warning(f"Çiftlik yumurta satış {es_id} tam senkronizasyon hatası: {ex}")

                # 6. Tüm giderleri eşitle
                exp_rows = conn.execute("SELECT * FROM expenses WHERE user_id = ? OR user_id IS NULL", (uid,)).fetchall()
                for exp in exp_rows:
                    e_dict = dict(exp)
                    e_id = e_dict["id"]
                    e_uid = e_dict.get("user_id") or uid
                    e_dict["synced_to_cloud"] = 1
                    doc_id = f"u{e_uid}_e{e_id}"
                    try:
                        self.firestore_db.collection("expenses").document(doc_id).set(e_dict)
                        conn.execute("UPDATE expenses SET synced_to_cloud = 1 WHERE id = ?", (e_id,))
                        pushed_count += 1
                    except Exception as ex:
                        logger.warning(f"Gider {e_id} tam senkronizasyon hatası: {ex}")

                # 7. Tüm çiftlik müşterilerini eşitle
                fc_rows = conn.execute("SELECT * FROM farm_customers WHERE user_id = ? OR user_id IS NULL", (uid,)).fetchall()
                for fc in fc_rows:
                    fc_dict = dict(fc)
                    fc_id = fc_dict["id"]
                    fc_uid = fc_dict.get("user_id") or uid
                    fc_dict["synced_to_cloud"] = 1
                    doc_id = f"u{fc_uid}_fc{fc_id}"
                    try:
                        self.firestore_db.collection("farm_customers").document(doc_id).set(fc_dict)
                        conn.execute("UPDATE farm_customers SET synced_to_cloud = 1 WHERE id = ?", (fc_id,))
                        pushed_count += 1
                    except Exception as ex:
                        logger.warning(f"Çiftlik müşteri {fc_id} tam senkronizasyon hatası: {ex}")

                # 8. Tüm yem alımlarını eşitle
                fp_rows = conn.execute("SELECT * FROM farm_feed_purchases WHERE user_id = ? OR user_id IS NULL", (uid,)).fetchall()
                for fp in fp_rows:
                    fp_dict = dict(fp)
                    fp_id = fp_dict["id"]
                    fp_uid = fp_dict.get("user_id") or uid
                    fp_dict["synced_to_cloud"] = 1
                    doc_id = f"u{fp_uid}_fp{fp_id}"
                    try:
                        self.firestore_db.collection("farm_feed_purchases").document(doc_id).set(fp_dict)
                        conn.execute("UPDATE farm_feed_purchases SET synced_to_cloud = 1 WHERE id = ?", (fp_id,))
                        pushed_count += 1
                    except Exception as ex:
                        logger.warning(f"Yem alım {fp_id} tam senkronizasyon hatası: {ex}")

            # 9. Çift yönlü çekme
            pulled_count = self.pull_all_from_firebase(user_id=uid)

            if pushed_count == 0 and pulled_count == 0:
                reason = "✅ Tüm verileriniz bulut ile eşitlendi ve güncel."
            elif pushed_count > 0 and pulled_count > 0:
                reason = f"☁️ Firebase eşitlendi: {pushed_count} veri buluta aktarıldı, {pulled_count} veri güncellendi."
            elif pushed_count > 0:
                reason = f"☁️ Firebase eşitlendi: {pushed_count} yeni veri buluta aktarıldı."
            else:
                reason = f"☁️ Firebase eşitlendi: {pulled_count} veri buluttan indirildi."

            return {
                "synced": True,
                "reason": reason,
                "pushed": pushed_count,
                "pulled": pulled_count,
            }
        except Exception as e:
            logger.error(f"Full sync hatası: {e}")
            return {"synced": False, "reason": str(e), "pushed": pushed_count, "pulled": 0}

    # ════════════════════════════════════════════════════════════════════
    # KATEGORİ İŞLEMLERİ (KULLANICIYA ÖZEL İZOLE)
    # ════════════════════════════════════════════════════════════════════
    def get_categories(self, user_id: Optional[Union[int, str]] = None) -> List[Dict[str, Any]]:
        uid = self._resolve_user_id(user_id)
        cats = []
        try:
            with self.db.get_connection() as conn:
                rows = conn.execute(
                    "SELECT id, name FROM categories WHERE (user_id = ? OR user_id IS NULL) ORDER BY name ASC",
                    (uid,),
                ).fetchall()
                cats = [{"id": r["id"], "name": r["name"]} for r in rows]
        except Exception:
            pass

        if not cats and not self._hydration_completed and self.ensure_firebase():
            try:
                self.ensure_initial_hydration()
                with self.db.get_connection() as conn:
                    rows = conn.execute(
                        "SELECT id, name FROM categories WHERE (user_id = ? OR user_id IS NULL) ORDER BY name ASC",
                        (uid,),
                    ).fetchall()
                    cats = [{"id": r["id"], "name": r["name"]} for r in rows]
            except Exception:
                pass

        if not cats:
            try:
                with self.db.get_connection() as conn:
                    rows = conn.execute("SELECT id, name FROM categories ORDER BY name ASC").fetchall()
                    cats = [{"id": r["id"], "name": r["name"]} for r in rows]
            except Exception:
                pass
        return cats

    def add_category(self, name: str, user_id: Optional[int] = None) -> Optional[int]:
        name_clean = name.strip()
        if not name_clean:
            return None
        raw_uid = user_id or 1
        with self.db.get_connection() as conn:
            u_check = conn.execute("SELECT id FROM users WHERE id = ?", (raw_uid,)).fetchone()
            uid = raw_uid if u_check else None

            if uid is not None:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO categories (name, user_id, synced_to_cloud) VALUES (?, ?, 0)",
                    (name_clean, uid),
                )
                cat_id = cursor.lastrowid
                if not cat_id:
                    row = conn.execute(
                        "SELECT id FROM categories WHERE name = ? AND user_id = ?",
                        (name_clean, uid),
                    ).fetchone()
                    cat_id = row["id"] if row else None
            else:
                row = conn.execute(
                    "SELECT id FROM categories WHERE name = ? AND (user_id IS NULL OR user_id = ?)",
                    (name_clean, raw_uid),
                ).fetchone()
                if row:
                    cat_id = row["id"]
                else:
                    cursor = conn.execute(
                        "INSERT OR IGNORE INTO categories (name, user_id, synced_to_cloud) VALUES (?, NULL, 0)",
                        (name_clean,),
                    )
                    cat_id = cursor.lastrowid or 1

        if cat_id and self.firestore_db:
            def _sync_cat():
                try:
                    doc_id = f"u{raw_uid}_c{cat_id}"
                    self.firestore_db.collection("categories").document(doc_id).set({
                        "id": cat_id,
                        "name": name_clean,
                        "user_id": raw_uid,
                        "synced_to_cloud": 1,
                    })
                    with self.db.get_connection() as c:
                        c.execute("UPDATE categories SET synced_to_cloud = 1 WHERE id = ?", (cat_id,))
                except Exception:
                    pass
            threading.Thread(target=_sync_cat, daemon=True).start()
        return cat_id

    # ════════════════════════════════════════════════════════════════════
    # ÜRÜN İŞLEMLERİ (KULLANICIYA ÖZEL İZOLE & STOK KORUMALI)
    # ════════════════════════════════════════════════════════════════════
    def get_product_by_id(self, product_id: int, user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Tek bir ürünü ID üzerinden 0ms gecikmeyle getirir."""
        uid = user_id or 1
        query = """
            SELECT p.*, c.name as category_name
            FROM products p
            LEFT JOIN categories c ON p.category_id = c.id
            WHERE p.id = ? AND p.is_active = 1 AND (p.user_id = ? OR p.user_id IS NULL)
        """
        with self.db.get_connection() as conn:
            row = conn.execute(query, (product_id, uid)).fetchone()
            return dict(row) if row else None
    def get_products(self, user_id: Optional[Union[int, str]] = None, search: str = "") -> List[Dict[str, Any]]:
        uid = self._resolve_user_id(user_id)
        query = """
            SELECT p.*, c.name as category_name
            FROM products p
            LEFT JOIN categories c ON p.category_id = c.id
            WHERE p.is_active = 1 AND (p.user_id = ? OR p.user_id IS NULL)
        """
        params: list = [uid]

        if search and search.strip():
            s = f"%{search.strip()}%"
            query += " AND (p.name LIKE ? OR p.barcode LIKE ? OR c.name LIKE ?)"
            params.extend([s, s, s])

        query += " ORDER BY p.name ASC"

        # 1. Hızlı gerçek ürün sayısı kontrolü (bağlantı anında kapanır)
        real_count = 1
        try:
            with self.db.get_connection() as conn:
                real_count = conn.execute("""
                    SELECT COUNT(*) FROM products 
                    WHERE barcode NOT IN ('869000000001', '869000000002', '869000000003', '869000000004', '869000000005', '869000000006')
                """).fetchone()[0]
        except Exception:
            pass

        # 2. Eğer gerçek ürün yoksa ve Firebase aktifse, hydration'ı açık bağlantı olmadan güvenle çağır
        if real_count == 0 and not search and self.ensure_firebase():
            now_ts = time.time()
            if now_ts - self._last_pull_timestamp > 10:
                try:
                    self.ensure_initial_hydration()
                except Exception as e:
                    logger.warning(f"get_products hydration uyarısı: {e}")

        # 3. Ürünleri oku
        try:
            with self.db.get_connection() as conn:
                rows = conn.execute(query, params).fetchall()
                products = [dict(r) for r in rows]

                if not products:
                    fb_query = query.replace("(p.user_id = ? OR p.user_id IS NULL)", "1=1")
                    fb_params = params[1:]
                    fb_rows = conn.execute(fb_query, fb_params).fetchall()
                    if fb_rows:
                        products = [dict(r) for r in fb_rows]

                return products
        except Exception as e:
            logger.error(f"get_products sorgu hatası: {e}")
            return []

    def get_product_by_barcode(self, barcode: str, user_id: Optional[Union[int, str]] = None) -> Optional[Dict[str, Any]]:
        uid = self._resolve_user_id(user_id)
        query = """
            SELECT p.*, c.name as category_name
            FROM products p
            LEFT JOIN categories c ON p.category_id = c.id
            WHERE p.barcode = ? AND p.is_active = 1 AND (p.user_id = ? OR p.user_id IS NULL)
        """
        params: list = [barcode.strip(), uid]

        with self.db.get_connection() as conn:
            row = conn.execute(query, params).fetchone()
            if not row:
                fb_query = """
                    SELECT p.*, c.name as category_name
                    FROM products p
                    LEFT JOIN categories c ON p.category_id = c.id
                    WHERE p.barcode = ? AND p.is_active = 1
                """
                row = conn.execute(fb_query, (barcode.strip(),)).fetchone()
            return dict(row) if row else None

    def save_product(
        self,
        name: str,
        barcode: str,
        category_name: str,
        purchase_price: float,
        sale_price: float,
        stock_quantity: float,
        critical_stock_level: float = 5,
        image_path: Optional[str] = None,
        product_id: Optional[int] = None,
        user_id: Optional[int] = None,
        unit: str = "adet",
    ) -> tuple[bool, str, Optional[Dict[str, Any]]]:

        raw_uid = user_id or 1
        cat_id = self.add_category(category_name, user_id=raw_uid) or 1
        now = get_turkey_now_str()
        unit_clean = (unit or "adet").strip().lower()
        if unit_clean not in ("adet", "kg"):
            unit_clean = "adet"

        with self.db.get_connection() as conn:
            u_check = conn.execute("SELECT id FROM users WHERE id = ?", (raw_uid,)).fetchone()
            uid = raw_uid if u_check else None

            if product_id:
                # Update existing product for this user (or legacy null user)
                existing = conn.execute(
                    "SELECT id, image_path, barcode FROM products WHERE id = ? AND (user_id = ? OR user_id IS NULL)",
                    (product_id, raw_uid)
                ).fetchone()

                if not existing:
                    return False, "Düzenlenecek ürün bulunamadı!", None

                # Check duplicate barcode for ANOTHER active product
                dup = conn.execute(
                    "SELECT id FROM products WHERE barcode = ? AND (user_id = ? OR user_id IS NULL) AND id != ? AND is_active = 1",
                    (barcode, raw_uid, product_id)
                ).fetchone()
                if dup:
                    return False, f"'{barcode}' barkodlu başka bir ürününüz zaten mevcut!", None

                # Fotoğraf koruma mantığı:
                if image_path == "__REMOVE__":
                    final_image_path = ""
                elif image_path is not None:
                    final_image_path = _save_base64_to_disk(image_path, raw_uid, product_id)
                else:
                    final_image_path = existing["image_path"] or ""

                conn.execute(
                    """
                    UPDATE products
                    SET category_id = ?, name = ?, barcode = ?, purchase_price = ?,
                        sale_price = ?, stock_quantity = ?, critical_stock_level = ?,
                        unit = ?, image_path = ?, updated_at = ?, is_active = 1
                    WHERE id = ? AND (user_id = ? OR user_id IS NULL)
                    """,
                    (
                        cat_id,
                        name,
                        barcode,
                        purchase_price,
                        sale_price,
                        float(stock_quantity),
                        float(critical_stock_level),
                        unit_clean,
                        final_image_path,
                        now,
                        product_id,
                        raw_uid,
                    ),
                )
                pid = product_id
                msg = f"'{name}' ürünü başarıyla güncellendi."
            else:
                final_image_path = image_path or ""
                # Check duplicate barcode for this user
                existing = conn.execute(
                    "SELECT id FROM products WHERE barcode = ? AND (user_id = ? OR user_id IS NULL) AND is_active = 1",
                    (barcode, raw_uid)
                ).fetchone()

                if existing:
                    return False, f"'{barcode}' barkodlu bir ürününüz zaten mevcut!", None

                cursor = conn.execute(
                    """
                    INSERT INTO products (
                        user_id, category_id, name, barcode, purchase_price, sale_price,
                        stock_quantity, critical_stock_level, unit, image_path, is_active, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        uid,
                        cat_id,
                        name,
                        barcode,
                        purchase_price,
                        sale_price,
                        float(stock_quantity),
                        float(critical_stock_level),
                        unit_clean,
                        "",
                        now,
                        now,
                    ),
                )
                pid = cursor.lastrowid
                if final_image_path:
                    final_image_path = _save_base64_to_disk(final_image_path, raw_uid, pid)
                    conn.execute("UPDATE products SET image_path = ? WHERE id = ?", (final_image_path, pid))
                msg = f"'{name}' ürünü başarıyla eklendi."

        prod_data = {
            "id": pid,
            "user_id": uid,
            "category_id": cat_id,
            "category_name": category_name,
            "name": name,
            "barcode": barcode,
            "purchase_price": purchase_price,
            "sale_price": sale_price,
            "stock_quantity": float(stock_quantity),
            "critical_stock_level": float(critical_stock_level),
            "unit": unit_clean,
            "image_path": final_image_path,
            "is_active": 1,
            "created_at": now,
            "updated_at": now,
        }
        if self.firestore_db:
            def _async_save():
                try:
                    doc_id = f"u{uid}_p{pid}"
                    to_sync = dict(prod_data)
                    to_sync["synced_to_cloud"] = 1
                    self.firestore_db.collection("products").document(doc_id).set(to_sync)
                    with self.db.get_connection() as c:
                        c.execute("UPDATE products SET synced_to_cloud = 1 WHERE id = ?", (pid,))
                except Exception as ex:
                    logger.warning(f"Firestore ürün arka plan kaydı uyarısı: {ex}")

            threading.Thread(target=_async_save, daemon=True).start()

        prod_data["synced_to_cloud"] = 1 if self.firestore_db else 0
        return True, msg, prod_data

    def update_product_image(
        self, product_id: int, image_path: str, user_id: Optional[int] = None
    ) -> tuple[bool, str, Optional[str]]:
        """Bir ürünün fotoğrafını anında günceller veya temizler ve Firestore'a senkronize eder."""
        raw_uid = user_id or 1
        now = get_turkey_now_str()
        clean_img = "" if image_path == "__REMOVE__" else (image_path or "")
        if clean_img:
            clean_img = _save_base64_to_disk(clean_img, raw_uid, product_id)

        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT id, name FROM products WHERE id = ? AND (user_id = ? OR user_id IS NULL) AND is_active = 1",
                (product_id, raw_uid)
            ).fetchone()
            if not row:
                return False, "Fotoğrafı güncellenecek ürün bulunamadı!", None

            conn.execute(
                "UPDATE products SET image_path = ?, synced_to_cloud = 0, updated_at = ? WHERE id = ? AND (user_id = ? OR user_id IS NULL)",
                (clean_img, now, product_id, raw_uid)
            )

        if self.firestore_db:
            def _async_img():
                try:
                    doc_id = f"u{raw_uid}_p{product_id}"
                    self.firestore_db.collection("products").document(doc_id).update({
                        "image_path": clean_img,
                        "updated_at": now,
                        "synced_to_cloud": 1
                    })
                    with self.db.get_connection() as c:
                        c.execute("UPDATE products SET synced_to_cloud = 1 WHERE id = ?", (product_id,))
                except Exception as e:
                    logger.warning(f"Firestore ürün görsel güncelleme uyarısı: {e}")

            threading.Thread(target=_async_img, daemon=True).start()

        msg = "Ürün görseli başarıyla kaldırıldı." if not clean_img else f"'{row['name']}' ürün fotoğrafı başarıyla güncellendi."
        return True, msg, clean_img

        msg = "Ürün görseli başarıyla kaldırıldı." if not clean_img else f"'{row['name']}' ürün fotoğrafı başarıyla güncellendi."
        return True, msg, clean_img

    def update_product_price(
        self, product_id: int, sale_price: float, purchase_price: Optional[float] = None, user_id: Optional[int] = None
    ) -> tuple[bool, str]:
        """Bir ürünün satış veya alış fiyatını anında günceller ve Firestore'a senkronize eder."""
        if sale_price < 0:
            return False, "Satış fiyatı negatif olamaz!"

        uid = user_id or 1
        now = get_turkey_now_str()

        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT id, name, purchase_price FROM products WHERE id = ? AND user_id = ? AND is_active = 1",
                (product_id, uid)
            ).fetchone()
            if not row:
                return False, "Fiyatı güncellenecek ürün bulunamadı!"

            p_price = purchase_price if purchase_price is not None else float(row["purchase_price"])
            conn.execute(
                "UPDATE products SET sale_price = ?, purchase_price = ?, synced_to_cloud = 0, updated_at = ? WHERE id = ? AND user_id = ?",
                (sale_price, p_price, now, product_id, uid)
            )

        if self.firestore_db:
            def _async_price():
                try:
                    doc_id = f"u{uid}_p{product_id}"
                    self.firestore_db.collection("products").document(doc_id).update({
                        "sale_price": sale_price,
                        "purchase_price": p_price,
                        "updated_at": now,
                        "synced_to_cloud": 1
                    })
                    with self.db.get_connection() as c:
                        c.execute("UPDATE products SET synced_to_cloud = 1 WHERE id = ?", (product_id,))
                except Exception as e:
                    logger.warning(f"Firestore fiyat güncelleme uyarısı: {e}")
            threading.Thread(target=_async_price, daemon=True).start()

        return True, f"'{row['name']}' ürününün yeni satış fiyatı {sale_price:.2f} ₺ olarak güncellendi."

    def delete_product(self, product_id: int, user_id: Optional[int] = None) -> tuple[bool, str]:
        uid = user_id or 1
        now = get_turkey_now_str()
        with self.db.get_connection() as conn:
            conn.execute(
                "UPDATE products SET is_active = 0, synced_to_cloud = 0, updated_at = ? WHERE id = ? AND user_id = ?",
                (now, product_id, uid)
            )

        if self.firestore_db:
            def _async_del_prod():
                try:
                    doc_id = f"u{uid}_p{product_id}"
                    self.firestore_db.collection("products").document(doc_id).update({
                        "is_active": 0,
                        "updated_at": now,
                        "synced_to_cloud": 1
                    })
                    with self.db.get_connection() as c:
                        c.execute("UPDATE products SET synced_to_cloud = 1 WHERE id = ?", (product_id,))
                except Exception as e:
                    logger.warning(f"Firestore ürün silme uyarısı: {e}")
            threading.Thread(target=_async_del_prod, daemon=True).start()

        return True, "Ürün stok kataloğundan silindi."

    def update_product_stock(self, product_id: int, stock_quantity: float, user_id: Optional[int] = None) -> tuple[bool, str]:
        if stock_quantity < 0:
            return False, "Stok miktarı negatif olamaz!"

        uid = user_id or 1
        now = get_turkey_now_str()
        with self.db.get_connection() as conn:
            conn.execute(
                "UPDATE products SET stock_quantity = ?, synced_to_cloud = 0, updated_at = ? WHERE id = ? AND user_id = ? AND is_active = 1",
                (float(stock_quantity), now, product_id, uid)
            )
            p_row = conn.execute("SELECT unit FROM products WHERE id = ?", (product_id,)).fetchone()
            unit_str = p_row["unit"] if p_row and p_row["unit"] else "adet"

        if self.firestore_db:
            def _async_stock():
                try:
                    doc_id = f"u{uid}_p{product_id}"
                    self.firestore_db.collection("products").document(doc_id).update({
                        "stock_quantity": float(stock_quantity),
                        "updated_at": now,
                        "synced_to_cloud": 1
                    })
                    with self.db.get_connection() as c:
                        c.execute("UPDATE products SET synced_to_cloud = 1 WHERE id = ?", (product_id,))
                except Exception as e:
                    logger.warning(f"Firestore stok güncelleme uyarısı: {e}")
            threading.Thread(target=_async_stock, daemon=True).start()

        return True, f"Stok miktarı {float(stock_quantity):g} {unit_str} olarak güncellendi."

    # ════════════════════════════════════════════════════════════════════
    # SATIŞ İŞLEMLERİ (KULLANICIYA ÖZEL İZOLE)
    # ════════════════════════════════════════════════════════════════════
    def add_sale(
        self,
        cart_items: List[Dict[str, Any]],
        note: str = "Mobil/PC Satış",
        user_id: Optional[int] = None,
        channel: str = "magaza",
        total_amount_override: Optional[float] = None,
        customer_name: Optional[str] = None,
        sold_at: Optional[str] = None,
        actor: Optional[Dict[str, Any]] = None,
        payment_method: Optional[str] = None,
        customer_id: Optional[int] = None,
        paid_cash_amount: Optional[float] = None,
        paid_pos_amount: Optional[float] = None,
        overpayment_mode: Optional[str] = None,
    ) -> tuple[bool, str]:
        if not cart_items:
            return False, "Sepet boş!"

        # Render cold-start sonrası yerel SQLite boşsa satış ID çakışmasını önlemek için önce buluttan verileri çek
        if not self._hydration_completed and self.ensure_firebase():
            try:
                self.ensure_initial_hydration()
            except Exception as e_hyd:
                logger.warning(f"Satış öncesi initial hydration uyarısı: {e_hyd}")

        uid = self._resolve_user_id(user_id)
        ch = (channel or "magaza").strip().lower()
        if ch not in ("magaza", "internet"):
            ch = "magaza"

        pm_raw = str(payment_method or ("kart" if ch == "internet" else "nakit")).strip().lower()
        if pm_raw == "pos":
            pm_raw = "kart"
        pm = pm_raw if pm_raw in ("nakit", "kart", "veresiye", "karma", "havale") else "nakit"

        cust_name = customer_name.strip() if customer_name and str(customer_name).strip() else None

        for item in cart_items:
            u_price = float(item.get("unit_price", 0))
            qty = float(item.get("quantity", 1))
            if u_price < 0 or qty <= 0:
                return False, "Birim fiyat negatif, miktar 0 veya negatif olamaz!"

        calculated_total = sum(float(item.get("subtotal", item.get("unit_price", 0) * item.get("quantity", 1))) for item in cart_items)
        if total_amount_override is not None:
            try:
                total_amount = round(float(total_amount_override), 2)
            except (ValueError, TypeError):
                total_amount = round(calculated_total, 2)
        else:
            total_amount = round(calculated_total, 2)

        if total_amount < 0:
            return False, "Satış toplam tutarı negatif olamaz!"

        # Kuruş bazlı kesin ödeme ve cari hesap ayrıştırması
        total_k = to_kurus(total_amount)
        if paid_cash_amount is not None and float(paid_cash_amount) < 0:
            return False, "Ödenen nakit tutarı negatif olamaz!"
        if paid_pos_amount is not None and float(paid_pos_amount) < 0:
            return False, "Ödenen POS tutarı negatif olamaz!"

        if pm == "veresiye":
            paid_cash_k = 0
            paid_pos_k = 0
        elif pm == "karma":
            paid_cash_k = to_kurus(paid_cash_amount or 0)
            paid_pos_k = to_kurus(paid_pos_amount or 0)
        elif pm == "nakit":
            paid_cash_k = to_kurus(paid_cash_amount) if paid_cash_amount is not None else total_k
            paid_pos_k = to_kurus(paid_pos_amount or 0)
        else:
            paid_cash_k = to_kurus(paid_cash_amount or 0)
            paid_pos_k = to_kurus(paid_pos_amount) if paid_pos_amount is not None else total_k

        paid_total_k = paid_cash_k + paid_pos_k
        diff_k = total_k - paid_total_k  # >0: veresiye borç, <0: fazla ödeme
        ov_mode: Optional[str] = None
        overpay_k = 0
        if diff_k < 0:
            overpay_k = abs(diff_k)
            ov_mode = "change" if str(overpayment_mode or "credit").strip().lower() == "change" else "credit"

        needs_credit_customer = (pm == "veresiye") or (diff_k > 0) or (overpay_k > 0 and ov_mode == "credit")
        if needs_credit_customer and not customer_id and not cust_name:
            return False, "Veresiye, kısmi ödeme veya cari alacak kaydı için müşteri seçilmesi zorunludur!"

        act = actor or {"name": "Patron", "role": "patron", "user_id": uid}
        created_by = str(act.get("name") or "Patron")
        created_by_role = normalize_role(act.get("role"))

        item_count = len(cart_items)
        now = normalize_datetime_str(sold_at)
        product_summary = ", ".join(
            f"{str(it.get('product_name', 'Ürün')).strip()} x{float(it.get('quantity', 1)):g}"
            for it in cart_items[:5]
        )

        # Cari müşteri çözümle (gerekiyorsa veya seçildiyse)
        resolved_cust_id: Optional[int] = None
        if customer_id:
            with self.db.get_connection() as conn:
                c_row = conn.execute(
                    "SELECT id, name FROM credit_customers WHERE id = ? AND (user_id = ? OR user_id IS NULL)",
                    (int(customer_id), uid),
                ).fetchone()
                if c_row:
                    resolved_cust_id = int(c_row["id"])
                    if not cust_name:
                        cust_name = c_row["name"]
                elif needs_credit_customer:
                    return False, "Seçilen cari müşteri bulunamadı!"
        elif needs_credit_customer and cust_name:
            ok_c, _, cust_rec = self.get_or_create_credit_customer(cust_name, user_id=uid, actor=act)
            if ok_c and cust_rec:
                resolved_cust_id = int(cust_rec["id"])
                cust_name = cust_rec["name"]

        credit_net_k = diff_k if (overpay_k == 0 or ov_mode == "credit") else 0

        with self.db.get_connection() as conn:
            # Çevrimdışı kuyruk tekrar gönderimlerinde mükerrer satış kaydını engelle (idempotency)
            if sold_at:
                dup_row = conn.execute(
                    """
                    SELECT id FROM sales
                    WHERE (user_id = ? OR user_id IS NULL)
                      AND sold_at = ?
                      AND ABS(total_amount - ?) < 0.01
                      AND item_count = ?
                      AND channel = ?
                    LIMIT 1
                    """,
                    (uid, now, total_amount, item_count, ch),
                ).fetchone()
                if dup_row:
                    return True, f"Satış başarıyla tamamlandı. Toplam: {total_amount:.2f} ₺"

            cursor = conn.execute(
                """
                INSERT INTO sales (
                    user_id, total_amount, item_count, sold_at, note, channel, payment_method,
                    customer_id, customer_name, paid_cash_amount, paid_pos_amount, credit_amount, overpayment_mode,
                    status, created_by, created_by_role, synced_to_cloud
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, 0)
                """,
                (
                    uid, total_amount, item_count, now, note, ch, pm,
                    resolved_cust_id, cust_name,
                    from_kurus(paid_cash_k), from_kurus(paid_pos_k), from_kurus(credit_net_k), ov_mode,
                    created_by, created_by_role,
                ),
            )
            sale_id = cursor.lastrowid

            for item in cart_items:
                raw_pid = item.get("product_id")
                if raw_pid is not None:
                    p_exists = conn.execute("SELECT id FROM products WHERE id = ?", (raw_pid,)).fetchone()
                    if not p_exists:
                        p_name = item.get("product_name", "Ürün")
                        p_barcode = item.get("barcode", f"AUTO{raw_pid}")
                        p_price = float(item.get("unit_price", 0))
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO products (
                                id, user_id, category_id, name, barcode, purchase_price, sale_price,
                                stock_quantity, critical_stock_level, unit, is_active, synced_to_cloud, created_at, updated_at
                            ) VALUES (?, ?, 1, ?, ?, 0.0, ?, 0.0, 5.0, ?, 1, 0, ?, ?)
                            """,
                            (raw_pid, uid, p_name, p_barcode, p_price, str(item.get("unit", "adet")), now, now),
                        )
                valid_pid = raw_pid

                conn.execute(
                    """
                    INSERT INTO sale_items (sale_id, product_id, product_name, barcode, unit_price, quantity, subtotal, unit)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sale_id,
                        valid_pid,
                        item["product_name"],
                        item["barcode"],
                        float(item["unit_price"]),
                        float(item["quantity"]),
                        float(item["subtotal"]),
                        str(item.get("unit", "adet")),
                    ),
                )
                # Deduct stock ONLY for active products of this user (mark synced_to_cloud = 0 until cloud sync)
                if valid_pid is not None:
                    conn.execute(
                        """
                        UPDATE products
                        SET stock_quantity = MAX(0.0, stock_quantity - ?), synced_to_cloud = 0, updated_at = ?
                        WHERE id = ? AND user_id = ? AND is_active = 1
                        """,
                        (float(item["quantity"]), now, valid_pid, uid),
                    )

            # Cari hesap hareketlerini kaydet (borç + satış anı ödemesi)
            if resolved_cust_id is not None and total_k > 0:
                conn.execute(
                    """
                    INSERT INTO credit_transactions (
                        user_id, customer_id, sale_id, tx_type, payment_method,
                        amount_kurus, signed_kurus, description, product_summary,
                        tx_date, status, created_by, created_by_role, created_at
                    ) VALUES (?, ?, ?, 'borc', ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                    """,
                    (
                        uid, resolved_cust_id, sale_id, pm,
                        total_k, total_k, f"Satış Fiş #{sale_id}", product_summary,
                        now, created_by, created_by_role, now,
                    ),
                )
                # Satış anında yapılan ödeme cariye alacak (-) olarak işlenir
                ledger_pay_k = total_k if (overpay_k > 0 and ov_mode == "change") else paid_total_k
                if ledger_pay_k > 0:
                    pay_pm = "karma" if (paid_cash_k > 0 and paid_pos_k > 0) else ("nakit" if paid_cash_k > 0 else "kart")
                    pay_desc = (
                        f"Satış Fiş #{sale_id} ödemesi (fazla ödeme alacak işlendi)"
                        if (overpay_k > 0 and ov_mode == "credit")
                        else f"Satış Fiş #{sale_id} ödemesi"
                    )
                    conn.execute(
                        """
                        INSERT INTO credit_transactions (
                            user_id, customer_id, sale_id, tx_type, payment_method,
                            amount_kurus, signed_kurus, description, product_summary,
                            tx_date, status, created_by, created_by_role, created_at
                        ) VALUES (?, ?, ?, 'odeme', ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                        """,
                        (
                            uid, resolved_cust_id, sale_id, pay_pm,
                            ledger_pay_k, -ledger_pay_k, pay_desc, product_summary,
                            now, created_by, created_by_role, now,
                        ),
                    )

            # Fazla ödeme para üstü olarak kasadan verildiyse kasa çıkışı kaydet
            if overpay_k > 0 and ov_mode == "change":
                change_tl = from_kurus(overpay_k)
                day_row = conn.execute(
                    "SELECT id FROM cash_days WHERE business_date = ? AND (user_id = ? OR user_id IS NULL) LIMIT 1",
                    (now[:10], uid),
                ).fetchone()
                cday_id = day_row["id"] if day_row else None
                conn.execute(
                    """
                    INSERT INTO cash_movements (
                        user_id, cash_day_id, movement_type, category, amount, description, customer_name,
                        movement_date, note, status, created_by, created_by_role, synced_to_cloud, created_at
                    ) VALUES (?, ?, 'kasa_cikis', 'para_ustu_verildi', ?, ?, ?, ?, ?, 'active', ?, ?, 0, ?)
                    """,
                    (
                        uid, cday_id, change_tl,
                        f"Fiş #{sale_id} para üstü iadesi",
                        cust_name, now, f"Satış #{sale_id} fazla ödeme para üstü",
                        created_by, created_by_role, now,
                    ),
                )

            log_audit(
                action="SALE_CREATE",
                entity_type="sale",
                entity_id=sale_id,
                old_value=None,
                new_value={
                    "id": sale_id,
                    "total_amount": total_amount,
                    "item_count": item_count,
                    "channel": ch,
                    "payment_method": pm,
                    "customer_id": resolved_cust_id,
                    "customer_name": cust_name,
                    "paid_cash_amount": from_kurus(paid_cash_k),
                    "paid_pos_amount": from_kurus(paid_pos_k),
                    "credit_amount": from_kurus(credit_net_k),
                    "overpayment_mode": ov_mode,
                    "sold_at": now,
                    "note": note,
                    "status": "active",
                },
                actor=act,
                user_id=uid,
                conn=conn,
            )

        # Dükkan satışındaki yumurtaları otomatik Çiftlik kaydına aktar
        try:
            from app.services.farm_service import parse_egg_product
            discount_ratio = (total_amount / calculated_total) if (total_amount_override is not None and calculated_total > 0) else 1.0
            for item in cart_items:
                p_name = str(item.get("product_name", "")).strip()
                p_norm = p_name.replace("İ", "i").replace("I", "ı").lower()
                if "yumurta" in p_norm:
                    orig_unit_price = float(item.get("unit_price", 0))
                    qty = float(item.get("quantity", 1))
                    effective_item_total = round(orig_unit_price * qty * discount_ratio, 2)

                    u_type, egg_boxes, egg_pieces = parse_egg_product(p_name, qty)
                    if u_type == "adet" and egg_pieces > 0:
                        effective_price = round(effective_item_total / egg_pieces, 2)
                    elif egg_boxes > 0:
                        effective_price = round(effective_item_total / egg_boxes, 2)
                    else:
                        effective_price = round(orig_unit_price * discount_ratio, 2)

                    egg_cust = cust_name or "Dükkan Perakende"
                    egg_note = f"Dükkan Satışı (Fiş #{sale_id}) - {p_name}"
                    self.add_farm_egg_sale(
                        customer_name=egg_cust,
                        box_count=egg_boxes,
                        unit_price=effective_price,
                        sale_date=now,
                        source="Dükkan",
                        note=egg_note,
                        user_id=uid,
                        unit_type=u_type,
                        piece_count=egg_pieces,
                        total_amount=effective_item_total,
                    )
        except Exception as e_egg:
            logger.warning(f"Dükkan yumurta satış aktarımı uyarısı: {e_egg}")

        def _async_sync_sale():
            self.ensure_firebase()
            if not self.firestore_db:
                return
            try:
                doc_id = f"u{uid}_s{sale_id}"
                sale_payload = {
                    "id": sale_id,
                    "user_id": uid,
                    "total_amount": total_amount,
                    "item_count": item_count,
                    "sold_at": now,
                    "note": note,
                    "channel": ch,
                    "payment_method": pm,
                    "customer_name": cust_name,
                    "items": cart_items,
                    "synced_to_cloud": 1,
                    "created_at": now,
                }
                self.firestore_db.collection("sales").document(doc_id).set(sale_payload)
                with self.db.get_connection() as c:
                    c.execute("UPDATE sales SET synced_to_cloud = 1 WHERE id = ?", (sale_id,))
                logger.info(f"Satış #{sale_id} ({total_amount:.2f} ₺) Firebase Firestore'a arka planda iletildi.")
            except Exception as e:
                logger.warning(f"Firestore satış kaydı iletim hatası: {e}")
                return

            try:
                for item in cart_items:
                    pid = item.get("product_id")
                    if pid:
                        with self.db.get_connection() as c:
                            p_row = c.execute("SELECT stock_quantity, unit FROM products WHERE id = ?", (pid,)).fetchone()
                        if p_row:
                            self.firestore_db.collection("products").document(f"u{uid}_p{pid}").set({
                                "stock_quantity": float(p_row["stock_quantity"]),
                                "unit": str(p_row["unit"]),
                                "updated_at": now,
                            }, merge=True)
                            with self.db.get_connection() as c:
                                c.execute("UPDATE products SET synced_to_cloud = 1 WHERE id = ?", (pid,))
            except Exception as e_stock:
                logger.warning(f"Firestore ürün stok anlık güncelleme uyarısı: {e_stock}")

        # Render üzerinde uyku/kapanma kaynaklı veri kaybını önlemek için Firestore kaydını anında senkron dene
        _async_sync_sale()
        return True, f"Satış başarıyla tamamlandı. Toplam: {total_amount:.2f} ₺"

    def cancel_sale(
        self,
        sale_id: Union[int, str],
        cancel_reason: str,
        cancel_note: str = "",
        user_id: Optional[int] = None,
        restore_stock: bool = True,
        actor: Optional[Dict[str, Any]] = None,
    ) -> tuple[bool, str]:
        """Satışı fiziksel olarak silmek yerine zorunlu neden ile 'cancelled' durumuna alır."""
        return self.delete_sale(
            sale_id,
            user_id=user_id,
            restore_stock=restore_stock,
            cancel_reason=cancel_reason,
            cancel_note=cancel_note,
            actor=actor,
            require_reason=True,
        )

    def delete_sale(
        self,
        sale_id: Union[int, str],
        user_id: Optional[int] = None,
        restore_stock: bool = True,
        cancel_reason: Optional[str] = None,
        cancel_note: str = "",
        actor: Optional[Dict[str, Any]] = None,
        require_reason: bool = False,
    ) -> tuple[bool, str]:
        """Satışı fiziksel olarak silmez; iptal edildi (cancelled) durumuna alır, stok iade eder ve audit log yazar."""
        if require_reason:
            ok_r, msg_r, clean_reason, clean_note = validate_cancel_reason(cancel_reason, cancel_note)
            if not ok_r:
                return False, msg_r
        else:
            clean_reason = str(cancel_reason or "Sistem / Yönetici iptali").strip()
            clean_note = str(cancel_note or "").strip()

        uid = self._resolve_user_id(user_id)
        now = get_turkey_now_str()
        act = actor or {"name": "Patron", "role": "patron", "user_id": uid}
        actor_name = str(act.get("name") or "Patron")
        actor_role = normalize_role(act.get("role"))

        # Handle farm egg sale cancellation if ID has "farm_" prefix
        s_id_str = str(sale_id).strip()
        if s_id_str.startswith("farm_"):
            try:
                fid = int(s_id_str.replace("farm_", ""))
                return self.delete_farm_egg_sale(
                    fid,
                    user_id=uid,
                    cancel_reason=clean_reason,
                    cancel_note=clean_note,
                    actor=act,
                    require_reason=require_reason,
                )
            except ValueError:
                return False, "Geçersiz çiftlik satış numarası!"

        try:
            numeric_sale_id = int(sale_id)
        except (ValueError, TypeError):
            return False, "Geçersiz satış numarası!"

        affected_product_ids = set()
        mirror_egg_ids = []

        with self.db.get_connection() as conn:
            sale = conn.execute(
                "SELECT * FROM sales WHERE id = ? AND (user_id = ? OR user_id IS NULL)",
                (numeric_sale_id, uid),
            ).fetchone()

            if not sale:
                return False, "Satış kaydı bulunamadı veya bu hesaba ait değil!"

            if str(sale["status"] or "active").lower() == "cancelled":
                return False, "Bu satış zaten iptal edilmiş!"

            old_snapshot = {
                "id": sale["id"],
                "total_amount": sale["total_amount"],
                "item_count": sale["item_count"],
                "sold_at": sale["sold_at"],
                "channel": sale["channel"],
                "customer_name": sale["customer_name"],
                "status": sale["status"] or "active",
            }

            if restore_stock:
                items = conn.execute(
                    "SELECT product_id, barcode, quantity FROM sale_items WHERE sale_id = ?",
                    (numeric_sale_id,),
                ).fetchall()
                for it in items:
                    pid = it["product_id"]
                    barcode = it["barcode"]
                    qty = float(it["quantity"] or 0)
                    if qty <= 0:
                        continue

                    updated = False
                    if pid:
                        cur = conn.execute(
                            """
                            UPDATE products
                            SET stock_quantity = stock_quantity + ?, updated_at = ?, synced_to_cloud = 0
                            WHERE id = ? AND (user_id = ? OR user_id IS NULL)
                            """,
                            (qty, now, pid, uid),
                        )
                        if cur.rowcount > 0:
                            updated = True
                            affected_product_ids.add(pid)

                    if not updated and barcode:
                        cur = conn.execute(
                            """
                            UPDATE products
                            SET stock_quantity = stock_quantity + ?, updated_at = ?, synced_to_cloud = 0
                            WHERE barcode = ? AND (user_id = ? OR user_id IS NULL)
                            """,
                            (qty, now, barcode, uid),
                        )
                        if cur.rowcount > 0:
                            p_row = conn.execute(
                                "SELECT id FROM products WHERE barcode = ? AND (user_id = ? OR user_id IS NULL)",
                                (barcode, uid),
                            ).fetchone()
                            if p_row:
                                affected_product_ids.add(p_row["id"])

            # Fiziksel silme yerine iptal edildi (cancelled) durumuna al
            egg_rows = conn.execute(
                "SELECT id FROM farm_egg_sales WHERE (note LIKE ? OR note LIKE ?) AND (user_id = ? OR user_id IS NULL)",
                (f"%Fiş #{numeric_sale_id}%", f"%Fiş #{numeric_sale_id})%", uid)
            ).fetchall()
            mirror_egg_ids = [r["id"] for r in egg_rows]
            conn.execute(
                """
                UPDATE sales
                SET status = 'cancelled',
                    cancelled_at = ?,
                    cancelled_by = ?,
                    cancelled_by_role = ?,
                    cancel_reason = ?,
                    cancel_note = ?,
                    synced_to_cloud = 0
                WHERE id = ? AND (user_id = ? OR user_id IS NULL)
                """,
                (now, actor_name, actor_role, clean_reason, clean_note, numeric_sale_id, uid),
            )
            # Varsa bu dükkan fişinden türemiş çiftlik yumurta kaydını da iptal et
            conn.execute(
                """
                UPDATE farm_egg_sales
                SET status = 'cancelled',
                    cancelled_at = ?,
                    cancelled_by = ?,
                    cancelled_by_role = ?,
                    cancel_reason = ?,
                    cancel_note = ?,
                    synced_to_cloud = 0
                WHERE (note LIKE ? OR note LIKE ?) AND (user_id = ? OR user_id IS NULL)
                  AND COALESCE(status, 'active') != 'cancelled'
                """,
                (now, actor_name, actor_role, clean_reason, clean_note, f"%Fiş #{numeric_sale_id}%", f"%Fiş #{numeric_sale_id})%", uid),
            )
            # Varsa bu satışa bağlı cari hesap (veresiye) hareketlerini ve para üstü çıkışını da iptal et
            conn.execute(
                """
                UPDATE credit_transactions
                SET status = 'cancelled',
                    cancelled_at = ?,
                    cancelled_by = ?,
                    cancelled_by_role = ?,
                    cancel_reason = ?,
                    cancel_note = ?
                WHERE sale_id = ? AND COALESCE(status, 'active') != 'cancelled'
                """,
                (now, actor_name, actor_role, clean_reason, clean_note, numeric_sale_id),
            )
            conn.execute(
                """
                UPDATE cash_movements
                SET status = 'cancelled',
                    cancelled_at = ?,
                    cancelled_by = ?,
                    cancelled_by_role = ?,
                    cancel_reason = ?,
                    cancel_note = ?,
                    synced_to_cloud = 0
                WHERE category = 'para_ustu_verildi' AND description LIKE ?
                  AND COALESCE(status, 'active') != 'cancelled'
                """,
                (now, actor_name, actor_role, clean_reason, clean_note, f"Fiş #{numeric_sale_id} %"),
            )

            log_audit(
                action="SALE_CANCEL",
                entity_type="sale",
                entity_id=numeric_sale_id,
                old_value=old_snapshot,
                new_value={
                    "status": "cancelled",
                    "cancelled_at": now,
                    "cancelled_by": actor_name,
                    "cancelled_by_role": actor_role,
                    "cancel_reason": clean_reason,
                    "cancel_note": clean_note,
                    "restore_stock": restore_stock,
                },
                reason=clean_reason + (f" — {clean_note}" if clean_note else ""),
                actor=act,
                user_id=uid,
                conn=conn,
            )

        if self.firestore_db:
            def _async_del_sale():
                try:
                    doc_id = f"u{uid}_s{numeric_sale_id}"
                    cancel_payload = {
                        "status": "cancelled",
                        "cancelled_at": now,
                        "cancelled_by": actor_name,
                        "cancelled_by_role": actor_role,
                        "cancel_reason": clean_reason,
                        "cancel_note": clean_note,
                    }
                    self.firestore_db.collection("sales").document(doc_id).set(cancel_payload, merge=True)
                    for eid in mirror_egg_ids:
                        self.firestore_db.collection("farm_egg_sales").document(f"u{uid}_es{eid}").set(cancel_payload, merge=True)
                except Exception as e:
                    logger.warning(f"Firestore satış iptal güncelleme uyarısı: {e}")

                if restore_stock and affected_product_ids:
                    with self.db.get_connection() as c:
                        for pid in affected_product_ids:
                            p_row = c.execute(
                                "SELECT stock_quantity, unit FROM products WHERE id = ?", (pid,)
                            ).fetchone()
                            if p_row:
                                try:
                                    self.firestore_db.collection("products").document(f"u{uid}_p{pid}").update({
                                        "stock_quantity": float(p_row["stock_quantity"]),
                                        "unit": p_row["unit"],
                                        "updated_at": now,
                                        "synced_to_cloud": 1,
                                    })
                                    c.execute("UPDATE products SET synced_to_cloud = 1 WHERE id = ?", (pid,))
                                except Exception as ex:
                                    logger.warning(f"Firestore ürün stok güncelleme uyarısı ({pid}): {ex}")
            if hasattr(self.firestore_db, "_mock_name"):
                _async_del_sale()
            else:
                threading.Thread(target=_async_del_sale, daemon=True).start()

        return True, "Satış iptal edildi ve ürün stokları depoya geri yüklendi."

    def delete_sales_bulk(
        self,
        sale_ids: List[Union[int, str]],
        user_id: Optional[int] = None,
        restore_stock: bool = True,
        cancel_reason: Optional[str] = None,
        cancel_note: str = "",
        actor: Optional[Dict[str, Any]] = None,
        require_reason: bool = False,
    ) -> tuple[bool, str, int]:
        """Birden fazla satışı fiziksel silme yerine tek işlemde iptal eder ve stokları iade eder."""
        if not sale_ids:
            return False, "İptal edilecek satış seçilmedi!", 0

        if require_reason:
            ok_r, msg_r, clean_reason, clean_note = validate_cancel_reason(cancel_reason, cancel_note)
            if not ok_r:
                return False, msg_r, 0
        else:
            clean_reason = str(cancel_reason or "Toplu yönetici iptali").strip()
            clean_note = str(cancel_note or "").strip()

        uid = self._resolve_user_id(user_id)
        now = get_turkey_now_str()
        act = actor or {"name": "Patron", "role": "patron", "user_id": uid}
        actor_name = str(act.get("name") or "Patron")
        actor_role = normalize_role(act.get("role"))

        farm_ids: List[int] = []
        cleaned_ids: List[int] = []
        for sid in sale_ids:
            s_str = str(sid).strip()
            if s_str.startswith("farm_"):
                try:
                    farm_ids.append(int(s_str.replace("farm_", "")))
                except ValueError:
                    pass
            else:
                try:
                    cleaned_ids.append(int(s_str))
                except (ValueError, TypeError):
                    pass

        total_cancelled = 0

        for fid in farm_ids:
            ok, _ = self.delete_farm_egg_sale(
                fid,
                user_id=uid,
                cancel_reason=clean_reason,
                cancel_note=clean_note,
                actor=act,
                require_reason=require_reason,
            )
            if ok:
                total_cancelled += 1

        if not cleaned_ids:
            if total_cancelled > 0:
                return True, f"{total_cancelled} adet satış başarıyla iptal edildi.", total_cancelled
            return False, "Geçersiz satış listesi!", 0

        affected_product_ids = set()

        with self.db.get_connection() as conn:
            placeholders = ",".join("?" for _ in cleaned_ids)
            valid_sales = conn.execute(
                f"""
                SELECT * FROM sales
                WHERE id IN ({placeholders})
                  AND (user_id = ? OR user_id IS NULL)
                  AND COALESCE(status, 'active') != 'cancelled'
                """,
                (*cleaned_ids, uid),
            ).fetchall()
            valid_ids = [r["id"] for r in valid_sales]

            if not valid_ids and total_cancelled == 0:
                return False, "Seçilen satışlar bulunamadı veya zaten iptal edilmiş!", 0

            if valid_ids and restore_stock:
                v_placeholders = ",".join("?" for _ in valid_ids)
                items = conn.execute(
                    f"SELECT product_id, barcode, quantity FROM sale_items WHERE sale_id IN ({v_placeholders})",
                    valid_ids,
                ).fetchall()
                for it in items:
                    pid = it["product_id"]
                    barcode = it["barcode"]
                    qty = float(it["quantity"] or 0)
                    if qty <= 0:
                        continue

                    updated = False
                    if pid:
                        cur = conn.execute(
                            """
                            UPDATE products
                            SET stock_quantity = stock_quantity + ?, updated_at = ?, synced_to_cloud = 0
                            WHERE id = ? AND (user_id = ? OR user_id IS NULL)
                            """,
                            (qty, now, pid, uid),
                        )
                        if cur.rowcount > 0:
                            updated = True
                            affected_product_ids.add(pid)

                    if not updated and barcode:
                        cur = conn.execute(
                            """
                            UPDATE products
                            SET stock_quantity = stock_quantity + ?, updated_at = ?, synced_to_cloud = 0
                            WHERE barcode = ? AND (user_id = ? OR user_id IS NULL)
                            """,
                            (qty, now, barcode, uid),
                        )
                        if cur.rowcount > 0:
                            p_row = conn.execute(
                                "SELECT id FROM products WHERE barcode = ? AND (user_id = ? OR user_id IS NULL)",
                                (barcode, uid),
                            ).fetchone()
                            if p_row:
                                affected_product_ids.add(p_row["id"])

            if valid_ids:
                v_placeholders = ",".join("?" for _ in valid_ids)
                conn.execute(
                    f"""
                    UPDATE sales
                    SET status = 'cancelled',
                        cancelled_at = ?,
                        cancelled_by = ?,
                        cancelled_by_role = ?,
                        cancel_reason = ?,
                        cancel_note = ?,
                        synced_to_cloud = 0
                    WHERE id IN ({v_placeholders}) AND (user_id = ? OR user_id IS NULL)
                    """,
                    (now, actor_name, actor_role, clean_reason, clean_note, *valid_ids, uid),
                )
                for sr in valid_sales:
                    vid = sr["id"]
                    conn.execute(
                        """
                        UPDATE farm_egg_sales
                        SET status = 'cancelled',
                            cancelled_at = ?,
                            cancelled_by = ?,
                            cancelled_by_role = ?,
                            cancel_reason = ?,
                            cancel_note = ?,
                            synced_to_cloud = 0
                        WHERE (note LIKE ? OR note LIKE ?) AND (user_id = ? OR user_id IS NULL)
                          AND COALESCE(status, 'active') != 'cancelled'
                        """,
                        (now, actor_name, actor_role, clean_reason, clean_note, f"%Fiş #{vid}%", f"%Fiş #{vid})%", uid),
                    )
                    conn.execute(
                        """
                        UPDATE credit_transactions
                        SET status = 'cancelled',
                            cancelled_at = ?,
                            cancelled_by = ?,
                            cancelled_by_role = ?,
                            cancel_reason = ?,
                            cancel_note = ?
                        WHERE sale_id = ? AND COALESCE(status, 'active') != 'cancelled'
                        """,
                        (now, actor_name, actor_role, clean_reason, clean_note, vid),
                    )
                    log_audit(
                        action="SALE_CANCEL",
                        entity_type="sale",
                        entity_id=vid,
                        old_value={
                            "id": vid,
                            "total_amount": sr["total_amount"],
                            "sold_at": sr["sold_at"],
                            "status": sr["status"] or "active",
                        },
                        new_value={
                            "status": "cancelled",
                            "cancelled_at": now,
                            "cancelled_by": actor_name,
                            "cancelled_by_role": actor_role,
                            "cancel_reason": clean_reason,
                            "cancel_note": clean_note,
                        },
                        reason=clean_reason + (f" — {clean_note}" if clean_note else ""),
                        actor=act,
                        user_id=uid,
                        conn=conn,
                    )
                total_cancelled += len(valid_ids)

        if self.firestore_db and valid_ids:
            for sid in valid_ids:
                try:
                    self.firestore_db.collection("sales").document(f"u{uid}_s{sid}").set(
                        {
                            "status": "cancelled",
                            "cancelled_at": now,
                            "cancelled_by": actor_name,
                            "cancelled_by_role": actor_role,
                            "cancel_reason": clean_reason,
                            "cancel_note": clean_note,
                        },
                        merge=True,
                    )
                except Exception:
                    pass

            if restore_stock and affected_product_ids:
                with self.db.get_connection() as conn:
                    for pid in affected_product_ids:
                        p_row = conn.execute(
                            "SELECT stock_quantity, unit FROM products WHERE id = ?", (pid,)
                        ).fetchone()
                        if p_row:
                            try:
                                self.firestore_db.collection("products").document(f"u{uid}_p{pid}").update({
                                    "stock_quantity": float(p_row["stock_quantity"]),
                                    "unit": p_row["unit"],
                                    "updated_at": now,
                                })
                                conn.execute("UPDATE products SET synced_to_cloud = 1 WHERE id = ?", (pid,))
                            except Exception as ex:
                                logger.warning(f"Firestore ürün stok güncelleme uyarısı ({pid}): {ex}")

        return True, f"{total_cancelled} adet satış iptal edildi ve ürün stokları depoya geri yüklendi.", total_cancelled

    def update_sale_datetime(
        self,
        sale_id: Union[int, str],
        new_sold_at: str,
        user_id: Optional[int] = None,
        actor: Optional[Dict[str, Any]] = None,
    ) -> tuple[bool, str]:
        """Satış kaydının tarih ve saatini günceller (geriye dönük satış düzenleme)."""
        uid = self._resolve_user_id(user_id)
        act = actor or {"name": "Patron", "role": "patron", "user_id": uid}
        if not new_sold_at or not str(new_sold_at).strip():
            return False, "Geçerli bir tarih ve saat belirtilmelidir."

        raw = str(new_sold_at).strip().replace("T", " ")
        try:
            if len(raw) == 10:
                formatted_dt = f"{raw} 12:00:00"
            elif len(raw) == 16:
                formatted_dt = f"{raw}:00"
            elif len(raw) >= 19:
                formatted_dt = raw[:19]
            else:
                datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
                formatted_dt = raw
        except Exception:
            return False, "Tarih/saat formatı geçersiz (YYYY-MM-DD HH:MM:SS beklenir)."

        now = get_turkey_now_str()

        # Çiftlik yumurta satışı kontrolü
        s_id_str = str(sale_id).strip()
        if s_id_str.startswith("farm_"):
            try:
                fid = int(s_id_str.replace("farm_", ""))
            except ValueError:
                return False, "Geçersiz çiftlik satış numarası!"

            with self.db.get_connection() as conn:
                row = conn.execute(
                    "SELECT id, sale_date, status FROM farm_egg_sales WHERE id = ? AND (user_id = ? OR user_id IS NULL)",
                    (fid, uid)
                ).fetchone()
                if not row:
                    return False, "Çiftlik satış kaydı bulunamadı veya bu hesaba ait değil!"
                if str(row["status"] or "active").lower() == "cancelled":
                    return False, "İptal edilmiş kayıt düzenlenemez!"
                old_dt = row["sale_date"]
                conn.execute(
                    "UPDATE farm_egg_sales SET sale_date = ?, synced_to_cloud = 0 WHERE id = ?",
                    (formatted_dt, fid)
                )
                log_audit(
                    action="FARM_EGG_SALE_DATETIME_UPDATE",
                    entity_type="farm_egg_sale",
                    entity_id=fid,
                    old_value={"sale_date": old_dt},
                    new_value={"sale_date": formatted_dt},
                    actor=act,
                    user_id=uid,
                    conn=conn,
                )

            if self.firestore_db:
                def _sync_upd_egg():
                    try:
                        self.firestore_db.collection("farm_egg_sales").document(f"u{uid}_es{fid}").update({
                            "sale_date": formatted_dt,
                        })
                    except Exception as ex:
                        logger.warning(f"Firestore farm egg sale datetime update warning: {ex}")
                threading.Thread(target=_sync_upd_egg, daemon=True).start()

            return True, "Çiftlik satış tarihi ve saati başarıyla güncellendi."

        try:
            numeric_sale_id = int(sale_id)
        except (ValueError, TypeError):
            return False, "Geçersiz satış numarası!"

        with self.db.get_connection() as conn:
            sale = conn.execute(
                "SELECT id, sold_at, status FROM sales WHERE id = ? AND (user_id = ? OR user_id IS NULL)",
                (numeric_sale_id, uid)
            ).fetchone()

            if not sale:
                return False, "Satış kaydı bulunamadı veya bu hesaba ait değil!"
            if str(sale["status"] or "active").lower() == "cancelled":
                return False, "İptal edilmiş satış düzenlenemez!"

            old_dt = sale["sold_at"]
            conn.execute(
                "UPDATE sales SET sold_at = ?, synced_to_cloud = 0 WHERE id = ? AND (user_id = ? OR user_id IS NULL)",
                (formatted_dt, numeric_sale_id, uid)
            )
            # Dükkandan satılan yumurtanın çiftlik modülündeki kaydının tarihini de güncelle
            conn.execute(
                "UPDATE farm_egg_sales SET sale_date = ?, synced_to_cloud = 0 WHERE (note LIKE ? OR note LIKE ?) AND (user_id = ? OR user_id IS NULL)",
                (formatted_dt, f"%Fiş #{numeric_sale_id}%", f"%Fiş #{numeric_sale_id})%", uid)
            )
            log_audit(
                action="SALE_DATETIME_UPDATE",
                entity_type="sale",
                entity_id=numeric_sale_id,
                old_value={"sold_at": old_dt},
                new_value={"sold_at": formatted_dt},
                actor=act,
                user_id=uid,
                conn=conn,
            )

        if self.firestore_db:
            doc_id = f"u{uid}_s{numeric_sale_id}"
            try:
                self.firestore_db.collection("sales").document(doc_id).update({
                    "sold_at": formatted_dt,
                    "updated_at": now,
                })
                with self.db.get_connection() as conn:
                    conn.execute("UPDATE sales SET synced_to_cloud = 1 WHERE id = ?", (numeric_sale_id,))
            except Exception as ex:
                logger.warning(f"Firestore satış tarihi güncelleme uyarısı ({doc_id}): {ex}")

            def _sync_upd_shop_farm_egg():
                try:
                    docs = self.firestore_db.collection("farm_egg_sales").where("user_id", "==", uid).stream()
                    for d in docs:
                        data = d.to_dict()
                        n = str(data.get("note") or "")
                        if f"Fiş #{numeric_sale_id}" in n:
                            d.reference.update({"sale_date": formatted_dt})
                except Exception as ex:
                    logger.warning(f"Firestore farm egg sale date update warning: {ex}")
            threading.Thread(target=_sync_upd_shop_farm_egg, daemon=True).start()

        return True, "Satış tarihi ve saati başarıyla güncellendi."

    def get_sales_history(
        self,
        user_id: Optional[Union[int, str]] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        customer_name: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        uid = self._resolve_user_id(user_id)
        query = "SELECT * FROM sales WHERE (user_id = ? OR user_id IS NULL)"
        params: list = [uid]
        if start_date:
            query += " AND replace(sold_at, 'T', ' ') >= ?"
            params.append(start_date + " 00:00:00")
        if end_date:
            query += " AND replace(sold_at, 'T', ' ') <= ?"
            params.append(end_date + " 23:59:59")
        if customer_name and str(customer_name).strip():
            query += " AND customer_name LIKE ?"
            params.append(f"%{customer_name.strip()}%")
        query += " ORDER BY replace(sold_at, 'T', ' ') DESC, id DESC LIMIT 500"

        def _fetch_with_items(q, p):
            with self.db.get_connection() as conn:
                rows = conn.execute(q, p).fetchall()
                s_list = [dict(r) for r in rows] if rows else []
                s_ids = [s["id"] for s in s_list]
                if s_ids:
                    placeholders = ",".join("?" for _ in s_ids)
                    items_query = f"SELECT * FROM sale_items WHERE sale_id IN ({placeholders})"
                    items_rows = conn.execute(items_query, s_ids).fetchall()
                    items_by_sale: Dict[int, List[Dict[str, Any]]] = {}
                    for i in items_rows:
                        i_dict = dict(i)
                        items_by_sale.setdefault(i_dict["sale_id"], []).append(i_dict)
                    for s in s_list:
                        s["items"] = items_by_sale.get(s["id"], [])
                return s_list

        sales = _fetch_with_items(query, params)

        # Eğer yerel veritabanında satış bulunamadıysa ve Firestore bağlıysa, otomatik çekmeyi dene
        if not sales and self.ensure_firebase():
            with self.db.get_connection() as conn:
                tot_s = conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0]
            if tot_s == 0:
                now_ts = time.time()
                if now_ts - self._last_pull_timestamp > 10:
                    try:
                        self.pull_all_from_firebase(user_id=uid)
                        self._last_pull_timestamp = now_ts
                        self._hydration_completed = True
                        sales = _fetch_with_items(query, params)
                    except Exception as e_pull:
                        logger.warning(f"get_sales_history Firestore otomatik çekme uyarısı: {e_pull}")

        # Eğer hala satış bulunamadıysa, mağazada herhangi bir kullanıcıya ait satış var mı kontrol et
        if not sales:
            try:
                fb_query = query.replace("(user_id = ? OR user_id IS NULL)", "1=1")
                fb_params = params[1:]
                sales = _fetch_with_items(fb_query, fb_params)
            except Exception:
                pass

        return sales

    def get_recent_sales(
        self,
        user_id: Optional[Union[int, str]] = None,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """Satış ekranının (POS) kenarında son yapılan işlemleri göstermek için en son N satışı döner."""
        uid = self._resolve_user_id(user_id)
        safe_limit = max(1, min(int(limit or 5), 50))
        query = (
            "SELECT * FROM sales WHERE (user_id = ? OR user_id IS NULL) "
            "ORDER BY replace(sold_at, 'T', ' ') DESC, id DESC LIMIT ?"
        )
        params: list = [uid, safe_limit]

        def _fetch_recent(q: str, p: list) -> List[Dict[str, Any]]:
            with self.db.get_connection() as conn:
                rows = conn.execute(q, p).fetchall()
                s_list = [dict(r) for r in rows] if rows else []
                s_ids = [s["id"] for s in s_list]
                if s_ids:
                    placeholders = ",".join("?" for _ in s_ids)
                    items_query = f"SELECT * FROM sale_items WHERE sale_id IN ({placeholders})"
                    items_rows = conn.execute(items_query, s_ids).fetchall()
                    items_by_sale: Dict[int, List[Dict[str, Any]]] = {}
                    for i in items_rows:
                        i_dict = dict(i)
                        items_by_sale.setdefault(i_dict["sale_id"], []).append(i_dict)
                    for s in s_list:
                        s["items"] = items_by_sale.get(s["id"], [])
                return s_list

        sales = _fetch_recent(query, params)

        if not sales and self.ensure_firebase():
            with self.db.get_connection() as conn:
                tot_s = conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0]
            if tot_s == 0:
                now_ts = time.time()
                if now_ts - self._last_pull_timestamp > 10:
                    try:
                        self.pull_all_from_firebase(user_id=uid)
                        self._last_pull_timestamp = now_ts
                        self._hydration_completed = True
                        sales = _fetch_recent(query, params)
                    except Exception as e_pull:
                        logger.warning(f"get_recent_sales Firestore otomatik çekme uyarısı: {e_pull}")

        if not sales:
            try:
                fb_query = query.replace("(user_id = ? OR user_id IS NULL)", "1=1")
                fb_params = [safe_limit]
                sales = _fetch_recent(fb_query, fb_params)
            except Exception:
                pass

        return sales

    def sync_shop_egg_sales_to_farm(self, user_id: Optional[int] = None) -> int:
        """Dükkanda satılmış olup henüz farm_egg_sales tablosuna aktarılmamış tüm yumurta satışlarını aktarır."""
        uid = self._resolve_user_id(user_id) or 1
        synced_count = 0
        from app.services.farm_service import parse_egg_product
        pending_to_add = []
        with self.db.get_connection() as conn:
            if user_id is not None:
                query = """
                    SELECT si.id as item_id, si.sale_id, si.product_name, si.quantity, si.unit_price, si.subtotal,
                           s.sold_at, s.customer_name, s.user_id
                    FROM sale_items si
                    JOIN sales s ON si.sale_id = s.id
                    WHERE (s.user_id = ? OR s.user_id IS NULL)
                """
                rows = conn.execute(query, (uid,)).fetchall()
            else:
                query = """
                    SELECT si.id as item_id, si.sale_id, si.product_name, si.quantity, si.unit_price, si.subtotal,
                           s.sold_at, s.customer_name, s.user_id
                    FROM sale_items si
                    JOIN sales s ON si.sale_id = s.id
                """
                rows = conn.execute(query).fetchall()

            for r in rows:
                p_name = str(r["product_name"] or "").strip()
                p_norm = p_name.replace("İ", "i").replace("I", "ı").lower()
                if "yumurta" not in p_norm:
                    continue

                sid = r["sale_id"]
                row_uid = r["user_id"] or uid
                existing = conn.execute(
                    "SELECT id FROM farm_egg_sales WHERE (user_id = ? OR user_id IS NULL) AND (note LIKE ? OR note LIKE ?)",
                    (row_uid, f"%Fiş #{sid}%", f"%Fiş #{sid})%")
                ).fetchone()
                if existing:
                    continue

                qty = float(r["quantity"] or 1)
                subtot = float(r["subtotal"] or 0)
                u_type, egg_boxes, egg_pieces = parse_egg_product(p_name, qty)
                if u_type == "adet" and egg_pieces > 0:
                    eff_price = round(subtot / egg_pieces, 2)
                elif egg_boxes > 0:
                    eff_price = round(subtot / egg_boxes, 2)
                else:
                    eff_price = float(r["unit_price"] or 0)

                egg_cust = r["customer_name"] or "Dükkan Perakende"
                egg_note = f"Dükkan Satışı (Fiş #{sid}) - {p_name}"
                pending_to_add.append({
                    "customer_name": egg_cust,
                    "box_count": egg_boxes,
                    "unit_price": eff_price,
                    "sale_date": r["sold_at"],
                    "source": "Dükkan",
                    "note": egg_note,
                    "user_id": row_uid,
                    "unit_type": u_type,
                    "piece_count": egg_pieces,
                    "total_amount": subtot,
                })

        for item in pending_to_add:
            ok, _, _ = self.add_farm_egg_sale(**item)
            if ok:
                synced_count += 1
        return synced_count

    def get_sales_analytics(
        self,
        user_id: Optional[Union[int, str]] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        uid = self._resolve_user_id(user_id)
        with self.db.get_connection() as conn:
            sales_query = (
                "SELECT COUNT(*) as total_transactions, SUM(total_amount) as total_revenue, "
                "SUM(item_count) as total_items FROM sales "
                "WHERE (user_id = ? OR user_id IS NULL) AND COALESCE(status, 'active') != 'cancelled'"
            )
            s_params: list = [uid]
            if start_date:
                sales_query += " AND replace(sold_at, 'T', ' ') >= ?"
                s_params.append(start_date + " 00:00:00")
            if end_date:
                sales_query += " AND replace(sold_at, 'T', ' ') <= ?"
                s_params.append(end_date + " 23:59:59")

            s_row = conn.execute(sales_query, s_params).fetchone()
            total_tx = s_row["total_transactions"] if s_row else 0
            if total_tx == 0 and not self._hydration_completed:
                try:
                    self.ensure_initial_hydration()
                    s_row = conn.execute(sales_query, s_params).fetchone()
                    total_tx = s_row["total_transactions"] if s_row else 0
                except Exception:
                    pass

            if total_tx == 0:
                fb_sq = sales_query.replace("(user_id = ? OR user_id IS NULL)", "1=1")
                fb_sparams = s_params[1:]
                fb_row = conn.execute(fb_sq, fb_sparams).fetchone()
                if fb_row and fb_row["total_transactions"]:
                    s_row = fb_row
                    total_tx = fb_row["total_transactions"]
                    uid_filter = "1=1"
                    top_params = [p for p in s_params[1:]]
                    ch_uid_filter = "1=1"
                    ch_params_extra = [p for p in s_params[1:]]
                else:
                    uid_filter = "(s.user_id = ? OR s.user_id IS NULL)"
                    top_params = [uid]
                    ch_uid_filter = "(user_id = ? OR user_id IS NULL)"
                    ch_params_extra = [uid]
            else:
                uid_filter = "(s.user_id = ? OR s.user_id IS NULL)"
                top_params = [uid]
                ch_uid_filter = "(user_id = ? OR user_id IS NULL)"
                ch_params_extra = [uid]

            total_rev = s_row["total_revenue"] or 0.0
            total_items = s_row["total_items"] or 0
            avg_cart = (total_rev / total_tx) if total_tx > 0 else 0.0

            # Top 5 Best Selling Products for this user (iptal edilen satışlar hariç)
            top_query = f"""
                SELECT si.product_name, SUM(si.quantity) as total_qty, SUM(si.subtotal) as total_sales_amount
                FROM sale_items si
                JOIN sales s ON si.sale_id = s.id
                WHERE {uid_filter} AND COALESCE(s.status, 'active') != 'cancelled'
            """
            if start_date:
                top_query += " AND replace(s.sold_at, 'T', ' ') >= ?"
                top_params.append(start_date + " 00:00:00")
            if end_date:
                top_query += " AND replace(s.sold_at, 'T', ' ') <= ?"
                top_params.append(end_date + " 23:59:59")
            top_query += " GROUP BY si.product_name ORDER BY total_qty DESC LIMIT 5"
            top_rows = conn.execute(top_query, top_params).fetchall()
            top_products = [dict(r) for r in top_rows]

            # Low Stock Items for this user
            low_query = "SELECT id, name, barcode, stock_quantity, critical_stock_level FROM products WHERE is_active = 1 AND (user_id = ? OR user_id IS NULL) AND stock_quantity <= critical_stock_level"
            low_rows = conn.execute(low_query, [uid]).fetchall()
            if not low_rows:
                low_rows = conn.execute("SELECT id, name, barcode, stock_quantity, critical_stock_level FROM products WHERE is_active = 1 AND stock_quantity <= critical_stock_level").fetchall()
            low_stock_items = [dict(r) for r in low_rows]

            # Total Expenses for this user (iptal edilen giderler hariç)
            exp_query = (
                "SELECT SUM(amount) as total_exp FROM expenses "
                "WHERE (user_id = ? OR user_id IS NULL) AND COALESCE(status, 'active') != 'cancelled'"
            )
            exp_params: list = [uid]
            if start_date:
                exp_query += " AND expense_date >= ?"
                exp_params.append(start_date)
            if end_date:
                exp_query += " AND expense_date <= ?"
                exp_params.append(end_date)

            exp_row = conn.execute(exp_query, exp_params).fetchone()
            total_exp = exp_row["total_exp"] or 0.0 if exp_row else 0.0
            net_profit = total_rev - total_exp

            # Channel Breakdown (Mağaza vs İnternet, iptal edilenler hariç)
            ch_query = f"""
                SELECT COALESCE(channel, 'magaza') as channel,
                       COUNT(*) as tx_count,
                       SUM(total_amount) as revenue,
                       SUM(item_count) as items
                FROM sales
                WHERE {ch_uid_filter} AND COALESCE(status, 'active') != 'cancelled'
            """
            ch_params: list = ch_params_extra
            if start_date:
                ch_query += " AND replace(sold_at, 'T', ' ') >= ?"
                ch_params.append(start_date + " 00:00:00")
            if end_date:
                ch_query += " AND replace(sold_at, 'T', ' ') <= ?"
                ch_params.append(end_date + " 23:59:59")
            ch_query += " GROUP BY COALESCE(channel, 'magaza')"
            ch_rows = conn.execute(ch_query, ch_params).fetchall()

            channel_stats = {
                "magaza": {"revenue": 0.0, "items": 0, "transactions": 0},
                "internet": {"revenue": 0.0, "items": 0, "transactions": 0},
            }
            for row in ch_rows:
                k = row["channel"] if row["channel"] in channel_stats else "magaza"
                channel_stats[k]["revenue"] = round(float(row["revenue"] or 0.0), 2)
                channel_stats[k]["items"] = int(row["items"] or 0)
                channel_stats[k]["transactions"] = int(row["tx_count"] or 0)

            # Monthly Breakdown (Her Ayın Toplam Cirosu - iptal edilenler hariç)
            month_query = """
                SELECT strftime('%Y-%m', sold_at) as month_key,
                       COUNT(*) as tx_count,
                       SUM(total_amount) as revenue,
                       SUM(item_count) as items,
                       SUM(CASE WHEN COALESCE(channel, 'magaza') = 'magaza' THEN total_amount ELSE 0 END) as store_revenue,
                       SUM(CASE WHEN COALESCE(channel, 'magaza') = 'internet' THEN total_amount ELSE 0 END) as net_revenue
                FROM sales
                WHERE (user_id = ? OR user_id IS NULL) AND COALESCE(status, 'active') != 'cancelled'
                GROUP BY month_key
                ORDER BY month_key DESC
                LIMIT 24
            """
            month_rows = conn.execute(month_query, (uid,)).fetchall()
            months_tr = {
                "01": "Ocak", "02": "Şubat", "03": "Mart", "04": "Nisan",
                "05": "Mayıs", "06": "Haziran", "07": "Temmuz", "08": "Ağustos",
                "09": "Eylül", "10": "Ekim", "11": "Kasım", "12": "Aralık"
            }
            monthly_stats = []
            for mr in month_rows:
                m_key = mr["month_key"] or ""
                if not m_key:
                    continue
                parts = m_key.split("-")
                m_label = f"{months_tr.get(parts[1], parts[1])} {parts[0]}" if len(parts) == 2 else m_key
                rev = round(float(mr["revenue"] or 0), 2)
                tx = int(mr["tx_count"] or 0)
                monthly_stats.append({
                    "month_key": m_key,
                    "month_label": m_label,
                    "revenue": rev,
                    "tx_count": tx,
                    "items": int(mr["items"] or 0),
                    "avg_cart": round(rev / tx, 2) if tx > 0 else 0.0,
                    "store_revenue": round(float(mr["store_revenue"] or 0), 2),
                    "net_revenue": round(float(mr["net_revenue"] or 0), 2),
                })

            # Weekly Breakdown (Her Haftanın Toplam Cirosu - iptal edilenler hariç)
            week_query = """
                SELECT strftime('%Y-W%W', sold_at) as week_key,
                       MIN(date(sold_at)) as week_start,
                       MAX(date(sold_at)) as week_end,
                       COUNT(*) as tx_count,
                       SUM(total_amount) as revenue,
                       SUM(item_count) as items,
                       SUM(CASE WHEN COALESCE(channel, 'magaza') = 'magaza' THEN total_amount ELSE 0 END) as store_revenue,
                       SUM(CASE WHEN COALESCE(channel, 'magaza') = 'internet' THEN total_amount ELSE 0 END) as net_revenue
                FROM sales
                WHERE (user_id = ? OR user_id IS NULL) AND COALESCE(status, 'active') != 'cancelled'
                GROUP BY week_key
                ORDER BY week_key DESC
                LIMIT 16
            """
            week_rows = conn.execute(week_query, (uid,)).fetchall()
            weekly_stats = []
            for wr in week_rows:
                w_key = wr["week_key"] or ""
                if not w_key:
                    continue
                w_num = w_key.split("-W")[-1] if "-W" in w_key else w_key
                w_start = wr["week_start"] or ""
                w_end = wr["week_end"] or ""

                def format_short_tr(d_str):
                    try:
                        dt = datetime.strptime(d_str, "%Y-%m-%d")
                        m_name = months_tr.get(f"{dt.month:02d}", "")
                        return f"{dt.day} {m_name}"
                    except Exception:
                        return d_str

                range_str = f"{format_short_tr(w_start)} - {format_short_tr(w_end)}" if w_start and w_end else w_key
                rev = round(float(wr["revenue"] or 0), 2)
                tx = int(wr["tx_count"] or 0)
                weekly_stats.append({
                    "week_key": w_key,
                    "week_label": f"{w_num}. Hafta ({range_str})",
                    "week_start": w_start,
                    "week_end": w_end,
                    "revenue": rev,
                    "tx_count": tx,
                    "items": int(wr["items"] or 0),
                    "avg_cart": round(rev / tx, 2) if tx > 0 else 0.0,
                    "store_revenue": round(float(wr["store_revenue"] or 0), 2),
                    "net_revenue": round(float(wr["net_revenue"] or 0), 2),
                })

            # İptal edilen satış özeti (toplama DAHİL EDİLMEZ, sadece bilgi amaçlı)
            canc_q = "SELECT COUNT(*) as cnt, COALESCE(SUM(total_amount), 0) as amt FROM sales WHERE (user_id = ? OR user_id IS NULL) AND status = 'cancelled'"
            canc_p: list = [uid]
            if start_date:
                canc_q += " AND replace(sold_at, 'T', ' ') >= ?"
                canc_p.append(start_date + " 00:00:00")
            if end_date:
                canc_q += " AND replace(sold_at, 'T', ' ') <= ?"
                canc_p.append(end_date + " 23:59:59")
            canc_row = conn.execute(canc_q, canc_p).fetchone()

            return {
                "total_revenue": round(total_rev, 2),
                "total_items_sold": total_items,
                "total_transactions": total_tx,
                "average_cart": round(avg_cart, 2),
                "total_expenses": round(total_exp, 2),
                "net_profit": round(net_profit, 2),
                "top_products": top_products,
                "low_stock_items": low_stock_items,
                "channel_stats": channel_stats,
                "monthly_stats": monthly_stats,
                "weekly_stats": weekly_stats,
                "cancelled_sales_count": int(canc_row["cnt"] if canc_row else 0),
                "cancelled_sales_amount": round(float(canc_row["amt"] if canc_row else 0.0), 2),
            }

    # ════════════════════════════════════════════════════════════════════
    # GİDER VE FATURA İŞLEMLERİ (KULLANICIYA ÖZEL İZOLE)
    # ════════════════════════════════════════════════════════════════════
    def add_expense(
        self,
        title: str,
        amount: float,
        category: str = "Fatura",
        expense_date: Optional[str] = None,
        note: str = "",
        user_id: Optional[int] = None,
        actor: Optional[Dict[str, Any]] = None,
        payment_method: str = "nakit",
    ) -> tuple[bool, str, Optional[Dict[str, Any]]]:
        t_clean = title.strip()
        if not t_clean or amount <= 0:
            return False, "Lütfen geçerli bir gider adı ve 0'dan büyük tutar giriniz!", None

        uid = user_id or 1
        exp_date = expense_date or get_turkey_now_str("%Y-%m-%d")
        now = get_turkey_now_str()
        pm_raw = str(payment_method or "nakit").strip().lower()
        pm = pm_raw if pm_raw in ("nakit", "kart", "havale") else "nakit"
        act = actor or {"name": "Patron", "role": "patron", "user_id": uid}
        created_by = str(act.get("name") or "Patron")
        created_by_role = normalize_role(act.get("role"))

        with self.db.get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO expenses (
                    user_id, title, amount, category, payment_method, expense_date, note,
                    status, created_by, created_by_role, synced_to_cloud, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, 0, ?)
                """,
                (uid, t_clean, amount, category.strip(), pm, exp_date, note.strip(), created_by, created_by_role, now),
            )
            eid = cursor.lastrowid
            log_audit(
                action="EXPENSE_CREATE",
                entity_type="expense",
                entity_id=eid,
                old_value=None,
                new_value={
                    "id": eid,
                    "title": t_clean,
                    "amount": amount,
                    "category": category.strip(),
                    "payment_method": pm,
                    "expense_date": exp_date,
                    "note": note.strip(),
                    "status": "active",
                },
                actor=act,
                user_id=uid,
                conn=conn,
            )

        exp_data = {
            "id": eid,
            "user_id": uid,
            "title": t_clean,
            "amount": amount,
            "category": category,
            "payment_method": pm,
            "expense_date": exp_date,
            "note": note,
            "status": "active",
            "created_by": created_by,
            "created_by_role": created_by_role,
            "created_at": now,
        }

        synced = 0
        if self.firestore_db:
            try:
                doc_id = f"u{uid}_e{eid}"
                exp_data["synced_to_cloud"] = 1
                self.firestore_db.collection("expenses").document(doc_id).set(exp_data)
                synced = 1
            except Exception:
                synced = 0

        with self.db.get_connection() as conn:
            conn.execute("UPDATE expenses SET synced_to_cloud = ? WHERE id = ?", (synced, eid))

        exp_data["synced_to_cloud"] = synced
        return True, f"'{t_clean}' ({amount:.2f} ₺) gideri başarıyla eklendi.", exp_data

    def cancel_expense(
        self,
        expense_id: int,
        cancel_reason: str,
        cancel_note: str = "",
        user_id: Optional[int] = None,
        actor: Optional[Dict[str, Any]] = None,
    ) -> tuple[bool, str]:
        """Gider kaydını fiziksel silmek yerine iptal edildi (cancelled) durumuna alır ve audit log yazar."""
        ok_r, msg_r, clean_reason, clean_note = validate_cancel_reason(cancel_reason, cancel_note)
        if not ok_r:
            return False, msg_r

        uid = self._resolve_user_id(user_id)
        now = get_turkey_now_str()
        act = actor or {"name": "Patron", "role": "patron", "user_id": uid}
        actor_name = str(act.get("name") or "Patron")
        actor_role = normalize_role(act.get("role"))

        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM expenses WHERE id = ? AND (user_id = ? OR user_id IS NULL)",
                (expense_id, uid),
            ).fetchone()
            if not row:
                return False, "Gider kaydı bulunamadı!"
            if str(row["status"] or "active").lower() == "cancelled":
                return False, "Bu gider zaten iptal edilmiş!"

            old_val = {
                "id": row["id"],
                "title": row["title"],
                "amount": row["amount"],
                "category": row["category"],
                "expense_date": row["expense_date"],
                "status": row["status"] or "active",
            }
            conn.execute(
                """
                UPDATE expenses
                SET status = 'cancelled',
                    cancelled_at = ?,
                    cancelled_by = ?,
                    cancelled_by_role = ?,
                    cancel_reason = ?,
                    cancel_note = ?,
                    synced_to_cloud = 0
                WHERE id = ?
                """,
                (now, actor_name, actor_role, clean_reason, clean_note, expense_id),
            )
            log_audit(
                action="EXPENSE_CANCEL",
                entity_type="expense",
                entity_id=expense_id,
                old_value=old_val,
                new_value={
                    "status": "cancelled",
                    "cancelled_at": now,
                    "cancelled_by": actor_name,
                    "cancelled_by_role": actor_role,
                    "cancel_reason": clean_reason,
                    "cancel_note": clean_note,
                },
                reason=clean_reason + (f" — {clean_note}" if clean_note else ""),
                actor=act,
                user_id=uid,
                conn=conn,
            )

        if self.firestore_db:
            try:
                self.firestore_db.collection("expenses").document(f"u{uid}_e{expense_id}").set(
                    {
                        "status": "cancelled",
                        "cancelled_at": now,
                        "cancelled_by": actor_name,
                        "cancel_reason": clean_reason,
                        "cancel_note": clean_note,
                    },
                    merge=True,
                )
            except Exception:
                pass

        return True, "Gider kaydı iptal edildi."

    # ════════════════════════════════════════════════════════════════════
    # GÜN AÇILIŞI VE KASA YÖNETİMİ (CASH DAYS + BEKLENEN NAKİT)
    # ════════════════════════════════════════════════════════════════════
    def get_cash_day(
        self,
        business_date: Optional[str] = None,
        user_id: Optional[Union[int, str]] = None,
    ) -> Optional[Dict[str, Any]]:
        uid = self._resolve_user_id(user_id)
        b_date = (business_date or get_turkey_now_str("%Y-%m-%d")).strip()[:10]
        with self.db.get_connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM cash_days
                WHERE business_date = ? AND (user_id = ? OR user_id IS NULL)
                ORDER BY id DESC LIMIT 1
                """,
                (b_date, uid),
            ).fetchone()
            return dict(row) if row else None

    def open_cash_day(
        self,
        opening_amount: float,
        *,
        business_date: Optional[str] = None,
        opening_note: str = "",
        user_id: Optional[int] = None,
        actor: Optional[Dict[str, Any]] = None,
    ) -> tuple[bool, str, Optional[Dict[str, Any]]]:
        """Belirtilen gün için açılış kasasını kaydeder, kim/saat bilgisini tutar ve audit log yazar."""
        if opening_amount < 0:
            return False, "Açılış kasası negatif olamaz!", None

        uid = self._resolve_user_id(user_id)
        b_date = (business_date or get_turkey_now_str("%Y-%m-%d")).strip()[:10]
        now = get_turkey_now_str()
        act = actor or {"name": "Patron", "role": "patron", "user_id": uid}
        opened_by = str(act.get("name") or "Patron")
        opened_by_role = normalize_role(act.get("role"))
        clean_amt = round(float(opening_amount), 2)
        clean_note = str(opening_note or "").strip()

        with self.db.get_connection() as conn:
            existing = conn.execute(
                "SELECT * FROM cash_days WHERE business_date = ? AND (user_id = ? OR user_id IS NULL)",
                (b_date, uid),
            ).fetchone()
            if existing:
                if existing["status"] == "open":
                    return False, f"{b_date} günü zaten açılmış (Açılış kasası: {float(existing['opening_amount']):.2f} ₺).", dict(existing)
                return False, f"{b_date} günü kapatılmış; yeniden açmak için Patron yetkisi gerekir.", dict(existing)

            cur = conn.execute(
                """
                INSERT INTO cash_days (
                    user_id, business_date, opening_amount, opened_at,
                    opened_by, opened_by_role, opening_note, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'open')
                """,
                (uid, b_date, clean_amt, now, opened_by, opened_by_role, clean_note),
            )
            day_id = cur.lastrowid
            row = conn.execute("SELECT * FROM cash_days WHERE id = ?", (day_id,)).fetchone()
            day_dict = dict(row)

            log_audit(
                action="CASH_DAY_OPEN",
                entity_type="cash_day",
                entity_id=day_id,
                old_value=None,
                new_value={
                    "id": day_id,
                    "business_date": b_date,
                    "field": "Sabah Kasa",
                    "opening_amount": clean_amt,
                    "amount": clean_amt,
                    "opened_at": now,
                    "opened_by": opened_by,
                    "opened_by_role": opened_by_role,
                    "opening_note": clean_note,
                },
                reason=clean_note or f"{b_date} sabah kasa açılışı",
                actor=act,
                user_id=uid,
                conn=conn,
            )

        return True, f"Gün açıldı! Sabah kasa: {clean_amt:.2f} ₺", day_dict

    def update_cash_day_values(
        self,
        *,
        business_date: Optional[str] = None,
        opening_amount: Optional[float] = None,
        counted_cash: Optional[float] = None,
        entered_pos_total: Optional[float] = None,
        new_expenses: Optional[List[Dict[str, Any]]] = None,
        reason: str = "",
        user_id: Optional[int] = None,
        actor: Optional[Dict[str, Any]] = None,
    ) -> tuple[bool, str, Optional[Dict[str, Any]]]:
        """Sabah kasa, akşam kasa (sayılan nakit) ve POS değerlerini günceller; her değişikliği saat + eski/yeni tutar ile audit_logs'a yazar."""
        uid = self._resolve_user_id(user_id)
        b_date = (business_date or get_turkey_now_str("%Y-%m-%d")).strip()[:10]
        act = actor or {"name": "Patron", "role": "patron", "user_id": uid}
        clean_reason = str(reason or "").strip()

        day = self.get_cash_day(business_date=b_date, user_id=uid)
        if day and str(day.get("status") or "").lower() == "closed":
            return False, f"🔒 {b_date} günü kapatılmıştır! Değişiklik için Patron günü yeniden açmalıdır.", None

        if not day:
            # Henüz gün açılmadıysa sabah kasası ile aç
            init_open = float(opening_amount) if opening_amount is not None else 0.0
            if init_open < 0:
                return False, "Sabah kasa tutarı negatif olamaz!", None
            ok_o, msg_o, day = self.open_cash_day(
                opening_amount=init_open,
                business_date=b_date,
                opening_note=clean_reason,
                user_id=uid,
                actor=act,
            )
            if not ok_o:
                return False, msg_o, None
            opening_amount = None  # zaten ilk giriş olarak loglandı

        # Yeni gider kalemlerini kaydet (varsa)
        for item in (new_expenses or []):
            t = str(item.get("title") or item.get("description") or "").strip()
            try:
                amt = float(item.get("amount") or 0.0)
            except (ValueError, TypeError):
                return False, "Gider tutarı geçersiz!", None
            if not t or amt <= 0:
                return False, "Her gider kalemi için geçerli açıklama ve tutar zorunludur!", None
            pm = str(item.get("payment_method") or "nakit").strip().lower()
            if pm not in ("nakit", "kart"):
                pm = "nakit"
            ok_e, msg_e, _ = self.add_expense(
                title=t,
                amount=round(amt, 2),
                category=str(item.get("category") or "Gün İçi Gider"),
                expense_date=b_date,
                note="POS / Gün Sonu gideri",
                user_id=uid,
                actor=act,
                payment_method=pm,
            )
            if not ok_e:
                return False, msg_e, None

        changes_made = 0
        now = get_turkey_now_str()

        with self.db.get_connection() as conn:
            fresh = conn.execute("SELECT * FROM cash_days WHERE id = ?", (day["id"],)).fetchone()
            if not fresh:
                return False, "Kasa günü bulunamadı!", None

            # 1. Sabah kasa değişikliği
            if opening_amount is not None:
                new_open = round(float(opening_amount), 2)
                if new_open < 0:
                    return False, "Sabah kasa tutarı negatif olamaz!", None
                old_open = round(float(fresh["opening_amount"] or 0.0), 2)
                if abs(new_open - old_open) >= 0.005:
                    conn.execute("UPDATE cash_days SET opening_amount = ? WHERE id = ?", (new_open, day["id"]))
                    log_audit(
                        action="MORNING_CASH_UPDATE",
                        entity_type="cash_day",
                        entity_id=day["id"],
                        old_value={"business_date": b_date, "field": "Sabah Kasa", "amount": old_open},
                        new_value={"business_date": b_date, "field": "Sabah Kasa", "amount": new_open, "updated_at": now},
                        reason=clean_reason or f"Sabah kasa güncellendi ({old_open:.2f} ₺ → {new_open:.2f} ₺)",
                        actor=act,
                        user_id=uid,
                        conn=conn,
                    )
                    changes_made += 1

            # 2. Akşam kasa (sayılan nakit) girişi / değişikliği
            if counted_cash is not None:
                new_cc = round(float(counted_cash), 2)
                if new_cc < 0:
                    return False, "Akşam kasa (sayılan nakit) tutarı negatif olamaz!", None
                old_cc_raw = fresh["counted_cash"]
                old_cc = round(float(old_cc_raw), 2) if old_cc_raw is not None else None
                if old_cc is None or abs(new_cc - old_cc) >= 0.005:
                    conn.execute("UPDATE cash_days SET counted_cash = ?, closing_amount = ? WHERE id = ?", (new_cc, new_cc, day["id"]))
                    act_code = "EVENING_CASH_SET" if old_cc is None else "EVENING_CASH_UPDATE"
                    log_audit(
                        action=act_code,
                        entity_type="cash_day",
                        entity_id=day["id"],
                        old_value={"business_date": b_date, "field": "Akşam Kasa", "amount": old_cc} if old_cc is not None else None,
                        new_value={"business_date": b_date, "field": "Akşam Kasa", "amount": new_cc, "updated_at": now},
                        reason=clean_reason or (
                            f"Akşam kasa ilk girişi: {new_cc:.2f} ₺"
                            if old_cc is None
                            else f"Akşam kasa güncellendi ({old_cc:.2f} ₺ → {new_cc:.2f} ₺)"
                        ),
                        actor=act,
                        user_id=uid,
                        conn=conn,
                    )
                    changes_made += 1

            # 3. Akşam POS toplamı girişi / değişikliği
            if entered_pos_total is not None:
                new_pos = round(float(entered_pos_total), 2)
                if new_pos < 0:
                    return False, "POS tutarı negatif olamaz!", None
                old_pos_raw = fresh["entered_pos_total"]
                old_pos = round(float(old_pos_raw), 2) if old_pos_raw is not None else None
                if old_pos is None or abs(new_pos - old_pos) >= 0.005:
                    conn.execute("UPDATE cash_days SET entered_pos_total = ? WHERE id = ?", (new_pos, day["id"]))
                    act_code = "EVENING_POS_SET" if old_pos is None else "EVENING_POS_UPDATE"
                    log_audit(
                        action=act_code,
                        entity_type="cash_day",
                        entity_id=day["id"],
                        old_value={"business_date": b_date, "field": "Akşam POS", "amount": old_pos} if old_pos is not None else None,
                        new_value={"business_date": b_date, "field": "Akşam POS", "amount": new_pos, "updated_at": now},
                        reason=clean_reason or (
                            f"Akşam POS ilk girişi: {new_pos:.2f} ₺"
                            if old_pos is None
                            else f"Akşam POS güncellendi ({old_pos:.2f} ₺ → {new_pos:.2f} ₺)"
                        ),
                        actor=act,
                        user_id=uid,
                        conn=conn,
                    )
                    changes_made += 1

        summary = self.get_cash_summary(business_date=b_date, user_id=uid)
        msg = "Kasa değerleri kaydedildi ve saatli geçmişe işlendi." if (changes_made > 0 or new_expenses) else "Değişiklik yapılmadı."
        return True, msg, summary

    def get_cash_day_value_history(
        self,
        business_date: Optional[str] = None,
        user_id: Optional[Union[int, str]] = None,
    ) -> List[Dict[str, Any]]:
        """Belirtilen gün için sabah kasa, akşam kasa ve POS tutarlarının saatli değişiklik geçmişini döner."""
        uid = self._resolve_user_id(user_id)
        b_date = (business_date or get_turkey_now_str("%Y-%m-%d")).strip()[:10]
        day = self.get_cash_day(business_date=b_date, user_id=uid)
        if not day:
            return []

        target_actions = (
            "CASH_DAY_OPEN",
            "MORNING_CASH_UPDATE",
            "EVENING_CASH_SET",
            "EVENING_CASH_UPDATE",
            "EVENING_POS_SET",
            "EVENING_POS_UPDATE",
            "CASH_DAY_CLOSE",
            "DAY_REOPEN",
        )
        placeholders = ",".join("?" for _ in target_actions)
        with self.db.get_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM audit_logs
                WHERE entity_type = 'cash_day'
                  AND entity_id = ?
                  AND action IN ({placeholders})
                ORDER BY id ASC
                """,
                (str(day["id"]), *target_actions),
            ).fetchall()

        history: List[Dict[str, Any]] = []
        field_labels = {
            "CASH_DAY_OPEN": "Sabah Kasa (Açılış)",
            "MORNING_CASH_UPDATE": "Sabah Kasa (Değişiklik)",
            "EVENING_CASH_SET": "Akşam Kasa (İlk Giriş)",
            "EVENING_CASH_UPDATE": "Akşam Kasa (Değişiklik)",
            "EVENING_POS_SET": "Akşam POS (İlk Giriş)",
            "EVENING_POS_UPDATE": "Akşam POS (Değişiklik)",
            "CASH_DAY_CLOSE": "Gün Sonu Kapanış",
            "DAY_REOPEN": "Gün Yeniden Açıldı",
        }
        for r in rows:
            old_v = None
            new_v = None
            try:
                old_v = json.loads(r["old_value"]) if r["old_value"] else None
            except Exception:
                old_v = None
            try:
                new_v = json.loads(r["new_value"]) if r["new_value"] else None
            except Exception:
                new_v = None

            old_amt = None
            new_amt = None
            if isinstance(old_v, dict):
                old_amt = old_v.get("amount") if old_v.get("amount") is not None else old_v.get("opening_amount")
            if isinstance(new_v, dict):
                if r["action"] == "CASH_DAY_CLOSE":
                    new_amt = new_v.get("counted_cash")
                else:
                    new_amt = new_v.get("amount") if new_v.get("amount") is not None else new_v.get("opening_amount")

            created = str(r["created_at"] or "")
            time_str = created.split(" ")[1][:8] if " " in created else created
            history.append({
                "id": r["id"],
                "action": r["action"],
                "field_label": field_labels.get(r["action"], r["action"]),
                "old_amount": round(float(old_amt), 2) if old_amt is not None else None,
                "new_amount": round(float(new_amt), 2) if new_amt is not None else None,
                "actor_name": r["actor_name"],
                "actor_role": r["actor_role"],
                "reason": r["reason"] or "",
                "created_at": created,
                "time": time_str,
            })

        return history

    def is_cash_day_closed(
        self,
        business_date: Optional[str] = None,
        user_id: Optional[Union[int, str]] = None,
    ) -> bool:
        day = self.get_cash_day(business_date=business_date, user_id=user_id)
        return bool(day and str(day.get("status") or "").lower() == "closed")

    def get_cash_summary(
        self,
        business_date: Optional[str] = None,
        user_id: Optional[Union[int, str]] = None,
        counted_cash: Optional[float] = None,
        entered_pos_total: Optional[float] = None,
        entered_expenses_total: Optional[float] = None,
        pending_expenses: Optional[List[Dict[str, Any]]] = None,
        opening_amount_override: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Kasa özeti ve gün sonu mutabakat hesabı (genişletilebilir formül motoruyla)."""
        uid = self._resolve_user_id(user_id)
        b_date = (business_date or get_turkey_now_str("%Y-%m-%d")).strip()[:10]
        day = self.get_cash_day(business_date=b_date, user_id=uid)
        is_open = bool(day and day.get("status") == "open")
        is_closed = bool(day and day.get("status") == "closed")
        if opening_amount_override is not None and not is_closed:
            opening_amount = round(float(opening_amount_override), 2)
        else:
            opening_amount = round(float(day["opening_amount"]), 2) if day else 0.0

        start_ts = f"{b_date} 00:00:00"
        end_ts = f"{b_date} 23:59:59"

        with self.db.get_connection() as conn:
            # 1. Aktif satışlar (nakit, POS/kart, karma, veresiye, internet)
            sales_rows = conn.execute(
                """
                SELECT id, total_amount, sold_at, channel, COALESCE(payment_method, 'nakit') as payment_method,
                       COALESCE(paid_cash_amount, 0) as paid_cash_amount,
                       COALESCE(paid_pos_amount, 0) as paid_pos_amount,
                       customer_name, note, created_by
                FROM sales
                WHERE (user_id = ? OR user_id IS NULL)
                  AND COALESCE(status, 'active') != 'cancelled'
                  AND replace(sold_at, 'T', ' ') >= ?
                  AND replace(sold_at, 'T', ' ') <= ?
                ORDER BY replace(sold_at, 'T', ' ') DESC, id DESC
                """,
                (uid, start_ts, end_ts),
            ).fetchall()

            cash_sales = 0.0
            cash_sales_count = 0
            pos_sales = 0.0
            pos_sales_count = 0
            credit_sales = 0.0
            credit_sales_count = 0
            internet_sales = 0.0
            internet_sales_count = 0
            total_sales_count = len(sales_rows)
            for sr in sales_rows:
                amt = float(sr["total_amount"] or 0.0)
                pm = str(sr["payment_method"] or "nakit").lower()
                ch = str(sr["channel"] or "magaza").lower()
                p_cash = float(sr["paid_cash_amount"] or 0.0)
                p_pos = float(sr["paid_pos_amount"] or 0.0)
                if ch == "internet":
                    internet_sales += amt
                    internet_sales_count += 1
                elif pm == "veresiye":
                    # Tamamı veresiye: kasaya veya POS'a girmez, cari hesaba borç yazılır
                    credit_sales += amt
                    credit_sales_count += 1
                elif pm == "karma" or p_cash > 0 or p_pos > 0:
                    if p_cash > 0:
                        cash_sales += p_cash
                        cash_sales_count += 1
                    if p_pos > 0:
                        pos_sales += p_pos
                        pos_sales_count += 1
                    rem_credit = round(amt - (p_cash + p_pos), 2)
                    if rem_credit > 0.005:
                        credit_sales += rem_credit
                        credit_sales_count += 1
                elif pm == "nakit":
                    cash_sales += amt
                    cash_sales_count += 1
                else:
                    pos_sales += amt
                    pos_sales_count += 1

            # 2. Gün içi kasa hareketleri
            mov_rows = conn.execute(
                """
                SELECT * FROM cash_movements
                WHERE (user_id = ? OR user_id IS NULL)
                  AND replace(movement_date, 'T', ' ') >= ?
                  AND replace(movement_date, 'T', ' ') <= ?
                ORDER BY replace(movement_date, 'T', ' ') DESC, id DESC
                """,
                (uid, start_ts, end_ts),
            ).fetchall()
            movements = [dict(r) for r in mov_rows]

            # 3. Günün giderleri (nakit ve kart ayrı)
            exp_rows = conn.execute(
                """
                SELECT * FROM expenses
                WHERE (user_id = ? OR user_id IS NULL)
                  AND expense_date = ?
                ORDER BY id DESC
                """,
                (uid, b_date),
            ).fetchall()
            expenses = [dict(r) for r in exp_rows]

            # 4. Günün veresiye tahsilatları (nakit -> Beklenen Nakit, POS -> Beklenen POS)
            col_rows = conn.execute(
                """
                SELECT payment_method, amount_kurus FROM credit_transactions
                WHERE (user_id = ? OR user_id IS NULL)
                  AND tx_type = 'tahsilat'
                  AND COALESCE(status, 'active') != 'cancelled'
                  AND replace(tx_date, 'T', ' ') >= ?
                  AND replace(tx_date, 'T', ' ') <= ?
                """,
                (uid, start_ts, end_ts),
            ).fetchall()

        other_cash_in = 0.0
        other_cash_out = 0.0
        patron_withdrawals = 0.0
        credit_collections_cash_k = 0
        credit_collections_pos_k = 0
        for cr in col_rows:
            cpm = str(cr["payment_method"] or "nakit").lower()
            ak = int(cr["amount_kurus"] or 0)
            if cpm == "nakit":
                credit_collections_cash_k += ak
            else:
                credit_collections_pos_k += ak

        credit_collections_cash = from_kurus(credit_collections_cash_k)
        credit_collections_pos = from_kurus(credit_collections_pos_k)

        for m in movements:
            if str(m.get("status") or "active").lower() == "cancelled":
                continue
            amt = float(m.get("amount") or 0.0)
            mt = str(m.get("movement_type") or "")
            cat = str(m.get("category") or "genel")
            if mt == "tahsilat":
                credit_collections_cash = round(credit_collections_cash + amt, 2)
            elif mt == "kasa_giris":
                other_cash_in += amt
            elif mt == "kasa_cikis":
                if cat == "patron_cekis":
                    patron_withdrawals += amt
                else:
                    other_cash_out += amt

        cash_expenses = 0.0
        card_expenses = 0.0
        for ex in expenses:
            if str(ex.get("status") or "active").lower() == "cancelled":
                continue
            amt = float(ex.get("amount") or 0.0)
            pm = str(ex.get("payment_method") or "nakit").lower()
            if pm == "nakit":
                cash_expenses += amt
            else:
                card_expenses += amt

        # Önizlemede henüz kaydedilmemiş kalem kalem giderler varsa ekle
        if pending_expenses:
            for pe in pending_expenses:
                try:
                    p_amt = float(pe.get("amount") or 0.0)
                except (ValueError, TypeError):
                    p_amt = 0.0
                if p_amt <= 0:
                    continue
                p_pm = str(pe.get("payment_method") or "nakit").lower()
                if p_pm == "nakit":
                    cash_expenses += p_amt
                else:
                    card_expenses += p_amt

        eff_counted = counted_cash if counted_cash is not None else (day.get("counted_cash") if day else None)
        eff_pos = entered_pos_total if entered_pos_total is not None else (day.get("entered_pos_total") if day else None)
        eff_exp = entered_expenses_total if entered_expenses_total is not None else (day.get("entered_expenses_total") if is_closed and day else None)

        rec = calculate_day_reconciliation(
            opening_amount=opening_amount,
            cash_sales=cash_sales,
            pos_sales=pos_sales,
            internet_sales=internet_sales,
            cash_expenses=cash_expenses,
            card_expenses=card_expenses,
            patron_withdrawals=patron_withdrawals,
            other_cash_in=other_cash_in,
            other_cash_out=other_cash_out,
            credit_collections_cash=credit_collections_cash,
            credit_collections_pos=credit_collections_pos,
            counted_cash=eff_counted,
            entered_pos_total=eff_pos,
            entered_expenses_total=eff_exp,
        )

        cash_in = round(other_cash_in + credit_collections_cash, 2)
        cash_out = round(patron_withdrawals + other_cash_out, 2)
        card_sales = round(pos_sales + internet_sales, 2)
        credit_sales_r = round(credit_sales, 2)
        total_revenue = round(rec["cash_sales"] + rec["pos_sales"] + credit_sales_r + rec["internet_sales"], 2)

        return {
            "business_date": b_date,
            "is_open": is_open,
            "is_closed": is_closed,
            "day": day,
            "opening_amount": rec["opening_amount"],
            "cash_sales": rec["cash_sales"],
            "cash_sales_count": cash_sales_count,
            "pos_sales": rec["pos_sales"],
            "pos_sales_count": pos_sales_count,
            "credit_sales": credit_sales_r,
            "credit_sales_count": credit_sales_count,
            "internet_sales": rec["internet_sales"],
            "internet_sales_count": internet_sales_count,
            "total_revenue": total_revenue,
            "total_sales_count": total_sales_count,
            "card_sales": card_sales,
            "card_sales_count": pos_sales_count + internet_sales_count,
            "cash_expenses": rec["cash_expenses"],
            "card_expenses": rec["card_expenses"],
            "total_expenses": rec["total_expenses"],
            "cash_in": cash_in,
            "cash_out": cash_out,
            "other_cash_in": rec["other_cash_in"],
            "other_cash_out": rec["other_cash_out"],
            "patron_withdrawals": rec["patron_withdrawals"],
            "credit_collections_cash": rec["credit_collections_cash"],
            "credit_collections_pos": rec["credit_collections_pos"],
            "expected_cash": rec["expected_cash"],
            "expected_pos": rec["expected_pos"],
            "cash_formula_items": rec["cash_formula_items"],
            "pos_formula_items": rec["pos_formula_items"],
            "comparison": rec["comparison"],
            "has_difference": rec["has_difference"],
            "movements": movements,
            "expenses": expenses,
            "value_history": self.get_cash_day_value_history(business_date=b_date, user_id=uid),
        }

    def close_cash_day(
        self,
        *,
        counted_cash: float,
        entered_pos_total: float,
        business_date: Optional[str] = None,
        closing_note: str = "",
        new_expenses: Optional[List[Dict[str, Any]]] = None,
        entered_expenses_total: Optional[float] = None,
        user_id: Optional[int] = None,
        actor: Optional[Dict[str, Any]] = None,
    ) -> tuple[bool, str, Optional[Dict[str, Any]]]:
        """Gün sonu kapanışını ve mutabakatı kaydeder, günü kilitler ve audit log yazar."""
        if counted_cash < 0 or entered_pos_total < 0:
            return False, "Sayılan nakit ve POS toplamı negatif olamaz!", None

        uid = self._resolve_user_id(user_id)
        b_date = (business_date or get_turkey_now_str("%Y-%m-%d")).strip()[:10]
        day = self.get_cash_day(business_date=b_date, user_id=uid)
        if not day:
            return False, f"{b_date} günü için henüz açılış kasası girilmemiş!", None
        if str(day.get("status") or "").lower() == "closed":
            return False, f"{b_date} günü zaten kapatılmış! Değişiklik için Patron'un günü yeniden açması gerekir.", None

        act = actor or {"name": "Patron", "role": "patron", "user_id": uid}
        closed_by = str(act.get("name") or "Patron")
        closed_by_role = normalize_role(act.get("role"))
        clean_note = str(closing_note or "").strip()

        # Önce yeni kalem giderlerin doğrulamasını yap (kayıttan önce önizleme ile fark kontrolü)
        cleaned_new_expenses: List[Dict[str, Any]] = []
        for item in (new_expenses or []):
            t = str(item.get("title") or item.get("description") or "").strip()
            try:
                amt = float(item.get("amount") or 0.0)
            except (ValueError, TypeError):
                return False, "Gider tutarı geçersiz!", None
            if not t:
                return False, "Her gider kalemi için açıklama zorunludur!", None
            if amt <= 0:
                return False, f"'{t}' gider tutarı 0'dan büyük olmalıdır!", None
            pm = str(item.get("payment_method") or "nakit").strip().lower()
            if pm not in ("nakit", "kart"):
                pm = "nakit"
            cat = str(item.get("category") or "Gün Sonu Gider").strip() or "Gün Sonu Gider"
            cleaned_new_expenses.append({"title": t, "amount": round(amt, 2), "payment_method": pm, "category": cat})

        # Fark kontrolü: Fark varsa açıklama notu zorunlu!
        preview = self.get_cash_summary(
            business_date=b_date,
            user_id=uid,
            counted_cash=counted_cash,
            entered_pos_total=entered_pos_total,
            entered_expenses_total=entered_expenses_total,
            pending_expenses=cleaned_new_expenses,
        )
        if preview["has_difference"] and len(clean_note) < 3:
            return False, "⚠️ Kasa, POS veya gider mutabakatında fark bulunduğu için açıklama notu girilmesi zorunludur!", preview

        # Kalem kalem giderleri kaydet
        for ex in cleaned_new_expenses:
            ok_e, msg_e, _ = self.add_expense(
                title=ex["title"],
                amount=ex["amount"],
                category=ex["category"],
                expense_date=b_date,
                note="Gün sonu kapanış gideri",
                user_id=uid,
                actor=act,
                payment_method=ex["payment_method"],
            )
            if not ok_e:
                return False, msg_e, None

        # Nihai mutabakatı hesapla
        final_summary = self.get_cash_summary(
            business_date=b_date,
            user_id=uid,
            counted_cash=counted_cash,
            entered_pos_total=entered_pos_total,
            entered_expenses_total=entered_expenses_total,
        )
        comp = final_summary["comparison"]
        now = get_turkey_now_str()

        with self.db.get_connection() as conn:
            conn.execute(
                """
                UPDATE cash_days
                SET status = 'closed',
                    closed_at = ?,
                    closed_by = ?,
                    closed_by_role = ?,
                    closing_amount = ?,
                    expected_cash = ?,
                    expected_pos = ?,
                    expected_expenses = ?,
                    counted_cash = ?,
                    entered_pos_total = ?,
                    entered_expenses_total = ?,
                    cash_diff = ?,
                    pos_diff = ?,
                    expense_diff = ?,
                    closing_note = ?
                WHERE id = ?
                """,
                (
                    now,
                    closed_by,
                    closed_by_role,
                    round(float(counted_cash), 2),
                    comp["cash"]["system"],
                    comp["pos"]["system"],
                    comp["expenses"]["system"],
                    comp["cash"]["entered"],
                    comp["pos"]["entered"],
                    comp["expenses"]["entered"],
                    comp["cash"]["diff"],
                    comp["pos"]["diff"],
                    comp["expenses"]["diff"],
                    clean_note,
                    day["id"],
                ),
            )
            log_audit(
                action="CASH_DAY_CLOSE",
                entity_type="cash_day",
                entity_id=day["id"],
                old_value={"status": "open", "business_date": b_date, "opening_amount": day["opening_amount"]},
                new_value={
                    "status": "closed",
                    "business_date": b_date,
                    "closed_at": now,
                    "closed_by": closed_by,
                    "expected_cash": comp["cash"]["system"],
                    "counted_cash": comp["cash"]["entered"],
                    "cash_diff": comp["cash"]["diff"],
                    "expected_pos": comp["pos"]["system"],
                    "entered_pos_total": comp["pos"]["entered"],
                    "pos_diff": comp["pos"]["diff"],
                    "expected_expenses": comp["expenses"]["system"],
                    "entered_expenses_total": comp["expenses"]["entered"],
                    "expense_diff": comp["expenses"]["diff"],
                    "closing_note": clean_note,
                },
                reason=clean_note or f"{b_date} gün sonu kapanışı (Tutuyor)",
                actor=act,
                user_id=uid,
                conn=conn,
            )

            if final_summary["has_difference"]:
                cashier_name = resolve_day_cashier({
                    "opened_by": day.get("opened_by"),
                    "opened_by_role": day.get("opened_by_role"),
                    "closed_by": closed_by,
                    "closed_by_role": closed_by_role,
                })
                log_audit(
                    action="DISCREPANCY_ALERT",
                    entity_type="cash_day",
                    entity_id=day["id"],
                    old_value=None,
                    new_value={
                        "business_date": b_date,
                        "cashier_name": cashier_name,
                        "cash_diff": comp["cash"]["diff"],
                        "pos_diff": comp["pos"]["diff"],
                        "expense_diff": comp["expenses"]["diff"],
                        "closing_note": clean_note,
                    },
                    reason=f"Gün sonu fark uyarısı ({cashier_name}): {clean_note}",
                    actor=act,
                    user_id=uid,
                    conn=conn,
                )

        updated = self.get_cash_summary(business_date=b_date, user_id=uid)
        if final_summary["has_difference"]:
            disc_report = self.get_discrepancy_report(user_id=uid)
            cashier_name = resolve_day_cashier(updated.get("day") or {})
            matched_alert = next(
                (a for a in disc_report.get("repeat_alerts", []) if a.get("cashier_name") == cashier_name),
                None,
            )
            if matched_alert:
                updated["repeat_alert"] = matched_alert
                log_audit(
                    action="REPEATED_DISCREPANCY_ALERT",
                    entity_type="cash_day",
                    entity_id=day["id"],
                    old_value=None,
                    new_value=matched_alert,
                    reason=matched_alert["message"],
                    actor=act,
                    user_id=uid,
                    db=self.db,
                )

        return True, f"{b_date} günü başarıyla kapatıldı ve kilitlendi.", updated

    def get_discrepancy_report(
        self,
        user_id: Optional[Union[int, str]] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        cashier_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Patron için gün sonu fark geçmişi, çalışan bazında toplam/aylık rapor ve 3 gün tekrar uyarıları."""
        uid = self._resolve_user_id(user_id)
        q = "SELECT * FROM cash_days WHERE (user_id = ? OR user_id IS NULL) AND status = 'closed'"
        params: list = [uid]
        if start_date and str(start_date).strip():
            q += " AND business_date >= ?"
            params.append(str(start_date).strip()[:10])
        if end_date and str(end_date).strip():
            q += " AND business_date <= ?"
            params.append(str(end_date).strip()[:10])
        q += " ORDER BY business_date ASC"

        with self.db.get_connection() as conn:
            rows = [dict(r) for r in conn.execute(q, params).fetchall()]

        if cashier_name and str(cashier_name).strip():
            target = str(cashier_name).strip().lower()
            rows = [r for r in rows if resolve_day_cashier(r).lower() == target]

        return analyze_discrepancy_history(rows, repeat_threshold=3)

    def reopen_cash_day(
        self,
        *,
        business_date: str,
        reason: str,
        user_id: Optional[int] = None,
        actor: Optional[Dict[str, Any]] = None,
    ) -> tuple[bool, str, Optional[Dict[str, Any]]]:
        """Kapanan günü sadece Patron yetkisiyle yeniden açar ve denetim kaydına işler."""
        clean_reason = str(reason or "").strip()
        if not clean_reason:
            return False, "Günü yeniden açmak için neden belirtilmesi zorunludur!", None

        uid = self._resolve_user_id(user_id)
        b_date = str(business_date or get_turkey_now_str("%Y-%m-%d")).strip()[:10]
        now = get_turkey_now_str()
        act = actor or {"name": "Patron", "role": "patron", "user_id": uid}
        reopened_by = str(act.get("name") or "Patron")

        day = self.get_cash_day(business_date=b_date, user_id=uid)
        with self.db.get_connection() as conn:
            if day:
                old_status = day.get("status") or "closed"
                conn.execute(
                    """
                    UPDATE cash_days
                    SET status = 'open',
                        reopened_at = ?,
                        reopened_by = ?,
                        reopen_reason = ?
                    WHERE id = ?
                    """,
                    (now, reopened_by, clean_reason, day["id"]),
                )
                entity_id: Any = day["id"]
            else:
                old_status = "none"
                entity_id = b_date

            log_audit(
                action="DAY_REOPEN",
                entity_type="cash_day",
                entity_id=entity_id,
                old_value={"status": old_status, "business_date": b_date},
                new_value={"status": "open", "business_date": b_date, "reopened_at": now, "reopened_by": reopened_by},
                reason=clean_reason,
                actor=act,
                user_id=uid,
                conn=conn,
            )

        return True, f"{b_date} günü Patron yetkisiyle yeniden açıldı ve denetim kaydına işlendi.", self.get_cash_day(b_date, uid)

    # ════════════════════════════════════════════════════════════════════
    # TAHSİLAT VE KASA HAREKETLERİ (SOFT-CANCEL + AUDIT LOG)
    # ════════════════════════════════════════════════════════════════════
    def add_cash_movement(
        self,
        movement_type: str,
        amount: float,
        description: str,
        *,
        category: str = "genel",
        customer_name: Optional[str] = None,
        movement_date: Optional[str] = None,
        note: str = "",
        user_id: Optional[int] = None,
        actor: Optional[Dict[str, Any]] = None,
    ) -> tuple[bool, str, Optional[Dict[str, Any]]]:
        m_type = str(movement_type or "").strip().lower()
        if m_type not in CASH_MOVEMENT_TYPES:
            return False, "Geçersiz hareket türü! (tahsilat, kasa_giris veya kasa_cikis olmalıdır)", None

        desc = str(description or "").strip()
        if not desc:
            return False, "Açıklama zorunludur!", None
        if amount <= 0:
            return False, "Tutar 0'dan büyük olmalıdır!", None

        uid = self._resolve_user_id(user_id)
        now = get_turkey_now_str()
        m_date = normalize_datetime_str(movement_date) if movement_date else now
        b_date = m_date[:10]
        day = self.get_cash_day(business_date=b_date, user_id=uid)
        cash_day_id = day["id"] if day else None
        clean_cat = str(category or "genel").strip().lower() or "genel"
        cust = str(customer_name or "").strip() or None
        act = actor or {"name": "Patron", "role": "patron", "user_id": uid}
        created_by = str(act.get("name") or "Patron")
        created_by_role = normalize_role(act.get("role"))
        clean_amt = round(float(amount), 2)

        with self.db.get_connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO cash_movements (
                    user_id, cash_day_id, movement_type, category, amount, description, customer_name,
                    movement_date, note, status, created_by, created_by_role, synced_to_cloud, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, 0, ?)
                """,
                (uid, cash_day_id, m_type, clean_cat, clean_amt, desc, cust, m_date, str(note or "").strip(), created_by, created_by_role, now),
            )
            mid = cur.lastrowid
            if m_type == "tahsilat":
                action_name = "COLLECTION_CREATE"
                entity_name = "collection"
            elif clean_cat == "patron_cekis":
                action_name = "PATRON_CASH_WITHDRAW"
                entity_name = "cash_movement"
            else:
                action_name = "CASH_MOVEMENT_CREATE"
                entity_name = "cash_movement"
            log_audit(
                action=action_name,
                entity_type=entity_name,
                entity_id=mid,
                old_value=None,
                new_value={
                    "id": mid,
                    "cash_day_id": cash_day_id,
                    "movement_type": m_type,
                    "category": clean_cat,
                    "amount": clean_amt,
                    "description": desc,
                    "customer_name": cust,
                    "movement_date": m_date,
                    "note": str(note or "").strip(),
                    "status": "active",
                },
                reason=desc,
                actor=act,
                user_id=uid,
                conn=conn,
            )

        rec = {
            "id": mid,
            "user_id": uid,
            "cash_day_id": cash_day_id,
            "movement_type": m_type,
            "category": clean_cat,
            "amount": clean_amt,
            "description": desc,
            "customer_name": cust,
            "movement_date": m_date,
            "note": str(note or "").strip(),
            "status": "active",
            "created_by": created_by,
            "created_by_role": created_by_role,
            "created_at": now,
        }
        return True, f"Kasa hareketi kaydedildi ({clean_amt:.2f} ₺).", rec

    def get_cash_movements(
        self,
        user_id: Optional[Union[int, str]] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        movement_type: Optional[str] = None,
        include_cancelled: bool = True,
    ) -> Dict[str, Any]:
        uid = self._resolve_user_id(user_id)
        query = "SELECT * FROM cash_movements WHERE (user_id = ? OR user_id IS NULL)"
        params: list = [uid]
        if not include_cancelled:
            query += " AND COALESCE(status, 'active') != 'cancelled'"
        if movement_type and str(movement_type).strip() in CASH_MOVEMENT_TYPES:
            query += " AND movement_type = ?"
            params.append(str(movement_type).strip())
        if start_date:
            query += " AND replace(movement_date, 'T', ' ') >= ?"
            params.append(start_date + " 00:00:00")
        if end_date:
            query += " AND replace(movement_date, 'T', ' ') <= ?"
            params.append(end_date + " 23:59:59")
        query += " ORDER BY replace(movement_date, 'T', ' ') DESC, id DESC LIMIT 200"

        with self.db.get_connection() as conn:
            rows = conn.execute(query, params).fetchall()
            items = [dict(r) for r in rows]

        total_tahsilat = round(
            sum(float(x["amount"] or 0) for x in items if x.get("status") != "cancelled" and x.get("movement_type") == "tahsilat"),
            2,
        )
        total_giris = round(
            sum(float(x["amount"] or 0) for x in items if x.get("status") != "cancelled" and x.get("movement_type") == "kasa_giris"),
            2,
        )
        total_cikis = round(
            sum(float(x["amount"] or 0) for x in items if x.get("status") != "cancelled" and x.get("movement_type") == "kasa_cikis"),
            2,
        )
        return {
            "movements": items,
            "summary": {
                "total_tahsilat": total_tahsilat,
                "total_kasa_giris": total_giris,
                "total_kasa_cikis": total_cikis,
                "net_cash": round(total_tahsilat + total_giris - total_cikis, 2),
            },
        }

    def cancel_cash_movement(
        self,
        movement_id: int,
        cancel_reason: str,
        cancel_note: str = "",
        user_id: Optional[int] = None,
        actor: Optional[Dict[str, Any]] = None,
    ) -> tuple[bool, str]:
        ok_r, msg_r, clean_reason, clean_note = validate_cancel_reason(cancel_reason, cancel_note)
        if not ok_r:
            return False, msg_r

        uid = self._resolve_user_id(user_id)
        now = get_turkey_now_str()
        act = actor or {"name": "Patron", "role": "patron", "user_id": uid}
        actor_name = str(act.get("name") or "Patron")
        actor_role = normalize_role(act.get("role"))

        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM cash_movements WHERE id = ? AND (user_id = ? OR user_id IS NULL)",
                (movement_id, uid),
            ).fetchone()
            if not row:
                return False, "Tahsilat / kasa hareketi bulunamadı!"
            if str(row["status"] or "active").lower() == "cancelled":
                return False, "Bu kayıt zaten iptal edilmiş!"

            old_val = {
                "id": row["id"],
                "movement_type": row["movement_type"],
                "amount": row["amount"],
                "description": row["description"],
                "status": row["status"] or "active",
            }
            conn.execute(
                """
                UPDATE cash_movements
                SET status = 'cancelled',
                    cancelled_at = ?,
                    cancelled_by = ?,
                    cancelled_by_role = ?,
                    cancel_reason = ?,
                    cancel_note = ?,
                    synced_to_cloud = 0
                WHERE id = ?
                """,
                (now, actor_name, actor_role, clean_reason, clean_note, movement_id),
            )
            action_name = "COLLECTION_CANCEL" if row["movement_type"] == "tahsilat" else "CASH_MOVEMENT_CANCEL"
            entity_name = "collection" if row["movement_type"] == "tahsilat" else "cash_movement"
            log_audit(
                action=action_name,
                entity_type=entity_name,
                entity_id=movement_id,
                old_value=old_val,
                new_value={
                    "status": "cancelled",
                    "cancelled_at": now,
                    "cancelled_by": actor_name,
                    "cancelled_by_role": actor_role,
                    "cancel_reason": clean_reason,
                    "cancel_note": clean_note,
                },
                reason=clean_reason + (f" — {clean_note}" if clean_note else ""),
                actor=act,
                user_id=uid,
                conn=conn,
            )

        return True, "Tahsilat / kasa hareketi iptal edildi."

    # ════════════════════════════════════════════════════════════════════
    # VERESİYE / CARİ HESAP YÖNETİMİ (BAKİYE HAREKETLERDEN KURUŞ OLARAK TÜRETİLİR)
    # ════════════════════════════════════════════════════════════════════
    def get_or_create_credit_customer(
        self,
        name: str,
        phone: str = "",
        note: str = "",
        user_id: Optional[int] = None,
        actor: Optional[Dict[str, Any]] = None,
    ) -> tuple[bool, str, Optional[Dict[str, Any]]]:
        clean_name = str(name or "").strip()
        if not clean_name:
            return False, "Müşteri adı boş olamaz!", None

        uid = self._resolve_user_id(user_id)
        now = get_turkey_now_str()
        clean_phone = str(phone or "").strip() or None
        clean_note = str(note or "").strip() or None

        with self.db.get_connection() as conn:
            existing = conn.execute(
                """
                SELECT * FROM credit_customers
                WHERE (user_id = ? OR user_id IS NULL) AND LOWER(name) = LOWER(?) AND is_active = 1
                LIMIT 1
                """,
                (uid, clean_name),
            ).fetchone()
            if existing:
                return True, f"'{existing['name']}' cari hesabı seçildi.", dict(existing)

            cur = conn.execute(
                """
                INSERT INTO credit_customers (user_id, name, phone, note, is_active, created_at)
                VALUES (?, ?, ?, ?, 1, ?)
                """,
                (uid, clean_name, clean_phone, clean_note, now),
            )
            cid = cur.lastrowid
            row = conn.execute("SELECT * FROM credit_customers WHERE id = ?", (cid,)).fetchone()
            log_audit(
                action="CREDIT_CUSTOMER_CREATE",
                entity_type="credit_customer",
                entity_id=cid,
                new_value={"id": cid, "name": clean_name, "phone": clean_phone},
                actor=actor,
                user_id=uid,
                conn=conn,
            )
            return True, f"'{clean_name}' cari hesabı oluşturuldu.", dict(row)

    def get_credit_customers(
        self,
        user_id: Optional[Union[int, str]] = None,
        search: str = "",
    ) -> Dict[str, Any]:
        """Tüm cari müşterileri hareketlerden türetilmiş borç bakiyesine göre (en çok borçlu başta) listeler."""
        uid = self._resolve_user_id(user_id)
        q = """
            SELECT
                c.id, c.name, c.phone, c.note, c.created_at,
                COALESCE(SUM(CASE WHEN COALESCE(t.status, 'active') != 'cancelled' THEN t.signed_kurus ELSE 0 END), 0) AS balance_kurus,
                COALESCE(SUM(CASE WHEN COALESCE(t.status, 'active') != 'cancelled' AND t.signed_kurus > 0 THEN t.amount_kurus ELSE 0 END), 0) AS total_debt_kurus,
                COALESCE(SUM(CASE WHEN COALESCE(t.status, 'active') != 'cancelled' AND t.signed_kurus < 0 THEN t.amount_kurus ELSE 0 END), 0) AS total_paid_kurus,
                MAX(CASE WHEN COALESCE(t.status, 'active') != 'cancelled' THEN t.tx_date ELSE NULL END) AS last_tx_date
            FROM credit_customers c
            LEFT JOIN credit_transactions t ON t.customer_id = c.id
            WHERE (c.user_id = ? OR c.user_id IS NULL) AND c.is_active = 1
        """
        params: list = [uid]
        if search and str(search).strip():
            q += " AND (LOWER(c.name) LIKE ? OR COALESCE(c.phone, '') LIKE ?)"
            s = f"%{str(search).strip().lower()}%"
            params.extend([s, s])
        q += " GROUP BY c.id ORDER BY balance_kurus DESC, c.name ASC"

        with self.db.get_connection() as conn:
            rows = conn.execute(q, params).fetchall()

        customers: List[Dict[str, Any]] = []
        total_receivable_k = 0
        total_customer_credit_k = 0
        debtor_count = 0

        for r in rows:
            bal_k = int(r["balance_kurus"] or 0)
            if bal_k > 0:
                total_receivable_k += bal_k
                debtor_count += 1
            elif bal_k < 0:
                total_customer_credit_k += abs(bal_k)
            customers.append({
                "id": r["id"],
                "name": r["name"],
                "phone": r["phone"] or "",
                "note": r["note"] or "",
                "created_at": r["created_at"],
                "last_tx_date": r["last_tx_date"],
                "balance_kurus": bal_k,
                "balance": from_kurus(bal_k),
                "total_debt": from_kurus(int(r["total_debt_kurus"] or 0)),
                "total_paid": from_kurus(int(r["total_paid_kurus"] or 0)),
                "balance_status": "borclu" if bal_k > 0 else ("alacakli" if bal_k < 0 else "kapali"),
            })

        return {
            "customers": customers,
            "summary": {
                "customer_count": len(customers),
                "debtor_count": debtor_count,
                "total_receivable": from_kurus(total_receivable_k),
                "total_customer_credit": from_kurus(total_customer_credit_k),
                "net_receivable": from_kurus(total_receivable_k - total_customer_credit_k),
            },
        }

    def get_credit_customer_detail(
        self,
        customer_id: int,
        user_id: Optional[Union[int, str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Müşteri detayı: kronolojik hareket dökümü ve her satırda yürüyen (türetilmiş) güncel bakiye."""
        uid = self._resolve_user_id(user_id)
        with self.db.get_connection() as conn:
            c_row = conn.execute(
                "SELECT * FROM credit_customers WHERE id = ? AND (user_id = ? OR user_id IS NULL)",
                (int(customer_id), uid),
            ).fetchone()
            if not c_row:
                return None

            tx_rows = conn.execute(
                """
                SELECT * FROM credit_transactions
                WHERE customer_id = ?
                ORDER BY replace(tx_date, 'T', ' ') ASC, id ASC
                """,
                (int(customer_id),),
            ).fetchall()

        running_k = 0
        total_debt_k = 0
        total_paid_k = 0
        transactions: List[Dict[str, Any]] = []

        for tr in tx_rows:
            is_canc = str(tr["status"] or "active").lower() == "cancelled"
            signed_k = int(tr["signed_kurus"] or 0)
            amt_k = int(tr["amount_kurus"] or 0)
            if not is_canc:
                running_k += signed_k
                if signed_k > 0:
                    total_debt_k += amt_k
                else:
                    total_paid_k += amt_k
            transactions.append({
                "id": tr["id"],
                "customer_id": tr["customer_id"],
                "sale_id": tr["sale_id"],
                "tx_type": tr["tx_type"],
                "payment_method": tr["payment_method"],
                "amount_kurus": amt_k,
                "amount": from_kurus(amt_k),
                "signed_kurus": signed_k,
                "signed_amount": from_kurus(signed_k),
                "description": tr["description"],
                "product_summary": tr["product_summary"] or "",
                "tx_date": tr["tx_date"],
                "status": tr["status"] or "active",
                "cancelled_at": tr["cancelled_at"],
                "cancelled_by": tr["cancelled_by"],
                "cancel_reason": tr["cancel_reason"],
                "cancel_note": tr["cancel_note"],
                "created_by": tr["created_by"] or "Patron",
                "running_balance_kurus": running_k,
                "running_balance": from_kurus(running_k),
            })

        # En yeni hareket üstte görünecek şekilde ters çevir (yürüyen bakiye doğru hesaplandı)
        transactions.reverse()

        return {
            "customer": {
                "id": c_row["id"],
                "name": c_row["name"],
                "phone": c_row["phone"] or "",
                "note": c_row["note"] or "",
                "balance_kurus": running_k,
                "balance": from_kurus(running_k),
                "total_debt": from_kurus(total_debt_k),
                "total_paid": from_kurus(total_paid_k),
                "balance_status": "borclu" if running_k > 0 else ("alacakli" if running_k < 0 else "kapali"),
            },
            "transactions": transactions,
        }

    def add_credit_collection(
        self,
        customer_id: int,
        amount: float,
        payment_method: str = "nakit",
        *,
        note: str = "",
        tx_date: Optional[str] = None,
        user_id: Optional[int] = None,
        actor: Optional[Dict[str, Any]] = None,
    ) -> tuple[bool, str, Optional[Dict[str, Any]]]:
        """Cari müşteriden sadece borç ödemesi (Tahsilat - Nakit veya POS) alır."""
        amt_k = to_kurus(amount)
        if amt_k <= 0:
            return False, "Tahsilat tutarı 0'dan büyük olmalıdır!", None

        pm_raw = str(payment_method or "nakit").strip().lower()
        if pm_raw == "pos":
            pm_raw = "kart"
        if pm_raw not in ("nakit", "kart"):
            return False, "Tahsilat ödeme tipi 'nakit' veya 'POS (kart)' olmalıdır!", None

        uid = self._resolve_user_id(user_id)
        now = normalize_datetime_str(tx_date) if tx_date else get_turkey_now_str()
        act = actor or {"name": "Patron", "role": "patron", "user_id": uid}
        created_by = str(act.get("name") or "Patron")
        created_by_role = normalize_role(act.get("role"))
        clean_note = str(note or "").strip()

        with self.db.get_connection() as conn:
            c_row = conn.execute(
                "SELECT * FROM credit_customers WHERE id = ? AND (user_id = ? OR user_id IS NULL)",
                (int(customer_id), uid),
            ).fetchone()
            if not c_row:
                return False, "Cari müşteri bulunamadı!", None

            pm_label = "Nakit" if pm_raw == "nakit" else "POS"
            desc = f"Tahsilat ({pm_label})" + (f" — {clean_note}" if clean_note else "")
            cur = conn.execute(
                """
                INSERT INTO credit_transactions (
                    user_id, customer_id, sale_id, tx_type, payment_method,
                    amount_kurus, signed_kurus, description, product_summary,
                    tx_date, status, created_by, created_by_role, created_at
                ) VALUES (?, ?, NULL, 'tahsilat', ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                """,
                (
                    uid, int(customer_id), pm_raw,
                    amt_k, -amt_k, desc, f"Tahsilat ({pm_label})",
                    now, created_by, created_by_role, now,
                ),
            )
            tx_id = cur.lastrowid

            bal_row = conn.execute(
                "SELECT COALESCE(SUM(signed_kurus), 0) AS bal FROM credit_transactions WHERE customer_id = ? AND COALESCE(status, 'active') != 'cancelled'",
                (int(customer_id),),
            ).fetchone()
            new_bal_k = int(bal_row["bal"] or 0)

            log_audit(
                action="CREDIT_COLLECTION_CREATE",
                entity_type="credit_collection",
                entity_id=tx_id,
                old_value={"customer_id": int(customer_id), "balance": from_kurus(new_bal_k + amt_k)},
                new_value={
                    "id": tx_id,
                    "customer_id": int(customer_id),
                    "customer_name": c_row["name"],
                    "amount": from_kurus(amt_k),
                    "payment_method": pm_raw,
                    "new_balance": from_kurus(new_bal_k),
                    "tx_date": now,
                },
                reason=clean_note or desc,
                actor=act,
                user_id=uid,
                conn=conn,
            )

        return True, f"'{c_row['name']}' müşterisinden {from_kurus(amt_k):.2f} ₺ ({pm_label}) tahsilat alındı.", {
            "id": tx_id,
            "customer_id": int(customer_id),
            "customer_name": c_row["name"],
            "amount": from_kurus(amt_k),
            "amount_kurus": amt_k,
            "payment_method": pm_raw,
            "tx_date": now,
            "new_balance": from_kurus(new_bal_k),
            "new_balance_kurus": new_bal_k,
        }

    def cancel_credit_collection(
        self,
        tx_id: int,
        cancel_reason: str,
        cancel_note: str = "",
        user_id: Optional[int] = None,
        actor: Optional[Dict[str, Any]] = None,
    ) -> tuple[bool, str]:
        """Tahsilat kaydını fiziksel silmek yerine zorunlu nedenle iptal eder ve bakiyeyi günceller."""
        ok_r, msg_r, clean_reason, clean_note = validate_cancel_reason(cancel_reason, cancel_note)
        if not ok_r:
            return False, msg_r

        uid = self._resolve_user_id(user_id)
        now = get_turkey_now_str()
        act = actor or {"name": "Patron", "role": "patron", "user_id": uid}
        actor_name = str(act.get("name") or "Patron")
        actor_role = normalize_role(act.get("role"))

        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM credit_transactions WHERE id = ? AND (user_id = ? OR user_id IS NULL)",
                (int(tx_id), uid),
            ).fetchone()
            if not row:
                return False, "Tahsilat hareketi bulunamadı!"
            if row["tx_type"] != "tahsilat":
                return False, "Satış kaynaklı cari hareketler ilgili satışın iptaliyle geri alınır!"
            if str(row["status"] or "active").lower() == "cancelled":
                return False, "Bu tahsilat zaten iptal edilmiş!"

            conn.execute(
                """
                UPDATE credit_transactions
                SET status = 'cancelled',
                    cancelled_at = ?,
                    cancelled_by = ?,
                    cancelled_by_role = ?,
                    cancel_reason = ?,
                    cancel_note = ?
                WHERE id = ?
                """,
                (now, actor_name, actor_role, clean_reason, clean_note, int(tx_id)),
            )
            log_audit(
                action="CREDIT_COLLECTION_CANCEL",
                entity_type="credit_collection",
                entity_id=int(tx_id),
                old_value={
                    "id": row["id"],
                    "customer_id": row["customer_id"],
                    "amount": from_kurus(int(row["amount_kurus"] or 0)),
                    "payment_method": row["payment_method"],
                    "status": "active",
                },
                new_value={
                    "status": "cancelled",
                    "cancelled_at": now,
                    "cancelled_by": actor_name,
                    "cancel_reason": clean_reason,
                    "cancel_note": clean_note,
                },
                reason=clean_reason + (f" — {clean_note}" if clean_note else ""),
                actor=act,
                user_id=uid,
                conn=conn,
            )

        return True, "Tahsilat kaydı iptal edildi ve müşteri bakiyesi güncellendi."

    def get_cancelled_report(
        self,
        user_id: Optional[Union[int, str]] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        entity_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Patron için tüm iptal edilen finansal kayıtların (satış, gider, tahsilat, kasa, çiftlik) raporu."""
        uid = self._resolve_user_id(user_id)
        records: List[Dict[str, Any]] = []
        filter_type = str(entity_type or "").strip().lower()

        with self.db.get_connection() as conn:
            if not filter_type or filter_type == "sale":
                q = "SELECT * FROM sales WHERE (user_id = ? OR user_id IS NULL) AND status = 'cancelled'"
                p: list = [uid]
                if start_date:
                    q += " AND COALESCE(cancelled_at, sold_at) >= ?"
                    p.append(start_date + " 00:00:00")
                if end_date:
                    q += " AND COALESCE(cancelled_at, sold_at) <= ?"
                    p.append(end_date + " 23:59:59")
                for r in conn.execute(q, p).fetchall():
                    records.append({
                        "entity_type": "sale",
                        "entity_label": "Satış",
                        "record_id": r["id"],
                        "amount": round(float(r["total_amount"] or 0), 2),
                        "title": r["customer_name"] or r["note"] or f"Fiş #{r['id']}",
                        "original_date": r["sold_at"],
                        "cancelled_at": r["cancelled_at"],
                        "cancelled_by": r["cancelled_by"] or "Patron",
                        "cancelled_by_role": r["cancelled_by_role"] or "patron",
                        "cancel_reason": r["cancel_reason"] or "",
                        "cancel_note": r["cancel_note"] or "",
                    })

            if not filter_type or filter_type == "expense":
                q = "SELECT * FROM expenses WHERE (user_id = ? OR user_id IS NULL) AND status = 'cancelled'"
                p = [uid]
                if start_date:
                    q += " AND COALESCE(cancelled_at, expense_date) >= ?"
                    p.append(start_date + " 00:00:00")
                if end_date:
                    q += " AND COALESCE(cancelled_at, expense_date) <= ?"
                    p.append(end_date + " 23:59:59")
                for r in conn.execute(q, p).fetchall():
                    records.append({
                        "entity_type": "expense",
                        "entity_label": "Gider",
                        "record_id": r["id"],
                        "amount": round(float(r["amount"] or 0), 2),
                        "title": f"{r['title']} ({r['category'] or 'Gider'})",
                        "original_date": r["expense_date"],
                        "cancelled_at": r["cancelled_at"],
                        "cancelled_by": r["cancelled_by"] or "Patron",
                        "cancelled_by_role": r["cancelled_by_role"] or "patron",
                        "cancel_reason": r["cancel_reason"] or "",
                        "cancel_note": r["cancel_note"] or "",
                    })

            if not filter_type or filter_type in ("cash_movement", "collection"):
                q = "SELECT * FROM cash_movements WHERE (user_id = ? OR user_id IS NULL) AND status = 'cancelled'"
                p = [uid]
                if start_date:
                    q += " AND COALESCE(cancelled_at, movement_date) >= ?"
                    p.append(start_date + " 00:00:00")
                if end_date:
                    q += " AND COALESCE(cancelled_at, movement_date) <= ?"
                    p.append(end_date + " 23:59:59")
                for r in conn.execute(q, p).fetchall():
                    et = "collection" if r["movement_type"] == "tahsilat" else "cash_movement"
                    if filter_type and filter_type != et:
                        continue
                    lbl = "Tahsilat" if r["movement_type"] == "tahsilat" else ("Kasa Giriş" if r["movement_type"] == "kasa_giris" else "Kasa Çıkış")
                    records.append({
                        "entity_type": et,
                        "entity_label": lbl,
                        "record_id": r["id"],
                        "amount": round(float(r["amount"] or 0), 2),
                        "title": r["description"],
                        "original_date": r["movement_date"],
                        "cancelled_at": r["cancelled_at"],
                        "cancelled_by": r["cancelled_by"] or "Patron",
                        "cancelled_by_role": r["cancelled_by_role"] or "patron",
                        "cancel_reason": r["cancel_reason"] or "",
                        "cancel_note": r["cancel_note"] or "",
                    })

            if not filter_type or filter_type == "farm_egg_sale":
                q = "SELECT * FROM farm_egg_sales WHERE (user_id = ? OR user_id IS NULL) AND status = 'cancelled'"
                p = [uid]
                if start_date:
                    q += " AND COALESCE(cancelled_at, sale_date) >= ?"
                    p.append(start_date + " 00:00:00")
                if end_date:
                    q += " AND COALESCE(cancelled_at, sale_date) <= ?"
                    p.append(end_date + " 23:59:59")
                for r in conn.execute(q, p).fetchall():
                    records.append({
                        "entity_type": "farm_egg_sale",
                        "entity_label": "Yumurta Satışı",
                        "record_id": r["id"],
                        "amount": round(float(r["total_amount"] or 0), 2),
                        "title": f"{r['customer_name']} ({r['source']})",
                        "original_date": r["sale_date"],
                        "cancelled_at": r["cancelled_at"],
                        "cancelled_by": r["cancelled_by"] or "Patron",
                        "cancelled_by_role": r["cancelled_by_role"] or "patron",
                        "cancel_reason": r["cancel_reason"] or "",
                        "cancel_note": r["cancel_note"] or "",
                    })

            if not filter_type or filter_type == "farm_feed_purchase":
                q = "SELECT * FROM farm_feed_purchases WHERE (user_id = ? OR user_id IS NULL) AND status = 'cancelled'"
                p = [uid]
                if start_date:
                    q += " AND COALESCE(cancelled_at, purchase_date) >= ?"
                    p.append(start_date + " 00:00:00")
                if end_date:
                    q += " AND COALESCE(cancelled_at, purchase_date) <= ?"
                    p.append(end_date + " 23:59:59")
                for r in conn.execute(q, p).fetchall():
                    records.append({
                        "entity_type": "farm_feed_purchase",
                        "entity_label": "Yem Alımı",
                        "record_id": r["id"],
                        "amount": round(float(r["total_amount"] or 0), 2),
                        "title": r["supplier"] or f"Yem Alımı #{r['id']}",
                        "original_date": r["purchase_date"],
                        "cancelled_at": r["cancelled_at"],
                        "cancelled_by": r["cancelled_by"] or "Patron",
                        "cancelled_by_role": r["cancelled_by_role"] or "patron",
                        "cancel_reason": r["cancel_reason"] or "",
                        "cancel_note": r["cancel_note"] or "",
                    })

        records.sort(key=lambda x: str(x.get("cancelled_at") or x.get("original_date") or ""), reverse=True)
        total_amount = round(sum(float(x["amount"] or 0) for x in records), 2)
        by_reason: Dict[str, int] = {}
        for rec in records:
            rs = rec.get("cancel_reason") or "Belirtilmedi"
            by_reason[rs] = by_reason.get(rs, 0) + 1

        return {
            "records": records,
            "total_count": len(records),
            "total_amount": total_amount,
            "by_reason": by_reason,
        }

    def get_expenses(
        self,
        user_id: Optional[Union[int, str]] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        uid = self._resolve_user_id(user_id)
        with self.db.get_connection() as conn:
            query = "SELECT * FROM expenses WHERE (user_id = ? OR user_id IS NULL)"
            params: list = [uid]
            if start_date:
                query += " AND expense_date >= ?"
                params.append(start_date)
            if end_date:
                query += " AND expense_date <= ?"
                params.append(end_date)
            query += " ORDER BY expense_date DESC, id DESC LIMIT 100"

            rows = conn.execute(query, params).fetchall()
            expenses = [dict(r) for r in rows]

            if not expenses and not self._hydration_completed:
                try:
                    self.ensure_initial_hydration()
                    rows = conn.execute(query, params).fetchall()
                    expenses = [dict(r) for r in rows]
                except Exception:
                    pass

            if not expenses:
                fb_q = query.replace("(user_id = ? OR user_id IS NULL)", "1=1")
                fb_p = params[1:]
                fb_rows = conn.execute(fb_q, fb_p).fetchall()
                if fb_rows:
                    expenses = [dict(r) for r in fb_rows]

            return expenses

    def get_store_hours(self, user_id: Optional[int] = None) -> Dict[str, str]:
        uid = user_id or 1
        with self.db.get_connection() as conn:
            row = conn.execute("SELECT weekday_hours, weekend_hours FROM store_settings WHERE user_id = ?", (uid,)).fetchone()
            if row:
                return {
                    "weekday": row["weekday_hours"] or "08:00 - 22:00",
                    "weekend": row["weekend_hours"] or "09:00 - 23:00",
                }
            return {"weekday": "08:00 - 22:00", "weekend": "09:00 - 23:00"}

    def save_store_hours(self, weekday: str, weekend: str, user_id: Optional[int] = None) -> bool:
        uid = user_id or 1
        with self.db.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO store_settings (user_id, weekday_hours, weekend_hours)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET weekday_hours = excluded.weekday_hours, weekend_hours = excluded.weekend_hours
                """,
                (uid, weekday.strip(), weekend.strip()),
            )
        if self.firestore_db:
            try:
                doc_id = f"u{uid}"
                self.firestore_db.collection("store_settings").document(doc_id).set({
                    "user_id": uid,
                    "weekday_hours": weekday.strip(),
                    "weekend_hours": weekend.strip(),
                })
            except Exception:
                pass
        return True

    def save_gemini_key(self, api_key: str) -> bool:
        clean_key = api_key.strip()
        if not clean_key:
            return False


        key_file = DATA_DIR / "gemini_key.txt"
        try:
            key_file.write_text(clean_key, encoding="utf-8")
        except Exception as e:
            logger.warning(f"Yerel gemini_key.txt yazma hatası: {e}")

        if self.firestore_db:
            try:
                self.firestore_db.collection("system_settings").document("gemini").set({
                    "api_key": clean_key,
                    "updated_at": get_turkey_now_str()
                }, merge=True)
                logger.info("Gemini API Key Firebase Firestore'a başarıyla kaydedildi.")
            except Exception as exc:
                logger.warning(f"Firestore Gemini key senkronizasyon hatası: {exc}")

        return True

    def get_gemini_key(self) -> Optional[str]:
        key_file = DATA_DIR / "gemini_key.txt"
        if key_file.exists():
            try:
                k = key_file.read_text(encoding="utf-8").strip()
                if k:
                    return k
            except Exception:
                pass

        if self.firestore_db:
            try:
                doc = self.firestore_db.collection("system_settings").document("gemini").get()
                if doc.exists:
                    d = doc.to_dict()
                    k = (d.get("api_key") or "").strip()
                    if k:
                        key_file.write_text(k, encoding="utf-8")
                        return k
            except Exception:
                pass

        return None

    # ════════════════════════════════════════════════════════════════════
    # BULUT TABANLI KULLANICI ÜYELİK VE GİRİŞ İŞLEMLERİ (CLOUD AUTH)
    # ════════════════════════════════════════════════════════════════════

    def _clean_phone(self, phone: str) -> str:
        cleaned = "".join(c for c in phone if c.isdigit())
        if cleaned.startswith("90") and len(cleaned) == 12:
            cleaned = cleaned[2:]
        if cleaned.startswith("0") and len(cleaned) == 11:
            cleaned = cleaned[1:]
        return cleaned

    def _hash_password(self, password: str) -> str:
        return hashlib.sha256(f"stok_salt_{password}".encode("utf-8")).hexdigest()

    def _generate_token(self) -> str:
        return secrets.token_hex(24)

    def register_user(
        self,
        company_name: str,
        full_name: str,
        phone: str,
        email: str,
        password: str,
    ) -> tuple[bool, str, Optional[Dict[str, Any]]]:
        c_name = company_name.strip()
        f_name = full_name.strip()
        p_raw = phone.strip()
        p_clean = self._clean_phone(p_raw)
        e_clean = email.strip().lower()
        if not c_name or not f_name or not p_raw or not e_clean or not password:
            return False, "Lütfen tüm zorunlu alanları doldurun!", None

        pass_hash = self._hash_password(password)
        token = self._generate_token()
        now = get_turkey_now_str()

        # 1. Firestore kontrolü
        if self.firestore_db:
            try:
                users_col = self.firestore_db.collection("users")
                docs = list(users_col.stream())
                for d in docs:
                    data = d.to_dict()
                    d_phone = self._clean_phone(str(data.get("phone", "")))
                    d_email = str(data.get("email", "")).strip().lower()
                    if (p_clean and d_phone == p_clean) or d_email == e_clean:
                        saved_hash = data.get("password_hash")
                        if not saved_hash or saved_hash == pass_hash:
                            uid = data.get("id") or (int(d.id) if d.id.isdigit() else 1)
                            data["password_hash"] = pass_hash
                            data["auth_token"] = token
                            data["updated_at"] = now
                            data["company_name"] = c_name or data.get("company_name", "")
                            data["full_name"] = f_name or data.get("full_name", "")
                            d.reference.set(data)
                            with self.db.get_connection() as conn:
                                conn.execute(
                                    """
                                    INSERT OR REPLACE INTO users (
                                        id, company_name, full_name, phone, email, password_hash, auth_token,
                                        synced_to_cloud, created_at, updated_at
                                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                                    """,
                                    (uid, data["company_name"], data["full_name"], p_clean, e_clean, pass_hash, token, now, now),
                                )
                            ret_user = dict(data)
                            ret_user.pop("password_hash", None)
                            if not self._hydration_completed:
                                self.ensure_initial_hydration()
                            return True, f"Mevcut hesabınız doğrulandı ve '{data['company_name']}' firmasıyla oturum açıldı!", ret_user
                        else:
                            return False, "Bu telefon veya e-posta ile kayıtlı bir hesap zaten var. Lütfen giriş yapınız.", None
            except Exception as e:
                logger.warning(f"Firestore kayıt kontrol uyarısı: {e}")

        # 2. SQLite kontrolü
        with self.db.get_connection() as conn:
            existing_p = conn.execute(
                "SELECT id, password_hash, company_name FROM users WHERE phone = ? OR phone = ? OR phone = ?",
                (p_raw, p_clean, "0" + p_clean)
            ).fetchone()
            if existing_p:
                if existing_p["password_hash"] == pass_hash or not existing_p["password_hash"]:
                    pass
                else:
                    return False, "Bu telefon numarası ile kayıtlı bir hesap zaten var! Lütfen 'Giriş Yap' sekmesinden giriş yapın.", None

            existing_e = conn.execute(
                "SELECT id, password_hash, company_name FROM users WHERE LOWER(email) = ?",
                (e_clean,)
            ).fetchone()
            if existing_e:
                if existing_e["password_hash"] == pass_hash or not existing_e["password_hash"]:
                    pass
                else:
                    return False, "Bu e-posta adresi ile kayıtlı bir hesap zaten var! Lütfen 'Giriş Yap' sekmesinden giriş yapın.", None

        if (existing_p and (existing_p["password_hash"] == pass_hash or not existing_p["password_hash"])) or \
           (existing_e and (existing_e["password_hash"] == pass_hash or not existing_e["password_hash"])):
            return self.login_user(phone_or_email=p_clean if existing_p else e_clean, password=password)

        with self.db.get_connection() as conn:
            max_id = 0
            row = conn.execute("SELECT MAX(id) as max_id FROM users").fetchone()
            if row and row["max_id"]:
                max_id = row["max_id"]

            uid = max_id + 1

            conn.execute(
                """
                INSERT INTO users (id, company_name, full_name, phone, email, password_hash, auth_token, synced_to_cloud, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (uid, c_name, f_name, p_clean, e_clean, pass_hash, token, now, now),
            )

        user_data = {
            "id": uid,
            "company_name": c_name,
            "full_name": f_name,
            "phone": p_clean,
            "email": e_clean,
            "password_hash": pass_hash,
            "auth_token": token,
            "synced_to_cloud": 1,
            "created_at": now,
            "updated_at": now,
        }

        if self.firestore_db:
            try:
                self.firestore_db.collection("users").document(str(uid)).set(user_data)
            except Exception as e:
                logger.warning(f"Firestore kullanıcı kaydı uyarısı: {e}")

        ret_user = dict(user_data)
        ret_user.pop("password_hash", None)
        return True, f"Tebrikler '{c_name}' firması ile hesabınız başarıyla oluşturuldu!", ret_user

    def login_user(self, phone_or_email: str, password: str, remember_me: bool = True) -> tuple[bool, str, Optional[Dict[str, Any]]]:
        query_val = phone_or_email.strip()
        p_clean = self._clean_phone(query_val)
        p_zero = ("0" + p_clean) if p_clean else ""
        p_90 = ("90" + p_clean) if p_clean else ""
        p_plus90 = ("+90" + p_clean) if p_clean else ""
        e_clean = query_val.lower()

        if not query_val or not password:
            return False, "Lütfen telefon/e-posta ve şifrenizi giriniz!", None

        pass_hash = self._hash_password(password)
        now = get_turkey_now_str()
        user_dict = None

        # 1. SQLite'da ara
        with self.db.get_connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM users 
                WHERE phone = ? OR phone = ? OR phone = ? OR phone = ? OR phone = ? OR LOWER(email) = ? OR LOWER(company_name) = ?
                """,
                (query_val, p_clean, p_zero, p_90, p_plus90, e_clean, e_clean),
            ).fetchone()

            if row:
                saved_hash = row["password_hash"]
                if not saved_hash or saved_hash == pass_hash:
                    user_dict = dict(row)
                    token = user_dict.get("auth_token")
                    if not token or remember_me or not saved_hash:
                        token = self._generate_token()
                        conn.execute(
                            "UPDATE users SET auth_token = ?, password_hash = ?, updated_at = ? WHERE id = ?",
                            (token, pass_hash, now, user_dict["id"])
                        )
                        user_dict["auth_token"] = token
                else:
                    return False, "Girilen şifre hatalı! Lütfen kontrol ediniz.", None

        if user_dict:
            if self.firestore_db:
                try:
                    self.firestore_db.collection("users").document(str(user_dict["id"])).update({
                        "auth_token": user_dict["auth_token"],
                        "password_hash": pass_hash,
                        "updated_at": now,
                    })
                except Exception:
                    pass
            user_dict.pop("password_hash", None)
            if not self._hydration_completed:
                self.ensure_initial_hydration()
            return True, f"Hoş geldiniz, {user_dict.get('full_name', '')} ({user_dict.get('company_name', '')})", user_dict

        # 2. SQLite'da bulunamazsa Firestore'dan sorgula
        if self.firestore_db:
            try:
                users_col = self.firestore_db.collection("users")
                docs = list(users_col.stream())
                for d in docs:
                    data = d.to_dict()
                    d_company = str(data.get("company_name", "")).strip().lower()
                    if (p_clean and d_phone == p_clean) or d_email == e_clean or str(data.get("phone", "")) == query_val or d_company == e_clean:
                        saved_hash = data.get("password_hash")
                        if not saved_hash or saved_hash == pass_hash:
                            token = self._generate_token()
                            uid = data.get("id") or (int(d.id) if d.id.isdigit() else 1)
                            data["id"] = uid
                            data["password_hash"] = pass_hash
                            data["auth_token"] = token
                            data["updated_at"] = now
                            d.reference.set(data)

                            with self.db.get_connection() as conn:
                                conn.execute(
                                    """
                                    INSERT OR REPLACE INTO users (
                                        id, company_name, full_name, phone, email, password_hash, auth_token,
                                        synced_to_cloud, created_at, updated_at
                                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                                    """,
                                    (
                                        uid,
                                        data.get("company_name", ""),
                                        data.get("full_name", ""),
                                        d_phone,
                                        d_email,
                                        pass_hash,
                                        token,
                                        data.get("created_at", now),
                                        now,
                                    ),
                                )
                            user_dict = dict(data)
                            user_dict.pop("password_hash", None)
                            if not self._hydration_completed:
                                self.ensure_initial_hydration()
                            return True, f"Hoş geldiniz, {user_dict.get('full_name', '')} ({user_dict.get('company_name', '')})", user_dict
                        else:
                            return False, "Girilen şifre hatalı! Lütfen kontrol ediniz.", None
            except Exception as e:
                logger.warning(f"Firestore oturum kontrol hatası: {e}")

        return False, "Bu telefon numarası veya e-posta ile kayıtlı bir hesap bulunamadı! 'Yeni Üyelik' sekmesinden ücretsiz kayıt olabilirsiniz.", None


    def verify_token(self, token: str) -> Optional[Dict[str, Any]]:
        if not token or not token.strip():
            return None
        tok = token.strip()

        # 1. SQLite kontrolü
        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT id, company_name, full_name, phone, email, auth_token, COALESCE(role, 'patron') as role FROM users WHERE auth_token = ?",
                (tok,),
            ).fetchone()
            if row:
                return dict(row)

        # 2. Firestore kontrolü
        if self.firestore_db:
            try:
                users_col = self.firestore_db.collection("users")
                docs = list(users_col.where("auth_token", "==", tok).stream())
                if docs:
                    data = docs[0].to_dict()
                    uid = data.get("id") or (int(docs[0].id) if docs[0].id.isdigit() else 1)
                    now = get_turkey_now_str()
                    with self.db.get_connection() as conn:
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO users (
                                id, company_name, full_name, phone, email, password_hash, auth_token,
                                synced_to_cloud, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                            """,
                            (
                                uid,
                                data.get("company_name", ""),
                                data.get("full_name", ""),
                                data.get("phone", ""),
                                data.get("email", ""),
                                data.get("password_hash", ""),
                                tok,
                                data.get("created_at", now),
                                now,
                            ),
                        )
                    ret_user = dict(data)
                    ret_user.pop("password_hash", None)
                    return ret_user
            except Exception as e:
                logger.warning(f"Firestore token doğrulama uyarısı: {e}")

        return None

    def get_all_stores(self) -> list[Dict[str, Any]]:
        """Sistemde kayıtlı mağazaları ve aktif ürün sayılarını listeler."""
        stores = []
        try:
            with self.db.get_connection() as conn:
                rows = conn.execute("""
                    SELECT u.id, u.company_name, u.full_name, u.phone, u.email, u.auth_token,
                           COUNT(p.id) as product_count
                    FROM users u
                    LEFT JOIN products p ON p.user_id = u.id AND p.is_active = 1
                    GROUP BY u.id
                    ORDER BY product_count DESC, u.id ASC
                """).fetchall()
                for r in rows:
                    token = r["auth_token"]
                    if not token:
                        token = self._generate_token()
                        conn.execute("UPDATE users SET auth_token = ? WHERE id = ?", (token, r["id"]))
                    stores.append({
                        "id": r["id"],
                        "company_name": r["company_name"] or f"Mağaza #{r['id']}",
                        "full_name": r["full_name"] or "",
                        "phone": r["phone"] or "",
                        "email": r["email"] or "",
                        "auth_token": token,
                        "product_count": int(r["product_count"] or 0)
                    })
        except Exception as e:
            logger.error(f"get_all_stores error: {e}")
        return stores

    def quick_switch_user(self, user_id: int) -> tuple[bool, str, Optional[Dict[str, Any]]]:
        """Kullanıcının şifresiz hızlı mağaza geçişi yapmasını sağlar (lokal/güvenilir ağ modu)."""
        try:
            with self.db.get_connection() as conn:
                row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
                if not row:
                    return False, f"Mağaza ID {user_id} bulunamadı!", None
                user_dict = dict(row)
                token = user_dict.get("auth_token")
                if not token:
                    token = self._generate_token()
                    conn.execute("UPDATE users SET auth_token = ? WHERE id = ?", (token, user_id))
                    user_dict["auth_token"] = token
                user_dict.pop("password_hash", None)
                return True, f"'{user_dict.get('company_name', '')}' mağazasına geçildi.", user_dict
        except Exception as e:
            return False, f"Mağaza geçiş hatası: {e}", None

    # ════════════════════════════════════════════════════════════════════
    # ÇİFTLİK YÖNETİMİ: YUMURTA SATIŞ, YEM ALIM & MÜŞTERİ METOTLARI
    # ════════════════════════════════════════════════════════════════════

    def save_farm_customer(self, name: str, phone: Optional[str] = None, user_id: Optional[int] = None) -> Optional[int]:
        """Çiftlik müşterisini kaydeder veya varsa mevcut olanı döner."""
        clean_name = name.strip()
        if not clean_name:
            return None
        uid = self._resolve_user_id(user_id) or 1
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cid = None

        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT id FROM farm_customers WHERE name = ? AND (user_id = ? OR user_id IS NULL)",
                (clean_name, uid),
            ).fetchone()
            if row:
                cid = row["id"]
            else:
                cursor = conn.execute(
                    "INSERT INTO farm_customers (user_id, name, phone, synced_to_cloud, created_at) VALUES (?, ?, ?, 0, ?)",
                    (uid, clean_name, (phone or "").strip(), now),
                )
                cid = cursor.lastrowid

        if cid and self.firestore_db:
            def _sync_fc():
                try:
                    doc_id = f"u{uid}_fc{cid}"
                    self.firestore_db.collection("farm_customers").document(doc_id).set({
                        "id": cid,
                        "user_id": uid,
                        "name": clean_name,
                        "phone": (phone or "").strip(),
                        "synced_to_cloud": 1,
                        "created_at": now,
                    })
                    with self.db.get_connection() as c:
                        c.execute("UPDATE farm_customers SET synced_to_cloud = 1 WHERE id = ?", (cid,))
                except Exception as ex:
                    logger.warning(f"Firestore farm customer sync error: {ex}")
            threading.Thread(target=_sync_fc, daemon=True).start()

        return cid

    def get_farm_customers(self, user_id: Optional[int] = None) -> List[str]:
        """Tüm kayıtlı ve geçmiş satışlardaki benzersiz müşteri isimlerini alfabetik sıralı döner."""
        uid = self._resolve_user_id(user_id) or 1
        names = set()
        with self.db.get_connection() as conn:
            # 1. farm_customers tablosundan
            fc_rows = conn.execute(
                "SELECT name FROM farm_customers WHERE (user_id = ? OR user_id IS NULL) AND name IS NOT NULL AND name != ''",
                (uid,),
            ).fetchall()
            for r in fc_rows:
                if r["name"] and r["name"].strip():
                    names.add(r["name"].strip())

            # 2. farm_egg_sales tablosundan
            es_rows = conn.execute(
                "SELECT DISTINCT customer_name FROM farm_egg_sales WHERE (user_id = ? OR user_id IS NULL) AND customer_name IS NOT NULL AND customer_name != ''",
                (uid,),
            ).fetchall()
            for r in es_rows:
                if r["customer_name"] and r["customer_name"].strip():
                    names.add(r["customer_name"].strip())

            # 3. sales tablosundaki müşteri isimlerinden de destek
            s_rows = conn.execute(
                "SELECT DISTINCT customer_name FROM sales WHERE (user_id = ? OR user_id IS NULL) AND customer_name IS NOT NULL AND customer_name != ''",
                (uid,),
            ).fetchall()
            for r in s_rows:
                if r["customer_name"] and r["customer_name"].strip():
                    names.add(r["customer_name"].strip())

        return sorted(list(names), key=lambda s: s.lower())

    def _cleanup_duplicate_farm_egg_sales(self) -> int:
        """Geçmişte tarih formatı ('YYYY-MM-DD' vs 'YYYY-MM-DD 00:00:00') veya çift tıklama nedeniyle
        mükerrer oluşmuş çiftlik yumurta satış kayıtlarını temizler."""
        removed = 0
        try:
            with self.db.get_connection() as conn:
                # 1. 'YYYY-MM-DD 00:00:00' olup aynı gün/müşteri/koli/tutar ile başka kaydı olan kopyaları bul
                dup_rows = conn.execute(
                    """
                    SELECT b.id, b.user_id
                    FROM farm_egg_sales a
                    JOIN farm_egg_sales b ON a.id < b.id
                      AND COALESCE(a.user_id, 1) = COALESCE(b.user_id, 1)
                      AND a.customer_name = b.customer_name
                      AND ABS(a.box_count - b.box_count) < 0.001
                      AND ABS(a.total_amount - b.total_amount) < 0.05
                      AND substr(a.sale_date, 1, 10) = substr(b.sale_date, 1, 10)
                      AND a.source = b.source
                      AND COALESCE(a.note, '') = COALESCE(b.note, '')
                      AND (
                          b.sale_date LIKE '%00:00:00'
                          OR a.sale_date = b.sale_date
                          OR ABS(strftime('%s', a.created_at) - strftime('%s', b.created_at)) <= 10
                      )
                    """
                ).fetchall()
                if dup_rows:
                    dup_ids = list({int(r["id"]) for r in dup_rows})
                    for did in dup_ids:
                        conn.execute("DELETE FROM farm_egg_sales WHERE id = ?", (did,))
                    removed = len(dup_ids)
                    if self.firestore_db:
                        def _del_cloud_dups(rows_to_del):
                            for r in rows_to_del:
                                try:
                                    uid_val = r["user_id"] or 1
                                    did_val = r["id"]
                                    self.firestore_db.collection("farm_egg_sales").document(f"u{uid_val}_es{did_val}").delete()
                                except Exception:
                                    pass
                        threading.Thread(target=_del_cloud_dups, args=([dict(r) for r in dup_rows],), daemon=True).start()
        except Exception as e:
            logger.warning(f"Mükerrer çiftlik yumurta satışı temizleme uyarısı: {e}")
        return removed

    def add_farm_egg_sale(
        self,
        customer_name: str,
        box_count: float,
        unit_price: float,
        sale_date: Optional[str] = None,
        source: str = "Ciftlik",
        note: Optional[str] = None,
        user_id: Optional[int] = None,
        unit_type: str = "koli",
        piece_count: Optional[float] = None,
        total_amount: Optional[float] = None,
    ) -> tuple[bool, str, Optional[int]]:
        """Yumurta satışını kaydeder (koli/adet sayısı ve birim fiyat üzerinden)."""
        clean_cust = customer_name.strip()
        if not clean_cust:
            return False, "Müşteri adı boş olamaz!", None

        try:
            b_count = float(box_count)
            u_price = float(unit_price)
            if b_count <= 0 or u_price < 0:
                return False, "Koli/adet miktarı pozitif, birim fiyat 0 veya üzeri olmalıdır!", None
        except (ValueError, TypeError):
            return False, "Geçersiz miktar veya fiyat formatı!", None

        u_type = "adet" if str(unit_type).lower().strip() == "adet" else "koli"
        if piece_count is not None:
            p_count = round(float(piece_count), 2)
        else:
            p_count = round(b_count * 30.0, 2) if u_type == "koli" else round(b_count, 2)

        tot_amount = round(float(total_amount), 2) if total_amount is not None else round(b_count * u_price, 2)
        uid = self._resolve_user_id(user_id) or 1
        now_dt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        raw_s_date = sale_date.strip() if sale_date and sale_date.strip() else now_dt
        if len(raw_s_date) == 10:
            s_date = f"{raw_s_date} {now_dt[11:]}"
        else:
            s_date = raw_s_date.replace("T", " ")

        clean_note = (note or "").strip()

        # Müşteriyi farm_customers'a da kaydet / hatırla
        self.save_farm_customer(clean_cust, user_id=uid)

        with self.db.get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO farm_egg_sales (
                    user_id, customer_name, box_count, unit_type, piece_count, unit_price, total_amount,
                    source, sale_date, note, status, synced_to_cloud, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 0, ?)
                """,
                (uid, clean_cust, b_count, u_type, p_count, u_price, tot_amount, source, s_date, clean_note, now_dt),
            )
            sale_id = cursor.lastrowid
            log_audit(
                action="FARM_EGG_SALE_CREATE",
                entity_type="farm_egg_sale",
                entity_id=sale_id,
                old_value=None,
                new_value={
                    "id": sale_id,
                    "customer_name": clean_cust,
                    "box_count": b_count,
                    "unit_type": u_type,
                    "piece_count": p_count,
                    "unit_price": u_price,
                    "total_amount": tot_amount,
                    "source": source,
                    "sale_date": s_date,
                },
                user_id=uid,
                conn=conn,
            )

        if sale_id:
            def _async_sync_egg_sale():
                self.ensure_firebase()
                if not self.firestore_db:
                    return
                try:
                    doc_id = f"u{uid}_es{sale_id}"
                    self.firestore_db.collection("farm_egg_sales").document(doc_id).set({
                        "id": sale_id,
                        "user_id": uid,
                        "customer_name": clean_cust,
                        "box_count": b_count,
                        "unit_type": u_type,
                        "piece_count": p_count,
                        "unit_price": u_price,
                        "total_amount": tot_amount,
                        "source": source,
                        "sale_date": s_date,
                        "note": clean_note,
                        "synced_to_cloud": 1,
                        "created_at": now_dt,
                    })
                    with self.db.get_connection() as c:
                        c.execute("UPDATE farm_egg_sales SET synced_to_cloud = 1 WHERE id = ?", (sale_id,))
                except Exception as ex:
                    logger.warning(f"Firestore farm egg sale sync error: {ex}")
            threading.Thread(target=_async_sync_egg_sale, daemon=True).start()

        qty_str = f"{int(p_count)} Adet" if u_type == "adet" else f"{b_count:g} Koli"
        return True, f"Yumurta satışı kaydedildi ({qty_str} - {tot_amount:.2f} ₺)", sale_id

    def get_farm_egg_sales(
        self,
        user_id: Optional[Union[int, str]] = None,
        customer_name: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        source: Optional[str] = None,
        limit: Optional[int] = None,
        include_cancelled: bool = False,
    ) -> List[Dict[str, Any]]:
        """Yumurta satış kayıtlarını filtreli ve tarih sırasına göre döner."""
        uid = self._resolve_user_id(user_id)
        query = "SELECT * FROM farm_egg_sales WHERE (user_id = ? OR user_id IS NULL)"
        if not include_cancelled:
            query += " AND COALESCE(status, 'active') != 'cancelled'"
        params: list = [uid]

        if customer_name and customer_name.strip():
            query += " AND customer_name LIKE ?"
            params.append(f"%{customer_name.strip()}%")

        if start_date and start_date.strip():
            sd = start_date.strip()
            if len(sd) == 10:
                query += " AND substr(replace(sale_date, 'T', ' '), 1, 10) >= ?"
                params.append(sd)
            else:
                query += " AND replace(sale_date, 'T', ' ') >= ?"
                params.append(sd)

        if end_date and end_date.strip():
            ed = end_date.strip()
            if len(ed) == 10:
                query += " AND substr(replace(sale_date, 'T', ' '), 1, 10) <= ?"
                params.append(ed)
            else:
                query += " AND replace(sale_date, 'T', ' ') <= ?"
                params.append(ed)

        if source and source.strip() and source.strip().lower() != "tümü":
            query += " AND source = ?"
            params.append(source.strip())

        query += " ORDER BY replace(sale_date, 'T', ' ') DESC, id DESC"

        if limit is not None and int(limit) > 0:
            query += f" LIMIT {int(limit)}"

        with self.db.get_connection() as conn:
            rows = conn.execute(query, params).fetchall()
            sales = [dict(r) for r in rows]

            if not sales and self.ensure_firebase():
                tot_es = conn.execute("SELECT COUNT(*) FROM farm_egg_sales").fetchone()[0]
                if tot_es == 0:
                    now_ts = time.time()
                    if now_ts - self._last_pull_timestamp > 10:
                        try:
                            self.pull_all_from_firebase(user_id=uid)
                            self._last_pull_timestamp = now_ts
                            self._hydration_completed = True
                            rows = conn.execute(query, params).fetchall()
                            sales = [dict(r) for r in rows]
                        except Exception as e_pull:
                            logger.warning(f"get_farm_egg_sales Firestore otomatik çekme uyarısı: {e_pull}")

            if not sales:
                fb_q = query.replace("(user_id = ? OR user_id IS NULL)", "1=1")
                fb_p = params[1:]
                fb_rows = conn.execute(fb_q, fb_p).fetchall()
                if fb_rows:
                    sales = [dict(r) for r in fb_rows]

            return sales

    def delete_farm_egg_sale(
        self,
        sale_id: int,
        user_id: Optional[int] = None,
        cancel_reason: Optional[str] = None,
        cancel_note: str = "",
        actor: Optional[Dict[str, Any]] = None,
        require_reason: bool = False,
    ) -> tuple[bool, str]:
        """Yumurta satışı kaydını fiziksel silmek yerine iptal edildi (cancelled) durumuna alır."""
        if require_reason:
            ok_r, msg_r, clean_reason, clean_note = validate_cancel_reason(cancel_reason, cancel_note)
            if not ok_r:
                return False, msg_r
        else:
            clean_reason = str(cancel_reason or "Çiftlik satış iptali").strip()
            clean_note = str(cancel_note or "").strip()

        uid = self._resolve_user_id(user_id) or 1
        now = get_turkey_now_str()
        act = actor or {"name": "Patron", "role": "patron", "user_id": uid}
        actor_name = str(act.get("name") or "Patron")
        actor_role = normalize_role(act.get("role"))

        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM farm_egg_sales WHERE id = ? AND (user_id = ? OR user_id IS NULL)",
                (sale_id, uid),
            ).fetchone()
            if not row:
                return False, "Satış kaydı bulunamadı!"
            if str(row["status"] or "active").lower() == "cancelled":
                return False, "Bu yumurta satışı zaten iptal edilmiş!"

            old_val = {
                "id": row["id"],
                "customer_name": row["customer_name"],
                "total_amount": row["total_amount"],
                "sale_date": row["sale_date"],
                "status": row["status"] or "active",
            }
            conn.execute(
                """
                UPDATE farm_egg_sales
                SET status = 'cancelled',
                    cancelled_at = ?,
                    cancelled_by = ?,
                    cancelled_by_role = ?,
                    cancel_reason = ?,
                    cancel_note = ?,
                    synced_to_cloud = 0
                WHERE id = ?
                """,
                (now, actor_name, actor_role, clean_reason, clean_note, sale_id),
            )
            log_audit(
                action="FARM_EGG_SALE_CANCEL",
                entity_type="farm_egg_sale",
                entity_id=sale_id,
                old_value=old_val,
                new_value={
                    "status": "cancelled",
                    "cancelled_at": now,
                    "cancelled_by": actor_name,
                    "cancel_reason": clean_reason,
                    "cancel_note": clean_note,
                },
                reason=clean_reason + (f" — {clean_note}" if clean_note else ""),
                actor=act,
                user_id=uid,
                conn=conn,
            )

        if self.firestore_db:
            def _cancel_egg():
                try:
                    self.firestore_db.collection("farm_egg_sales").document(f"u{uid}_es{sale_id}").set(
                        {"status": "cancelled", "cancelled_at": now, "cancel_reason": clean_reason}, merge=True
                    )
                except Exception:
                    pass
            threading.Thread(target=_cancel_egg, daemon=True).start()

        return True, "Yumurta satışı kaydı iptal edildi."

    def add_farm_feed_purchase(
        self,
        bag_count: float,
        unit_price: float = 0.0,
        total_amount: float = 0.0,
        purchase_date: Optional[str] = None,
        supplier: Optional[str] = None,
        note: Optional[str] = None,
        user_id: Optional[int] = None,
    ) -> tuple[bool, str, Optional[int]]:
        """Yem alımını kaydeder (1 çuval = 50 kg -> Ton hesabı otomatik yapılır)."""
        try:
            b_count = float(bag_count)
            if b_count <= 0:
                return False, "Çuval adedi 0'dan büyük olmalıdır!", None
        except (ValueError, TypeError):
            return False, "Geçersiz çuval adedi formatı!", None

        bag_weight_kg = 50.0
        total_weight_kg = round(b_count * bag_weight_kg, 2)
        total_weight_ton = round(total_weight_kg / 1000.0, 3)

        u_price = float(unit_price or 0.0)
        t_amount = float(total_amount or 0.0)
        if t_amount <= 0.0 and u_price > 0.0:
            t_amount = round(b_count * u_price, 2)
        elif u_price <= 0.0 and t_amount > 0.0:
            u_price = round(t_amount / b_count, 2)

        uid = self._resolve_user_id(user_id) or 1
        now_dt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        p_date = purchase_date.strip() if purchase_date and purchase_date.strip() else now_dt[:10]

        with self.db.get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO farm_feed_purchases (
                    user_id, bag_count, bag_weight_kg, total_weight_kg, total_weight_ton,
                    unit_price, total_amount, purchase_date, supplier, note, status, synced_to_cloud, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 0, ?)
                """,
                (
                    uid,
                    b_count,
                    bag_weight_kg,
                    total_weight_kg,
                    total_weight_ton,
                    u_price,
                    t_amount,
                    p_date,
                    (supplier or "").strip(),
                    (note or "").strip(),
                    now_dt,
                ),
            )
            purchase_id = cursor.lastrowid
            log_audit(
                action="FARM_FEED_PURCHASE_CREATE",
                entity_type="farm_feed_purchase",
                entity_id=purchase_id,
                old_value=None,
                new_value={
                    "id": purchase_id,
                    "bag_count": b_count,
                    "total_weight_ton": total_weight_ton,
                    "total_amount": t_amount,
                    "purchase_date": p_date,
                    "supplier": (supplier or "").strip(),
                },
                user_id=uid,
                conn=conn,
            )

        if purchase_id and self.firestore_db:
            def _sync_feed():
                try:
                    doc_id = f"u{uid}_fp{purchase_id}"
                    self.firestore_db.collection("farm_feed_purchases").document(doc_id).set({
                        "id": purchase_id,
                        "user_id": uid,
                        "bag_count": b_count,
                        "bag_weight_kg": bag_weight_kg,
                        "total_weight_kg": total_weight_kg,
                        "total_weight_ton": total_weight_ton,
                        "unit_price": u_price,
                        "total_amount": t_amount,
                        "purchase_date": p_date,
                        "supplier": (supplier or "").strip(),
                        "note": (note or "").strip(),
                        "synced_to_cloud": 1,
                        "created_at": now_dt,
                    })
                    with self.db.get_connection() as c:
                        c.execute("UPDATE farm_feed_purchases SET synced_to_cloud = 1 WHERE id = ?", (purchase_id,))
                except Exception as ex:
                    logger.warning(f"Firestore farm feed purchase sync error: {ex}")
            threading.Thread(target=_sync_feed, daemon=True).start()

        return True, f"Yem alımı kaydedildi ({b_count:g} Çuval = {total_weight_ton:.2f} Ton)", purchase_id

    def get_farm_feed_purchases(
        self,
        user_id: Optional[Union[int, str]] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: Optional[int] = None,
        include_cancelled: bool = False,
    ) -> List[Dict[str, Any]]:
        """Yem alım kayıtlarını tarih sırasına göre döner."""
        uid = self._resolve_user_id(user_id)
        query = "SELECT * FROM farm_feed_purchases WHERE (user_id = ? OR user_id IS NULL)"
        if not include_cancelled:
            query += " AND COALESCE(status, 'active') != 'cancelled'"
        params: list = [uid]

        if start_date and start_date.strip():
            query += " AND purchase_date >= ?"
            params.append(start_date.strip())

        if end_date and end_date.strip():
            query += " AND purchase_date <= ?"
            params.append(end_date.strip())

        query += " ORDER BY purchase_date DESC, id DESC"

        if limit is not None and int(limit) > 0:
            query += f" LIMIT {int(limit)}"

        with self.db.get_connection() as conn:
            rows = conn.execute(query, params).fetchall()
            purchases = [dict(r) for r in rows]

            if not purchases and self.ensure_firebase():
                tot_fp = conn.execute("SELECT COUNT(*) FROM farm_feed_purchases").fetchone()[0]
                if tot_fp == 0:
                    now_ts = time.time()
                    if now_ts - self._last_pull_timestamp > 10:
                        try:
                            self.pull_all_from_firebase(user_id=uid)
                            self._last_pull_timestamp = now_ts
                            self._hydration_completed = True
                            rows = conn.execute(query, params).fetchall()
                            purchases = [dict(r) for r in rows]
                        except Exception as e_pull:
                            logger.warning(f"get_farm_feed_purchases Firestore otomatik çekme uyarısı: {e_pull}")

            if not purchases:
                fb_q = query.replace("(user_id = ? OR user_id IS NULL)", "1=1")
                fb_p = params[1:]
                fb_rows = conn.execute(fb_q, fb_p).fetchall()
                if fb_rows:
                    purchases = [dict(r) for r in fb_rows]

            return purchases

    def get_farm_feed_summary(self, user_id: Optional[Union[int, str]] = None) -> Dict[str, float]:
        """Yem alımları için genel toplam özetini döner (iptal edilenler hariç)."""
        uid = self._resolve_user_id(user_id)
        with self.db.get_connection() as conn:
            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(bag_count), 0) as total_bags,
                    COALESCE(SUM(total_weight_ton), 0) as total_tons,
                    COALESCE(SUM(total_amount), 0) as total_cost
                FROM farm_feed_purchases
                WHERE (user_id = ? OR user_id IS NULL)
                  AND COALESCE(status, 'active') != 'cancelled'
                """,
                (uid,)
            ).fetchone()
            if not row or (row["total_bags"] == 0 and not self._hydration_completed):
                try:
                    self.ensure_initial_hydration()
                    row = conn.execute(
                        """
                        SELECT
                            COALESCE(SUM(bag_count), 0) as total_bags,
                            COALESCE(SUM(total_weight_ton), 0) as total_tons,
                            COALESCE(SUM(total_amount), 0) as total_cost
                        FROM farm_feed_purchases
                        WHERE (user_id = ? OR user_id IS NULL)
                          AND COALESCE(status, 'active') != 'cancelled'
                        """,
                        (uid,)
                    ).fetchone()
                except Exception:
                    pass
            if not row or row["total_bags"] == 0:
                fb_row = conn.execute(
                    """
                    SELECT
                        COALESCE(SUM(bag_count), 0) as total_bags,
                        COALESCE(SUM(total_weight_ton), 0) as total_tons,
                        COALESCE(SUM(total_amount), 0) as total_cost
                    FROM farm_feed_purchases
                    WHERE COALESCE(status, 'active') != 'cancelled'
                    """
                ).fetchone()
                if fb_row:
                    row = fb_row

            return {
                "total_bags": float(row["total_bags"] if row else 0),
                "total_tons": float(row["total_tons"] if row else 0),
                "total_cost": float(row["total_cost"] if row else 0),
            }


    def delete_farm_feed_purchase(
        self,
        purchase_id: int,
        user_id: Optional[int] = None,
        cancel_reason: Optional[str] = None,
        cancel_note: str = "",
        actor: Optional[Dict[str, Any]] = None,
        require_reason: bool = False,
    ) -> tuple[bool, str]:
        """Yem alım kaydını fiziksel silmek yerine iptal edildi (cancelled) durumuna alır."""
        if require_reason:
            ok_r, msg_r, clean_reason, clean_note = validate_cancel_reason(cancel_reason, cancel_note)
            if not ok_r:
                return False, msg_r
        else:
            clean_reason = str(cancel_reason or "Yem alım iptali").strip()
            clean_note = str(cancel_note or "").strip()

        uid = self._resolve_user_id(user_id) or 1
        now = get_turkey_now_str()
        act = actor or {"name": "Patron", "role": "patron", "user_id": uid}
        actor_name = str(act.get("name") or "Patron")
        actor_role = normalize_role(act.get("role"))

        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM farm_feed_purchases WHERE id = ? AND (user_id = ? OR user_id IS NULL)",
                (purchase_id, uid),
            ).fetchone()
            if not row:
                return False, "Yem alım kaydı bulunamadı!"
            if str(row["status"] or "active").lower() == "cancelled":
                return False, "Bu yem alımı zaten iptal edilmiş!"

            old_val = {
                "id": row["id"],
                "bag_count": row["bag_count"],
                "total_amount": row["total_amount"],
                "supplier": row["supplier"],
                "status": row["status"] or "active",
            }
            conn.execute(
                """
                UPDATE farm_feed_purchases
                SET status = 'cancelled',
                    cancelled_at = ?,
                    cancelled_by = ?,
                    cancelled_by_role = ?,
                    cancel_reason = ?,
                    cancel_note = ?,
                    synced_to_cloud = 0
                WHERE id = ?
                """,
                (now, actor_name, actor_role, clean_reason, clean_note, purchase_id),
            )
            log_audit(
                action="FARM_FEED_PURCHASE_CANCEL",
                entity_type="farm_feed_purchase",
                entity_id=purchase_id,
                old_value=old_val,
                new_value={
                    "status": "cancelled",
                    "cancelled_at": now,
                    "cancelled_by": actor_name,
                    "cancel_reason": clean_reason,
                    "cancel_note": clean_note,
                },
                reason=clean_reason + (f" — {clean_note}" if clean_note else ""),
                actor=act,
                user_id=uid,
                conn=conn,
            )

        if self.firestore_db:
            def _cancel_feed():
                try:
                    self.firestore_db.collection("farm_feed_purchases").document(f"u{uid}_fp{purchase_id}").set(
                        {"status": "cancelled", "cancelled_at": now, "cancel_reason": clean_reason}, merge=True
                    )
                except Exception:
                    pass
            threading.Thread(target=_cancel_feed, daemon=True).start()

        return True, "Yem alım kaydı iptal edildi."

    # =========================================================================
    # GALLUS MAMA WEB SİPARİŞLERİ, BÜLTEN MAİLLERİ VE PUAN ONAY SİSTEMİ
    # =========================================================================
    def _ensure_web_ecommerce_tables(self) -> None:
        try:
            with self.db.get_connection() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS web_orders (
                        id TEXT PRIMARY KEY,
                        order_date TEXT,
                        customer_name TEXT,
                        customer_phone TEXT,
                        customer_email TEXT,
                        customer_city TEXT,
                        customer_address TEXT,
                        customer_note TEXT,
                        payment_method TEXT,
                        total_str TEXT,
                        total_numeric REAL DEFAULT 0,
                        earned_points INTEGER DEFAULT 0,
                        used_points INTEGER DEFAULT 0,
                        points_approved INTEGER DEFAULT 0,
                        status TEXT DEFAULT 'Onay Bekliyor',
                        items_json TEXT,
                        raw_json TEXT,
                        updated_at TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS newsletter_subscribers (
                        email TEXT PRIMARY KEY,
                        name TEXT,
                        phone TEXT,
                        source TEXT,
                        subscribed_at TEXT
                    )
                    """
                )
        except Exception as e:
            logger.debug("Web ecommerce tables init warning: %s", e)

    def sync_and_get_web_orders_and_subscribers(self) -> Dict[str, Any]:
        """Web sitesinden (Firestore + gallusmama.com + yerel database.json) gelen siparişleri ve bülten/üye maillerini senkronize eder."""
        import json as _json
        import urllib.request
        from pathlib import Path

        self._ensure_web_ecommerce_tables()

        orders_map: Dict[str, Dict[str, Any]] = {}
        emails_map: Dict[str, Dict[str, Any]] = {}
        members_map: Dict[str, Dict[str, Any]] = {}

        def _register_email(email_str: str, name: str = "", phone: str = "", source: str = "Web Bülten", date_str: str = "") -> None:
            clean = str(email_str or "").strip().lower()
            if not clean or "@" not in clean:
                return
            prev = emails_map.get(clean, {})
            emails_map[clean] = {
                "email": clean,
                "name": name or prev.get("name", ""),
                "phone": phone or prev.get("phone", ""),
                "source": source if not prev.get("source") else prev.get("source"),
                "date": date_str or prev.get("date", "")
            }

        def _register_order(ord_obj: Dict[str, Any]) -> None:
            if not isinstance(ord_obj, dict):
                return
            oid = str(ord_obj.get("id") or "").strip()
            if not oid:
                return
            cust = ord_obj.get("customer") if isinstance(ord_obj.get("customer"), dict) else {}
            c_email = str(cust.get("email") or ord_obj.get("email") or "").strip().lower()
            c_name = str(cust.get("fullname") or ord_obj.get("customer_name") or "").strip()
            c_phone = str(cust.get("phone") or ord_obj.get("customer_phone") or "").strip()
            c_city = str(cust.get("city") or cust.get("district") or ord_obj.get("customer_city") or "").strip()
            c_addr = str(cust.get("address") or ord_obj.get("customer_address") or "").strip()
            c_note = str(cust.get("note") or ord_obj.get("customer_note") or "").strip()

            t_num = ord_obj.get("totalNumeric")
            if t_num is None:
                raw_t = str(ord_obj.get("total") or "0")
                import re
                s = re.sub(r"[^\d.,-]", "", raw_t).strip()
                if "." in s and "," in s:
                    s = s.replace(".", "").replace(",", ".")
                elif "," in s:
                    s = s.replace(",", ".")
                try:
                    t_num = float(s)
                except Exception:
                    t_num = 0.0
            else:
                try:
                    t_num = float(t_num)
                except Exception:
                    t_num = 0.0

            earned = ord_obj.get("earnedPoints")
            if earned is None:
                earned = int(round((t_num * 0.01) * 100))
            else:
                earned = int(earned)

            prev = orders_map.get(oid, {})
            is_approved = bool(
                ord_obj.get("pointsApproved") is True
                or ord_obj.get("status") in ["Onaylandı", "Tamamlandı"]
                or prev.get("pointsApproved") is True
                or prev.get("status") in ["Onaylandı", "Tamamlandı"]
            )
            status_str = "Onaylandı" if is_approved else str(ord_obj.get("status") or prev.get("status") or "Onay Bekliyor")

            normalized = {
                "id": oid,
                "date": str(ord_obj.get("date") or prev.get("date") or ""),
                "customer": {
                    "fullname": c_name,
                    "phone": c_phone,
                    "email": c_email,
                    "city": c_city,
                    "address": c_addr,
                    "note": c_note
                },
                "email": c_email,
                "paymentMethod": str(ord_obj.get("paymentMethod") or prev.get("paymentMethod") or "Havale / EFT"),
                "items": ord_obj.get("items") if isinstance(ord_obj.get("items"), list) else prev.get("items", []),
                "total": str(ord_obj.get("total") or f"{t_num:,.2f} ₺"),
                "totalNumeric": t_num,
                "earnedPoints": earned,
                "usedPoints": int(ord_obj.get("usedPoints") or prev.get("usedPoints") or 0),
                "pointsApproved": is_approved,
                "status": status_str
            }
            orders_map[oid] = normalized
            if c_email and "@" in c_email:
                _register_email(c_email, c_name, c_phone, "Sipariş Müşterisi", normalized["date"])

        # 1. Load existing records from local SQLite first
        try:
            with self.db.get_connection() as conn:
                for r in conn.execute("SELECT * FROM web_orders ORDER BY updated_at DESC, id DESC").fetchall():
                    try:
                        raw = _json.loads(r["raw_json"]) if r["raw_json"] else {}
                    except Exception:
                        raw = {}
                    raw["id"] = r["id"]
                    raw["pointsApproved"] = bool(r["points_approved"])
                    raw["status"] = r["status"]
                    _register_order(raw)
                for r in conn.execute("SELECT * FROM newsletter_subscribers").fetchall():
                    _register_email(r["email"], r["name"] or "", r["phone"] or "", r["source"] or "Web Bülten", r["subscribed_at"] or "")
        except Exception as e:
            logger.debug("SQLite web_orders read warning: %s", e)

        # 2. Load from local gallus mama database.json if present on this machine
        local_gallus_paths = [
            Path(r"c:\Users\birdo\OneDrive\Masaüstü\gallus mama\data\database.json"),
            Path(r"c:\Users\birdo\OneDrive\Masaüstü\gallus mama\gallus mama\data\database.json")
        ]
        for gp in local_gallus_paths:
            if gp.exists():
                try:
                    gdata = _json.loads(gp.read_text(encoding="utf-8"))
                    for o in gdata.get("orders", []):
                        _register_order(o)
                    for em in gdata.get("subscribers", []):
                        _register_email(em, source="Web Bülten")
                    for sd in gdata.get("subscriberDetails", []):
                        if isinstance(sd, dict):
                            _register_email(sd.get("email", ""), sd.get("name", ""), sd.get("phone", ""), sd.get("source", "Web Bülten"), sd.get("date", ""))
                    for m in gdata.get("members", []):
                        if isinstance(m, dict) and m.get("email"):
                            em_clean = str(m.get("email")).strip().lower()
                            members_map[em_clean] = m
                            _register_email(em_clean, m.get("fullname", ""), m.get("phone", ""), "Site Üyesi", m.get("createdAt", ""))
                except Exception:
                    pass

        # 3. Load from Firestore ('orders', 'newsletter_subscribers', 'web_members')
        if self.firestore_db:
            try:
                for doc in self.firestore_db.collection("orders").stream():
                    d = doc.to_dict() or {}
                    d["id"] = d.get("id") or doc.id
                    _register_order(d)
            except Exception as e:
                logger.debug("Firestore orders read warning: %s", e)
            try:
                for doc in self.firestore_db.collection("newsletter_subscribers").stream():
                    d = doc.to_dict() or {}
                    _register_email(d.get("email") or doc.id, d.get("name", ""), d.get("phone", ""), d.get("source", "Web Bülten"), str(d.get("date") or ""))
            except Exception as e:
                logger.debug("Firestore newsletter read warning: %s", e)
            try:
                for doc in self.firestore_db.collection("web_members").stream():
                    d = doc.to_dict() or {}
                    em = str(d.get("email") or doc.id).strip().lower()
                    if em and "@" in em:
                        members_map[em] = d
                        _register_email(em, d.get("fullname", ""), d.get("phone", ""), "Site Üyesi", str(d.get("createdAt") or ""))
            except Exception as e:
                logger.debug("Firestore web_members read warning: %s", e)

        # 4. Pull from live HTTP endpoints (localhost:3000 & gallusmama.com)
        for endpoint in ["http://localhost:3000/api/sync/web-data", "https://gallusmama.com/api/sync/web-data"]:
            try:
                req = urllib.request.Request(endpoint, headers={"User-Agent": "StokSatisTakip/1.0"})
                with urllib.request.urlopen(req, timeout=2.5) as resp:
                    if resp.status == 200:
                        payload = _json.loads(resp.read().decode("utf-8"))
                        for o in payload.get("orders", []):
                            _register_order(o)
                        for em in payload.get("subscribers", []):
                            _register_email(em, source="Web Bülten")
                        for sd in payload.get("subscriberDetails", []):
                            if isinstance(sd, dict):
                                _register_email(sd.get("email", ""), sd.get("name", ""), sd.get("phone", ""), sd.get("source", "Web Bülten"), sd.get("date", ""))
                        for m in payload.get("members", []):
                            if isinstance(m, dict) and m.get("email"):
                                em_clean = str(m.get("email")).strip().lower()
                                members_map[em_clean] = m
                                _register_email(em_clean, m.get("fullname", ""), m.get("phone", ""), "Site Üyesi", m.get("createdAt", ""))
            except Exception:
                pass

        # 5. Persist merged orders & subscribers back to SQLite
        try:
            now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with self.db.get_connection() as conn:
                for oid, o in orders_map.items():
                    c = o.get("customer") or {}
                    conn.execute(
                        """
                        INSERT INTO web_orders (
                            id, order_date, customer_name, customer_phone, customer_email,
                            customer_city, customer_address, customer_note, payment_method,
                            total_str, total_numeric, earned_points, used_points,
                            points_approved, status, items_json, raw_json, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET
                            customer_name = excluded.customer_name,
                            customer_phone = excluded.customer_phone,
                            customer_email = excluded.customer_email,
                            customer_city = excluded.customer_city,
                            customer_address = excluded.customer_address,
                            customer_note = excluded.customer_note,
                            payment_method = excluded.payment_method,
                            total_str = excluded.total_str,
                            total_numeric = excluded.total_numeric,
                            earned_points = excluded.earned_points,
                            used_points = excluded.used_points,
                            points_approved = MAX(web_orders.points_approved, excluded.points_approved),
                            status = CASE WHEN web_orders.points_approved = 1 OR excluded.points_approved = 1 THEN 'Onaylandı' ELSE excluded.status END,
                            items_json = excluded.items_json,
                            raw_json = excluded.raw_json,
                            updated_at = excluded.updated_at
                        """,
                        (
                            oid,
                            o.get("date", ""),
                            c.get("fullname", ""),
                            c.get("phone", ""),
                            c.get("email", ""),
                            c.get("city", ""),
                            c.get("address", ""),
                            c.get("note", ""),
                            o.get("paymentMethod", ""),
                            o.get("total", ""),
                            float(o.get("totalNumeric") or 0),
                            int(o.get("earnedPoints") or 0),
                            int(o.get("usedPoints") or 0),
                            1 if o.get("pointsApproved") else 0,
                            o.get("status", "Onay Bekliyor"),
                            _json.dumps(o.get("items", []), ensure_ascii=False),
                            _json.dumps(o, ensure_ascii=False),
                            now_iso
                        )
                    )
                for em, ed in emails_map.items():
                    conn.execute(
                        """
                        INSERT INTO newsletter_subscribers (email, name, phone, source, subscribed_at)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(email) DO UPDATE SET
                            name = CASE WHEN excluded.name != '' THEN excluded.name ELSE newsletter_subscribers.name END,
                            phone = CASE WHEN excluded.phone != '' THEN excluded.phone ELSE newsletter_subscribers.phone END,
                            source = excluded.source
                        """,
                        (em, ed.get("name", ""), ed.get("phone", ""), ed.get("source", "Web Bülten"), ed.get("date", now_iso))
                    )
        except Exception as e:
            logger.debug("SQLite web_orders persist warning: %s", e)

        sorted_orders = sorted(orders_map.values(), key=lambda x: str(x.get("date") or ""), reverse=True)
        subscriber_details = list(emails_map.values())
        all_emails = [x["email"] for x in subscriber_details]

        return {
            "orders": sorted_orders,
            "subscribers": all_emails,
            "subscriberDetails": subscriber_details,
            "members": list(members_map.values())
        }

    def approve_web_order_points(self, order_id: str) -> tuple[bool, str]:
        """Web siparişini ve müşterinin kazandığı (%1 * 100) puanı onaylar; müşterinin hesabında puanı aktif hale getirir."""
        import json as _json
        import urllib.request
        from pathlib import Path

        oid = str(order_id or "").strip()
        if not oid:
            return False, "Sipariş numarası gerekli."

        self._ensure_web_ecommerce_tables()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cust_email = ""
        earned_pts = 0

        # 1. Update SQLite
        try:
            with self.db.get_connection() as conn:
                row = conn.execute("SELECT * FROM web_orders WHERE id = ?", (oid,)).fetchone()
                if row:
                    cust_email = str(row["customer_email"] or "").strip().lower()
                    earned_pts = int(row["earned_points"] or 0)
                    try:
                        raw = _json.loads(row["raw_json"]) if row["raw_json"] else {}
                    except Exception:
                        raw = {}
                    raw["pointsApproved"] = True
                    raw["status"] = "Onaylandı"
                    raw["approvedAt"] = now_str
                    conn.execute(
                        "UPDATE web_orders SET points_approved = 1, status = 'Onaylandı', raw_json = ?, updated_at = ? WHERE id = ?",
                        (_json.dumps(raw, ensure_ascii=False), now_str, oid)
                    )
        except Exception as e:
            logger.debug("SQLite approve_web_order_points warning: %s", e)

        # 2. Update local gallus mama database.json if present
        for gp in [
            Path(r"c:\Users\birdo\OneDrive\Masaüstü\gallus mama\data\database.json"),
            Path(r"c:\Users\birdo\OneDrive\Masaüstü\gallus mama\gallus mama\data\database.json")
        ]:
            if gp.exists():
                try:
                    gdata = _json.loads(gp.read_text(encoding="utf-8"))
                    changed = False
                    for o in gdata.get("orders", []):
                        if o.get("id") == oid:
                            o["pointsApproved"] = True
                            o["status"] = "Onaylandı"
                            o["approvedAt"] = now_str
                            cust_email = cust_email or str((o.get("customer") or {}).get("email") or o.get("email") or "").strip().lower()
                            earned_pts = earned_pts or int(o.get("earnedPoints") or 0)
                            changed = True
                    if changed:
                        gp.write_text(_json.dumps(gdata, ensure_ascii=False, indent=2), encoding="utf-8")
                except Exception:
                    pass

        # 3. Update Firestore ('orders' & 'web_members')
        if self.firestore_db:
            try:
                self.firestore_db.collection("orders").document(oid).set({
                    "pointsApproved": True,
                    "status": "Onaylandı",
                    "approvedAt": now_str
                }, merge=True)
            except Exception:
                pass

        # 4. Notify localhost:3000 and gallusmama.com APIs
        for endpoint in ["http://localhost:3000/api/orders/approve", "https://gallusmama.com/api/orders/approve"]:
            try:
                req_body = _json.dumps({"orderId": oid, "status": "Onaylandı", "email": cust_email}).encode("utf-8")
                req = urllib.request.Request(
                    endpoint,
                    data=req_body,
                    headers={"Content-Type": "application/json", "User-Agent": "StokSatisTakip/1.0"},
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=3.0) as _:
                    pass
            except Exception:
                pass

        return True, f"Sipariş (#{oid}) onaylandı! Müşterinin +{earned_pts} Puanı hesabında aktifleşti."


