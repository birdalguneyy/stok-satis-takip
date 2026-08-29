import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import DATA_DIR, DB_PATH
from app.database.connection import Database
from app.database.migrations import run_migrations
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
        try:
            run_migrations()
        except Exception as exc:
            logger.warning(f"Migration otomatik çalıştırma uyarısı: {exc}")
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
                    if not raw_json.startswith("{"):
                        raw_json = base64.b64decode(raw_json).decode("utf-8")
                    c_dict = json.loads(raw_json)
                    if isinstance(c_dict, dict) and "private_key" in c_dict:
                        c_dict["private_key"] = c_dict["private_key"].replace("\\n", "\n").replace("\\\\n", "\n")
                    cred = credentials.Certificate(c_dict)
                    logger.info("Firebase kimlik bilgileri ortam değişkeninden (JSON) yüklendi.")
                except Exception as e:
                    logger.warning(f"Ortam değişkeninden Firebase JSON ayrıştırma hatası: {e}")

            if not cred and env_path and Path(env_path).exists():
                cred = credentials.Certificate(env_path)
                logger.info(f"Firebase kimlik bilgileri ortam yolundan ({env_path}) yüklendi.")

            if not cred and file_path.exists():
                try:
                    c_dict = json.loads(file_path.read_text(encoding="utf-8"))
                    if isinstance(c_dict, dict) and "private_key" in c_dict:
                        c_dict["private_key"] = c_dict["private_key"].replace("\\n", "\n").replace("\\\\n", "\n")
                    cred = credentials.Certificate(c_dict)
                    logger.info("Firebase kimlik bilgileri yerel dosyadan (firebase_credentials.json) yüklendi.")
                except Exception as e:
                    logger.warning(f"Yerel Firebase JSON ayrıştırma uyarısı: {e}")
                    cred = credentials.Certificate(str(file_path))

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

    def get_sales_history(
        self,
        user_id: Optional[int] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Yapılan tüm satış hareketlerini tarih aralığına göre kalemleriyle getirir."""
        query = "SELECT * FROM sales WHERE 1=1"
        params: list = []
        if user_id is not None:
            query += " AND user_id = ?"
            params.append(user_id)
        if start_date:
            query += " AND sold_at >= ?"
            params.append(start_date + " 00:00:00")
        if end_date:
            query += " AND sold_at <= ?"
            params.append(end_date + " 23:59:59")
        query += " ORDER BY id DESC LIMIT 200"

        sales = []
        with self.db.get_connection() as conn:
            rows = conn.execute(query, params).fetchall()
            for r in rows:
                s_dict = dict(r)
                items_rows = conn.execute(
                    "SELECT * FROM sale_items WHERE sale_id = ?", (s_dict["id"],)
                ).fetchall()
                s_dict["items"] = [dict(i) for i in items_rows]
                sales.append(s_dict)
        return sales

    def get_sales_analytics(
        self,
        user_id: Optional[int] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Profesyonel satış istatistikleri, giderler, net kar ve kritik stok analizi sunar."""
        with self.db.get_connection() as conn:
            sales_query = "SELECT COUNT(*) as total_transactions, SUM(total_amount) as total_revenue, SUM(item_count) as total_items FROM sales WHERE 1=1"
            s_params: list = []
            if user_id is not None:
                sales_query += " AND user_id = ?"
                s_params.append(user_id)
            if start_date:
                sales_query += " AND sold_at >= ?"
                s_params.append(start_date + " 00:00:00")
            if end_date:
                sales_query += " AND sold_at <= ?"
                s_params.append(end_date + " 23:59:59")

            s_row = conn.execute(sales_query, s_params).fetchone()
            total_tx = s_row["total_transactions"] if s_row else 0
            total_rev = s_row["total_revenue"] or 0.0
            total_items = s_row["total_items"] or 0
            avg_cart = (total_rev / total_tx) if total_tx > 0 else 0.0

            # Top 5 Best Selling Products in date range
            top_query = """
                SELECT si.product_name, SUM(si.quantity) as total_qty, SUM(si.subtotal) as total_sales_amount
                FROM sale_items si
                JOIN sales s ON si.sale_id = s.id
                WHERE 1=1
            """
            top_params: list = []
            if user_id is not None:
                top_query += " AND s.user_id = ?"
                top_params.append(user_id)
            if start_date:
                top_query += " AND s.sold_at >= ?"
                top_params.append(start_date + " 00:00:00")
            if end_date:
                top_query += " AND s.sold_at <= ?"
                top_params.append(end_date + " 23:59:59")
            top_query += " GROUP BY si.product_name ORDER BY total_qty DESC LIMIT 5"
            top_rows = conn.execute(top_query, top_params).fetchall()
            top_products = [dict(r) for r in top_rows]

            # Low Stock Items
            low_query = "SELECT id, name, barcode, stock_quantity, critical_stock_level FROM products WHERE is_active = 1 AND stock_quantity <= critical_stock_level"
            low_params: list = []
            if user_id is not None:
                low_query += " AND user_id = ?"
                low_params.append(user_id)
            low_rows = conn.execute(low_query, low_params).fetchall()
            low_stock_items = [dict(r) for r in low_rows]

            # Total Expenses in date range
            exp_query = "SELECT SUM(amount) as total_exp FROM expenses WHERE 1=1"
            exp_params: list = []
            if user_id is not None:
                exp_query += " AND user_id = ?"
                exp_params.append(user_id)
            if start_date:
                exp_query += " AND expense_date >= ?"
                exp_params.append(start_date)
            if end_date:
                exp_query += " AND expense_date <= ?"
                exp_params.append(end_date)
            
            exp_row = conn.execute(exp_query, exp_params).fetchone()
            total_exp = exp_row["total_exp"] or 0.0 if exp_row else 0.0
            net_profit = total_rev - total_exp

            return {
                "total_revenue": round(total_rev, 2),
                "total_items_sold": total_items,
                "total_transactions": total_tx,
                "average_cart": round(avg_cart, 2),
                "total_expenses": round(total_exp, 2),
                "net_profit": round(net_profit, 2),
                "top_products": top_products,
                "low_stock_items": low_stock_items,
            }

    # ════════════════════════════════════════════════════════════════════
    # FATURA VE GİDER TAKİBİ (ELEKTRİK, SU, İNTERNET, KİRA)
    # ════════════════════════════════════════════════════════════════════
    def add_expense(
        self,
        title: str,
        amount: float,
        category: str = "Fatura",
        expense_date: Optional[str] = None,
        note: str = "",
        user_id: Optional[int] = None,
    ) -> tuple[bool, str, Optional[Dict[str, Any]]]:
        t_clean = title.strip()
        if not t_clean or amount <= 0:
            return False, "Lütfen geçerli bir gider adı ve tutar giriniz!", None

        exp_date = expense_date or datetime.now().strftime("%Y-%m-%d")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with self.db.get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS expenses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    title TEXT NOT NULL,
                    amount REAL NOT NULL,
                    category TEXT DEFAULT 'Fatura',
                    expense_date TEXT NOT NULL,
                    note TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            cursor = conn.execute(
                """
                INSERT INTO expenses (user_id, title, amount, category, expense_date, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, t_clean, amount, category.strip(), exp_date, note.strip(), now),
            )
            eid = cursor.lastrowid
            exp_data = {
                "id": eid,
                "user_id": user_id,
                "title": t_clean,
                "amount": amount,
                "category": category,
                "expense_date": exp_date,
                "note": note,
            }

            if self.firestore_db:
                try:
                    self.firestore_db.collection("expenses").document(str(eid)).set(exp_data)
                except Exception:
                    pass

            return True, f"'{t_clean}' ({amount:.2f} ₺) gideri başarıyla eklendi.", exp_data

    def get_expenses(
        self,
        user_id: Optional[int] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        with self.db.get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS expenses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    title TEXT NOT NULL,
                    amount REAL NOT NULL,
                    category TEXT DEFAULT 'Fatura',
                    expense_date TEXT NOT NULL,
                    note TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            query = "SELECT * FROM expenses WHERE 1=1"
            params: list = []
            if user_id is not None:
                query += " AND user_id = ?"
                params.append(user_id)
            if start_date:
                query += " AND expense_date >= ?"
                params.append(start_date)
            if end_date:
                query += " AND expense_date <= ?"
                params.append(end_date)
            query += " ORDER BY expense_date DESC, id DESC LIMIT 100"

            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def get_store_hours(self, user_id: Optional[int] = None) -> Dict[str, str]:
        """İşletme çalışma saatlerini getirir."""
        with self.db.get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS store_settings (
                    user_id INTEGER PRIMARY KEY,
                    weekday_hours TEXT,
                    weekend_hours TEXT
                )
                """
            )
            uid = user_id or 1
            row = conn.execute("SELECT weekday_hours, weekend_hours FROM store_settings WHERE user_id = ?", (uid,)).fetchone()
            if row:
                return {
                    "weekday": row["weekday_hours"] or "08:00 - 22:00",
                    "weekend": row["weekend_hours"] or "09:00 - 23:00",
                }
            return {"weekday": "08:00 - 22:00", "weekend": "09:00 - 23:00"}

    def save_store_hours(self, weekday: str, weekend: str, user_id: Optional[int] = None) -> bool:
        """İşletme çalışma saatlerini kaydeder."""
        with self.db.get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS store_settings (
                    user_id INTEGER PRIMARY KEY,
                    weekday_hours TEXT,
                    weekend_hours TEXT
                )
                """
            )
            uid = user_id or 1
            conn.execute(
                """
                INSERT INTO store_settings (user_id, weekday_hours, weekend_hours)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET weekday_hours = excluded.weekday_hours, weekend_hours = excluded.weekend_hours
                """,
                (uid, weekday.strip(), weekend.strip()),
            )
            return True

    # ════════════════════════════════════════════════════════════════════
    # KULLANICI ÜYELİK VE GİRİŞ İŞLEMLERİ (AUTH)
    # ════════════════════════════════════════════════════════════════════
    def _clean_phone(self, phone: str) -> str:
        """Telefon numarasından boşluk, tire ve baştaki 0'ı temizler (Örn: 0532 123 4567 -> 5321234567)."""
        cleaned = "".join(c for c in phone if c.isdigit())
        if cleaned.startswith("0"):
            cleaned = cleaned[1:]
        return cleaned

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
        p_raw = phone.strip()
        p_clean = self._clean_phone(p_raw)
        e_clean = email.strip().lower()
        if not c_name or not f_name or not p_raw or not e_clean or not password:
            return False, "Lütfen tüm zorunlu alanları doldurun!", None

        pass_hash = self._hash_password(password)
        token = self._generate_token()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with self.db.get_connection() as conn:
            # Check duplicate phone or email
            existing_p = conn.execute("SELECT id FROM users WHERE phone = ? OR phone = ?", (p_raw, p_clean)).fetchone()
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
        p_clean = self._clean_phone(query_val)
        if not query_val or not password:
            return False, "Telefon / E-posta ve şifre giriniz!", None

        pass_hash = self._hash_password(password)
        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE (phone = ? OR phone = ? OR LOWER(email) = LOWER(?)) AND password_hash = ?",
                (query_val, p_clean, query_val, pass_hash),
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
