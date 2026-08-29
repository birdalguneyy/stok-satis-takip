import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import DATA_DIR, DB_PATH
from app.database.connection import Database
from app.models.product import Product

logger = logging.getLogger(__name__)

# Cloud storage & data directory setup
UPLOADS_DIR = DATA_DIR / "uploads" / "products"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


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
        self.firestore_db = None
        self._init_firebase_optional()
        self._initialized = True

    def _init_firebase_optional(self) -> None:
        """Firebase Admin / Firestore bağlantısını dondurmadan arka planda dener."""
        try:
            import base64
            import firebase_admin
            from firebase_admin import credentials, firestore

            if firebase_admin._apps:
                self.firestore_db = firestore.client()
                return

            cred = None
            env_json = os.environ.get("FIREBASE_CREDENTIALS_JSON")
            env_path = os.environ.get("FIREBASE_CREDENTIALS_PATH")
            file_path = Path(__file__).parent.parent.parent / "firebase_credentials.json"

            if env_json and env_json.strip():
                raw_json = env_json.strip()
                try:
                    # Check if base64 encoded
                    if not raw_json.startswith("{"):
                        raw_json = base64.b64decode(raw_json).decode("utf-8")
                    cred_dict = json.loads(raw_json)
                    cred = credentials.Certificate(cred_dict)
                    logger.info("Firebase kimlik bilgileri ortam değişkeninden (JSON) yüklendi.")
                except Exception as e:
                    logger.warning(f"Ortam değişkeninden Firebase JSON ayrıştırma hatası: {e}")

            if not cred and env_path and Path(env_path).exists():
                cred = credentials.Certificate(env_path)
                logger.info(f"Firebase kimlik bilgileri ortam yolundan ({env_path}) yüklendi.")

            if not cred and file_path.exists():
                cred = credentials.Certificate(str(file_path))
                logger.info("Firebase kimlik bilgileri yeral dosyadan (firebase_credentials.json) yüklendi.")

            if cred:
                firebase_admin.initialize_app(cred)
                self.firestore_db = firestore.client()
                logger.info("Firebase Firestore Cloud bağlantısı başarıyla kuruldu.")
            else:
                logger.info("Firebase kimlik bilgileri (FIREBASE_CREDENTIALS_JSON / firebase_credentials.json) bulunamadı, SQLite hibrit mod aktif.")
        except Exception as exc:
            logger.warning(f"Firebase Firestore istemcisi çevrimdışı modda başlatıldı: {exc}")

    # ════════════════════════════════════════════════════════════════════
    # KATEGORİ İŞLEMLERİ (FİRMA BAZLI İZOLE)
    # ════════════════════════════════════════════════════════════════════
    def get_categories(self, user_id: Optional[int] = None) -> List[Dict[str, Any]]:
        with self.db.get_connection() as conn:
            rows = conn.execute(
                "SELECT id, name FROM categories WHERE (user_id = ? OR user_id IS NULL OR ? IS NULL) ORDER BY name ASC",
                (user_id, user_id),
            ).fetchall()
            return [{"id": r["id"], "name": r["name"]} for r in rows]

    def add_category(self, name: str, user_id: Optional[int] = None) -> Optional[int]:
        name_clean = name.strip()
        if not name_clean:
            return None
        with self.db.get_connection() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO categories (name, user_id) VALUES (?, ?)",
                (name_clean, user_id),
            )
            cat_id = cursor.lastrowid
            if cat_id:
                if self.firestore_db:
                    try:
                        self.firestore_db.collection("categories").document(str(cat_id)).set({"name": name_clean, "user_id": user_id})
                    except Exception:
                        pass
                return cat_id
            row = conn.execute(
                "SELECT id FROM categories WHERE name = ? AND (user_id = ? OR user_id IS NULL OR ? IS NULL)",
                (name_clean, user_id, user_id),
            ).fetchone()
            return row["id"] if row else None

    # ════════════════════════════════════════════════════════════════════
    # ÜRÜN İŞLEMLERİ (FİRMA BAZLI İZOLE)
    # ════════════════════════════════════════════════════════════════════
    def get_products(self, user_id: Optional[int] = None, search: str = "") -> List[Dict[str, Any]]:
        query = """
            SELECT p.*, c.name as category_name
            FROM products p
            LEFT JOIN categories c ON p.category_id = c.id
            WHERE p.is_active = 1
        """
        params: list = []
        if user_id is not None:
            query += " AND p.user_id = ?"
            params.append(user_id)

        if search and search.strip():
            s = f"%{search.strip()}%"
            query += " AND (p.name LIKE ? OR p.barcode LIKE ? OR c.name LIKE ?)"
            params.extend([s, s, s])

        query += " ORDER BY p.name ASC"

        with self.db.get_connection() as conn:
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def get_product_by_barcode(self, barcode: str, user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        query = """
            SELECT p.*, c.name as category_name
            FROM products p
            LEFT JOIN categories c ON p.category_id = c.id
            WHERE p.barcode = ? AND p.is_active = 1
        """
        params: list = [barcode.strip()]
        if user_id is not None:
            query += " AND p.user_id = ?"
            params.append(user_id)

        with self.db.get_connection() as conn:
            row = conn.execute(query, params).fetchone()
            return dict(row) if row else None

    def save_product(
        self,
        name: str,
        barcode: str,
        category_name: str,
        purchase_price: float,
        sale_price: float,
        stock_quantity: int,
        critical_stock_level: int = 5,
        image_path: Optional[str] = None,
        product_id: Optional[int] = None,
        user_id: Optional[int] = None,
    ) -> tuple[bool, str, Optional[Dict[str, Any]]]:

        cat_id = self.add_category(category_name, user_id=user_id) or 1
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with self.db.get_connection() as conn:
            if product_id:
                # Update existing product
                conn.execute(
                    """
                    UPDATE products
                    SET category_id = ?, name = ?, barcode = ?, purchase_price = ?,
                        sale_price = ?, stock_quantity = ?, critical_stock_level = ?,
                        image_path = ?, updated_at = ?
                    WHERE id = ? AND (user_id = ? OR user_id IS NULL OR ? IS NULL)
                    """,
                    (
                        cat_id,
                        name,
                        barcode,
                        purchase_price,
                        sale_price,
                        stock_quantity,
                        critical_stock_level,
                        image_path or "",
                        now,
                        product_id,
                        user_id,
                        user_id,
                    ),
                )
                pid = product_id
                msg = f"'{name}' ürünü başarıyla güncellendi."
            else:
                # Check duplicate barcode for this specific user/firm
                if user_id is not None:
                    existing = conn.execute("SELECT id FROM products WHERE barcode = ? AND user_id = ? AND is_active = 1", (barcode, user_id)).fetchone()
                else:
                    existing = conn.execute("SELECT id FROM products WHERE barcode = ? AND is_active = 1", (barcode,)).fetchone()

                if existing:
                    return False, f"'{barcode}' barkodlu bir ürününüz zaten mevcut!", None

                cursor = conn.execute(
                    """
                    INSERT INTO products (
                        user_id, category_id, name, barcode, purchase_price, sale_price,
                        stock_quantity, critical_stock_level, image_path, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        cat_id,
                        name,
                        barcode,
                        purchase_price,
                        sale_price,
                        stock_quantity,
                        critical_stock_level,
                        image_path or "",
                        now,
                        now,
                    ),
                )
                pid = cursor.lastrowid
                msg = f"'{name}' ürünü başarıyla eklendi."

            prod_data = {
                "id": pid,
                "user_id": user_id,
                "category_id": cat_id,
                "category_name": category_name,
                "name": name,
                "barcode": barcode,
                "purchase_price": purchase_price,
                "sale_price": sale_price,
                "stock_quantity": stock_quantity,
                "critical_stock_level": critical_stock_level,
                "image_path": image_path or "",
                "updated_at": now,
            }
            if self.firestore_db:
                try:
                    self.firestore_db.collection("products").document(str(pid)).set(prod_data)
                except Exception as e:
                    logger.warning(f"Firestore cloud save error: {e}")

            return True, msg, prod_data

    # ════════════════════════════════════════════════════════════════════
    # SATIŞ İŞLEMLERİ (FİRMA BAZLI İZOLE)
    # ════════════════════════════════════════════════════════════════════
    def add_sale(
        self,
        cart_items: List[Dict[str, Any]],
        note: str = "Mobil/PC Satış",
        user_id: Optional[int] = None,
    ) -> tuple[bool, str]:
        if not cart_items:
            return False, "Sepet boş!"

        total_amount = sum(item["subtotal"] for item in cart_items)
        item_count = sum(item["quantity"] for item in cart_items)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with self.db.get_connection() as conn:
            cursor = conn.execute(
                "INSERT INTO sales (user_id, total_amount, item_count, sold_at, note) VALUES (?, ?, ?, ?, ?)",
                (user_id, total_amount, item_count, now, note),
            )
            sale_id = cursor.lastrowid

            for item in cart_items:
                conn.execute(
                    """
                    INSERT INTO sale_items (sale_id, product_id, product_name, barcode, unit_price, quantity, subtotal)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sale_id,
                        item["product_id"],
                        item["product_name"],
                        item["barcode"],
                        item["unit_price"],
                        item["quantity"],
                        item["subtotal"],
                    ),
                )
                # Deduct stock for this product
                conn.execute(
                    "UPDATE products SET stock_quantity = MAX(0, stock_quantity - ?), updated_at = ? WHERE id = ?",
                    (item["quantity"], now, item["product_id"]),
                )

            # Sync sale to Firestore
            if self.firestore_db:
                try:
                    self.firestore_db.collection("sales").document(str(sale_id)).set({
                        "id": sale_id,
                        "user_id": user_id,
                        "total_amount": total_amount,
                        "item_count": item_count,
                        "sold_at": now,
                        "note": note,
                        "items": cart_items,
                    })
                except Exception:
                    pass

            return True, f"Satış başarıyla tamamlandı. Toplam: {total_amount:.2f} ₺"

    # ════════════════════════════════════════════════════════════════════
    # KULLANICI ÜYELİK VE GİRİŞ İŞLEMLERİ (AUTH)
    # ════════════════════════════════════════════════════════════════════
    def _hash_password(self, password: str) -> str:
        import hashlib
        return hashlib.sha256(f"stok_salt_{password}".encode("utf-8")).hexdigest()

    def _generate_token(self) -> str:
        import secrets
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
        p_clean = phone.strip()
        e_clean = email.strip().lower()
        if not c_name or not f_name or not p_clean or not e_clean or not password:
            return False, "Lütfen tüm zorunlu alanları doldurun!", None

        pass_hash = self._hash_password(password)
        token = self._generate_token()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with self.db.get_connection() as conn:
            # Check duplicate phone or email
            existing_p = conn.execute("SELECT id FROM users WHERE phone = ?", (p_clean,)).fetchone()
            if existing_p:
                return False, "Bu telefon numarası ile kayıtlı bir hesap zaten var!", None

            existing_e = conn.execute("SELECT id FROM users WHERE email = ?", (e_clean,)).fetchone()
            if existing_e:
                return False, "Bu e-posta adresi ile kayıtlı bir hesap zaten var!", None

            cursor = conn.execute(
                """
                INSERT INTO users (company_name, full_name, phone, email, password_hash, auth_token, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (c_name, f_name, p_clean, e_clean, pass_hash, token, now, now),
            )
            uid = cursor.lastrowid

            user_data = {
                "id": uid,
                "company_name": c_name,
                "full_name": f_name,
                "phone": p_clean,
                "email": e_clean,
                "auth_token": token,
            }

            if self.firestore_db:
                try:
                    self.firestore_db.collection("users").document(str(uid)).set(user_data)
                except Exception:
                    pass

            return True, f"Tebrikler '{c_name}' firması ile hesabınız başarıyla oluşturuldu!", user_data

    def login_user(self, phone_or_email: str, password: str, remember_me: bool = True) -> tuple[bool, str, Optional[Dict[str, Any]]]:
        query_val = phone_or_email.strip()
        if not query_val or not password:
            return False, "Telefon / E-posta ve şifre giriniz!", None

        pass_hash = self._hash_password(password)
        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE (phone = ? OR LOWER(email) = LOWER(?)) AND password_hash = ?",
                (query_val, query_val, pass_hash),
            ).fetchone()

            if not row:
                return False, "Telefon/E-posta veya şifre hatalı!", None

            user_dict = dict(row)
            token = user_dict.get("auth_token")
            if not token or remember_me:
                token = self._generate_token()
                conn.execute("UPDATE users SET auth_token = ? WHERE id = ?", (token, user_dict["id"]))
                user_dict["auth_token"] = token

            user_dict.pop("password_hash", None)
            return True, f"Hoş geldiniz, {user_dict['full_name']} ({user_dict['company_name']})", user_dict

    def verify_token(self, token: str) -> Optional[Dict[str, Any]]:
        if not token or not token.strip():
            return None
        with self.db.get_connection() as conn:
            row = conn.execute("SELECT id, company_name, full_name, phone, email, auth_token FROM users WHERE auth_token = ?", (token.strip(),)).fetchone()
            return dict(row) if row else None
