import base64
import hashlib
import json
import logging
import os
import secrets
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
            import firebase_admin
            from firebase_admin import credentials, firestore

            if firebase_admin._apps:
                try:
                    self.firestore_db = firestore.client()
                    self.pull_all_from_firebase()
                    return
                except Exception:
                    pass

            cred = None
            env_json = os.environ.get("FIREBASE_CREDENTIALS_JSON")
            env_path = os.environ.get("FIREBASE_CREDENTIALS_PATH")

            candidate_paths = [
                Path(__file__).resolve().parent.parent.parent / "firebase_credentials.json",
                Path.cwd() / "firebase_credentials.json",
                DATA_DIR.parent / "firebase_credentials.json",
                DATA_DIR / "firebase_credentials.json",
            ]
            file_path = next((p for p in candidate_paths if p.exists()), None)

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

            if not cred and file_path and file_path.exists():
                try:
                    c_dict = json.loads(file_path.read_text(encoding="utf-8"))
                    if isinstance(c_dict, dict) and "private_key" in c_dict:
                        c_dict["private_key"] = c_dict["private_key"].replace("\\n", "\n").replace("\\\\n", "\n")
                    cred = credentials.Certificate(c_dict)
                    logger.info(f"Firebase kimlik bilgileri yerel dosyadan ({file_path.name}) yüklendi.")
                except Exception as e:
                    logger.warning(f"Yerel Firebase JSON ayrıştırma uyarısı: {e}")
                    cred = credentials.Certificate(str(file_path))

            if cred:
                firebase_admin.initialize_app(cred)
                self.firestore_db = firestore.client()
                logger.info("Firebase Firestore Cloud bağlantısı başarıyla kuruldu.")
                self.pull_all_from_firebase()
            else:
                logger.info("Firebase kimlik bilgileri bulunamadı, SQLite yerel mod aktif.")
        except Exception as exc:
            logger.warning(f"Firebase Firestore istemcisi çevrimdışı modda başlatıldı: {exc}")

    # ════════════════════════════════════════════════════════════════════
    # BULUTTAN YEREL VERİTABANINA TAM VERİ AKTARIMI (HYDRATION)
    # ════════════════════════════════════════════════════════════════════
    def pull_all_from_firebase(self, user_id: Optional[int] = None) -> int:
        """Firestore üzerindeki tüm verileri (users, categories, products, sales, expenses, settings)
        yerel SQLite veritabanına aktarır. Böylece sunucu yeniden başladığında hiçbir veri kaybolmaz.
        """
        if not self.firestore_db:
            return 0

        pulled_count = 0
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

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
                        cid = d.get("id") or (int(doc.id) if doc.id.isdigit() else None)
                        if not cid and doc.id.isdigit():
                            cid = int(doc.id)
                        if cid and d.get("name"):
                            conn.execute(
                                """
                                INSERT OR REPLACE INTO categories (
                                    id, name, user_id, synced_to_cloud
                                ) VALUES (?, ?, ?, 1)
                                """,
                                (cid, d.get("name"), d.get("user_id")),
                            )
                            pulled_count += 1
                except Exception as e:
                    logger.warning(f"Kategorileri buluttan çekme hatası: {e}")

                # 3. PULL PRODUCTS
                try:
                    p_docs = self.firestore_db.collection("products").stream()
                    for doc in p_docs:
                        d = doc.to_dict()
                        pid = d.get("id") or (int(doc.id) if doc.id.isdigit() else None)
                        if not pid and doc.id.isdigit():
                            pid = int(doc.id)
                        if pid and d.get("name"):
                            cat_id = d.get("category_id") or 1
                            conn.execute(
                                """
                                INSERT OR REPLACE INTO products (
                                    id, user_id, category_id, name, barcode, purchase_price, sale_price,
                                    stock_quantity, critical_stock_level, image_path, is_active, synced_to_cloud, created_at, updated_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                                """,
                                (
                                    pid,
                                    d.get("user_id"),
                                    cat_id,
                                    d.get("name", "Ürün"),
                                    d.get("barcode", f"KOD{pid}"),
                                    float(d.get("purchase_price", 0)),
                                    float(d.get("sale_price", 0)),
                                    int(d.get("stock_quantity", 0)),
                                    int(d.get("critical_stock_level", 5)),
                                    d.get("image_path", ""),
                                    int(d.get("is_active", 1)),
                                    d.get("created_at", now),
                                    d.get("updated_at", now),
                                ),
                            )
                            pulled_count += 1
                except Exception as e:
                    logger.warning(f"Ürünleri buluttan çekme hatası: {e}")

                # 4. PULL SALES & SALE ITEMS
                try:
                    sales_docs = self.firestore_db.collection("sales").stream()
                    for doc in sales_docs:
                        d = doc.to_dict()
                        sid = d.get("id") or (int(doc.id) if doc.id.isdigit() else None)
                        if not sid and doc.id.isdigit():
                            sid = int(doc.id)
                        if sid:
                            conn.execute(
                                """
                                INSERT OR REPLACE INTO sales (
                                    id, user_id, total_amount, item_count, sold_at, note, synced_to_cloud
                                ) VALUES (?, ?, ?, ?, ?, ?, 1)
                                """,
                                (
                                    sid,
                                    d.get("user_id"),
                                    float(d.get("total_amount", 0)),
                                    int(d.get("item_count", 0)),
                                    d.get("sold_at", now),
                                    d.get("note", "Satış"),
                                ),
                            )
                            # Sale items
                            items = d.get("items", [])
                            conn.execute("DELETE FROM sale_items WHERE sale_id = ?", (sid,))
                            for it in items:
                                conn.execute(
                                    """
                                    INSERT INTO sale_items (sale_id, product_id, product_name, barcode, unit_price, quantity, subtotal)
                                    VALUES (?, ?, ?, ?, ?, ?, ?)
                                    """,
                                    (
                                        sid,
                                        it.get("product_id"),
                                        it.get("product_name", ""),
                                        it.get("barcode", ""),
                                        float(it.get("unit_price", 0)),
                                        int(it.get("quantity", 1)),
                                        float(it.get("subtotal", 0)),
                                    ),
                                )
                            pulled_count += 1
                except Exception as e:
                    logger.warning(f"Satışları buluttan çekme hatası: {e}")

                # 5. PULL EXPENSES
                try:
                    exp_docs = self.firestore_db.collection("expenses").stream()
                    for doc in exp_docs:
                        d = doc.to_dict()
                        eid = d.get("id") or (int(doc.id) if doc.id.isdigit() else None)
                        if not eid and doc.id.isdigit():
                            eid = int(doc.id)
                        if eid and d.get("title"):
                            conn.execute(
                                """
                                INSERT OR REPLACE INTO expenses (
                                    id, user_id, title, amount, category, expense_date, note, synced_to_cloud, created_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                                """,
                                (
                                    eid,
                                    d.get("user_id"),
                                    d.get("title"),
                                    float(d.get("amount", 0)),
                                    d.get("category", "Fatura"),
                                    d.get("expense_date", now[:10]),
                                    d.get("note", ""),
                                    d.get("created_at", now),
                                ),
                            )
                            pulled_count += 1
                except Exception as e:
                    logger.warning(f"Giderleri buluttan çekme hatası: {e}")

                # 6. PULL STORE SETTINGS
                try:
                    settings_docs = self.firestore_db.collection("store_settings").stream()
                    for doc in settings_docs:
                        d = doc.to_dict()
                        suid = d.get("user_id") or (int(doc.id) if doc.id.isdigit() else 1)
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO store_settings (user_id, weekday_hours, weekend_hours)
                            VALUES (?, ?, ?)
                            """,
                            (suid, d.get("weekday_hours", "08:00 - 22:00"), d.get("weekend_hours", "09:00 - 23:00")),
                        )
                except Exception as e:
                    logger.warning(f"İşletme ayarlarını buluttan çekme hatası: {e}")

                # 7. UPDATE SQLITE AUTOINCREMENT SEQUENCES
                for tbl in ["users", "categories", "products", "sales", "expenses"]:
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

            logger.info(f"Firestore\'dan {pulled_count} adet kayıt yerel SQLite veritabanına başarıyla senkronize edildi.")
        except Exception as exc:
            logger.error(f"Firestore tam indirme (pull) hatası: {exc}")

        return pulled_count

    # ════════════════════════════════════════════════════════════════════
    # FİREBASE OTOMATİK SENKRONİZASYON (2-WAY OFFLINE-FIRST SYNC)
    # ════════════════════════════════════════════════════════════════════
    def sync_offline_data_with_firebase(self, user_id: Optional[int] = None) -> Dict[str, Any]:
        """Yerel SQLite veritabanında henüz buluta gönderilmemiş (synced_to_cloud = 0) kayıtları
        Firebase Firestore\'a aktarır ve Firestore\'daki güncel verileri yerelle senkronize eder.
        """
        if not self.firestore_db:
            self._init_firebase_optional()

        if not self.firestore_db:
            return {
                "synced": False,
                "reason": "Firebase bağlantısı çevrimdışı. Veriler yerel SQLite üzerinde saklanmaktadır.",
                "pushed": 0,
                "pulled": 0,
            }

        pushed_count = 0

        try:
            with self.db.get_connection() as conn:
                # 1. PUSH UN-SYNCED USERS
                un_users = conn.execute("SELECT * FROM users WHERE synced_to_cloud = 0").fetchall()
                for u in un_users:
                    u_dict = dict(u)
                    u_id = u_dict["id"]
                    u_dict["synced_to_cloud"] = 1
                    try:
                        self.firestore_db.collection("users").document(str(u_id)).set(u_dict)
                        conn.execute("UPDATE users SET synced_to_cloud = 1 WHERE id = ?", (u_id,))
                        pushed_count += 1
                    except Exception as e:
                        logger.warning(f"Kullanıcı {u_id} bulut senkronizasyon hatası: {e}")

                # 2. PUSH UN-SYNCED CATEGORIES
                un_cats = conn.execute("SELECT * FROM categories WHERE synced_to_cloud = 0").fetchall()
                for c in un_cats:
                    c_dict = dict(c)
                    c_id = c_dict["id"]
                    c_dict["synced_to_cloud"] = 1
                    try:
                        self.firestore_db.collection("categories").document(str(c_id)).set(c_dict)
                        conn.execute("UPDATE categories SET synced_to_cloud = 1 WHERE id = ?", (c_id,))
                        pushed_count += 1
                    except Exception as e:
                        logger.warning(f"Kategori {c_id} bulut senkronizasyon hatası: {e}")

                # 3. PUSH UN-SYNCED PRODUCTS
                un_prods = conn.execute(
                    """
                    SELECT p.*, c.name as category_name 
                    FROM products p 
                    LEFT JOIN categories c ON p.category_id = c.id 
                    WHERE p.synced_to_cloud = 0
                    """
                ).fetchall()
                for p in un_prods:
                    p_dict = dict(p)
                    p_id = p_dict["id"]
                    p_dict["synced_to_cloud"] = 1
                    try:
                        self.firestore_db.collection("products").document(str(p_id)).set(p_dict)
                        conn.execute("UPDATE products SET synced_to_cloud = 1 WHERE id = ?", (p_id,))
                        pushed_count += 1
                    except Exception as e:
                        logger.warning(f"Ürün {p_id} bulut senkronizasyon hatası: {e}")

                # 4. PUSH UN-SYNCED SALES
                un_sales = conn.execute("SELECT * FROM sales WHERE synced_to_cloud = 0").fetchall()
                for s in un_sales:
                    s_dict = dict(s)
                    s_id = s_dict["id"]
                    items_rows = conn.execute("SELECT * FROM sale_items WHERE sale_id = ?", (s_id,)).fetchall()
                    s_dict["items"] = [dict(i) for i in items_rows]
                    s_dict["synced_to_cloud"] = 1
                    try:
                        self.firestore_db.collection("sales").document(str(s_id)).set(s_dict)
                        conn.execute("UPDATE sales SET synced_to_cloud = 1 WHERE id = ?", (s_id,))
                        pushed_count += 1
                    except Exception as e:
                        logger.warning(f"Satış {s_id} bulut senkronizasyon hatası: {e}")

                # 5. PUSH UN-SYNCED EXPENSES
                un_exp = conn.execute("SELECT * FROM expenses WHERE synced_to_cloud = 0").fetchall()
                for e in un_exp:
                    e_dict = dict(e)
                    e_id = e_dict["id"]
                    e_dict["synced_to_cloud"] = 1
                    try:
                        self.firestore_db.collection("expenses").document(str(e_id)).set(e_dict)
                        conn.execute("UPDATE expenses SET synced_to_cloud = 1 WHERE id = ?", (e_id,))
                        pushed_count += 1
                    except Exception as ex:
                        logger.warning(f"Gider {e_id} bulut senkronizasyon hatası: {ex}")

            # 6. PULL REMOTE CHANGES
            pulled_count = self.pull_all_from_firebase(user_id=user_id)

            return {
                "synced": True,
                "reason": f"Firebase eşitlendi ({pushed_count} veri buluta aktarıldı, {pulled_count} veri buluttan indirildi).",
                "pushed": pushed_count,
                "pulled": pulled_count,
            }
        except Exception as exc:
            logger.error(f"Senkronizasyon hatası: {exc}")
            return {"synced": False, "reason": str(exc), "pushed": pushed_count, "pulled": 0}

    # ════════════════════════════════════════════════════════════════════
    # KATEGORİ İŞLEMLERİ
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
                "INSERT OR IGNORE INTO categories (name, user_id, synced_to_cloud) VALUES (?, ?, 0)",
                (name_clean, user_id),
            )
            cat_id = cursor.lastrowid
            if not cat_id:
                row = conn.execute(
                    "SELECT id FROM categories WHERE name = ? AND (user_id = ? OR user_id IS NULL OR ? IS NULL)",
                    (name_clean, user_id, user_id),
                ).fetchone()
                cat_id = row["id"] if row else None

        if cat_id and self.firestore_db:
            try:
                self.firestore_db.collection("categories").document(str(cat_id)).set({
                    "id": cat_id,
                    "name": name_clean,
                    "user_id": user_id,
                    "synced_to_cloud": 1,
                })
                with self.db.get_connection() as conn:
                    conn.execute("UPDATE categories SET synced_to_cloud = 1 WHERE id = ?", (cat_id,))
            except Exception:
                pass
        return cat_id

    # ════════════════════════════════════════════════════════════════════
    # ÜRÜN İŞLEMLERİ (KALICI VE KAPSAYICI)
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
            query += " AND (p.user_id = ? OR p.user_id IS NULL)"
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
            query += " AND (p.user_id = ? OR p.user_id IS NULL)"
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
                        image_path = ?, updated_at = ?, user_id = COALESCE(user_id, ?)
                    WHERE id = ?
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
                        user_id,
                        product_id,
                    ),
                )
                pid = product_id
                msg = f"'{name}' ürünü başarıyla güncellendi."
            else:
                # Check duplicate barcode
                if user_id is not None:
                    existing = conn.execute("SELECT id FROM products WHERE barcode = ? AND (user_id = ? OR user_id IS NULL) AND is_active = 1", (barcode, user_id)).fetchone()
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
            "is_active": 1,
            "created_at": now,
            "updated_at": now,
        }
        synced = 0
        if self.firestore_db:
            try:
                prod_data["synced_to_cloud"] = 1
                self.firestore_db.collection("products").document(str(pid)).set(prod_data)
                synced = 1
            except Exception as e:
                logger.warning(f"Firestore ürün bulut kaydı uyarısı: {e}")
                synced = 0

        with self.db.get_connection() as conn:
            conn.execute("UPDATE products SET synced_to_cloud = ? WHERE id = ?", (synced, pid))

        prod_data["synced_to_cloud"] = synced
        return True, msg, prod_data

    def delete_product(self, product_id: int, user_id: Optional[int] = None) -> tuple[bool, str]:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.db.get_connection() as conn:
            query = "UPDATE products SET is_active = 0, synced_to_cloud = 0, updated_at = ? WHERE id = ?"
            params: list = [now, product_id]
            if user_id is not None:
                query += " AND (user_id = ? OR user_id IS NULL)"
                params.append(user_id)
            conn.execute(query, params)

        if self.firestore_db:
            try:
                self.firestore_db.collection("products").document(str(product_id)).update({
                    "is_active": 0,
                    "updated_at": now,
                    "synced_to_cloud": 1
                })
                with self.db.get_connection() as conn:
                    conn.execute("UPDATE products SET synced_to_cloud = 1 WHERE id = ?", (product_id,))
            except Exception as e:
                logger.warning(f"Firestore ürün silme uyarısı: {e}")

        return True, "Ürün stok kataloğundan silindi."

    def update_product_stock(self, product_id: int, stock_quantity: int, user_id: Optional[int] = None) -> tuple[bool, str]:
        if stock_quantity < 0:
            return False, "Stok miktarı negatif olamaz!"

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.db.get_connection() as conn:
            query = "UPDATE products SET stock_quantity = ?, synced_to_cloud = 0, updated_at = ? WHERE id = ?"
            params: list = [stock_quantity, now, product_id]
            if user_id is not None:
                query += " AND (user_id = ? OR user_id IS NULL)"
                params.append(user_id)
            conn.execute(query, params)

        if self.firestore_db:
            try:
                self.firestore_db.collection("products").document(str(product_id)).update({
                    "stock_quantity": stock_quantity,
                    "updated_at": now,
                    "synced_to_cloud": 1
                })
                with self.db.get_connection() as conn:
                    conn.execute("UPDATE products SET synced_to_cloud = 1 WHERE id = ?", (product_id,))
            except Exception as e:
                logger.warning(f"Firestore stok güncelleme uyarısı: {e}")

        return True, f"Stok miktarı {stock_quantity} adet olarak güncellendi."


    # ════════════════════════════════════════════════════════════════════
    # SATIŞ İŞLEMLERİ
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
                # Deduct stock
                conn.execute(
                    "UPDATE products SET stock_quantity = MAX(0, stock_quantity - ?), updated_at = ? WHERE id = ?",
                    (item["quantity"], now, item["product_id"]),
                )

        synced = 0
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
                    "synced_to_cloud": 1,
                })
                synced = 1
            except Exception:
                synced = 0

        with self.db.get_connection() as conn:
            conn.execute("UPDATE sales SET synced_to_cloud = ? WHERE id = ?", (synced, sale_id))

        return True, f"Satış başarıyla tamamlandı. Toplam: {total_amount:.2f} ₺"

    def get_sales_history(
        self,
        user_id: Optional[int] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM sales WHERE 1=1"
        params: list = []
        if user_id is not None:
            query += " AND (user_id = ? OR user_id IS NULL)"
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
        with self.db.get_connection() as conn:
            sales_query = "SELECT COUNT(*) as total_transactions, SUM(total_amount) as total_revenue, SUM(item_count) as total_items FROM sales WHERE 1=1"
            s_params: list = []
            if user_id is not None:
                sales_query += " AND (user_id = ? OR user_id IS NULL)"
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

            # Top 5 Best Selling Products
            top_query = """
                SELECT si.product_name, SUM(si.quantity) as total_qty, SUM(si.subtotal) as total_sales_amount
                FROM sale_items si
                JOIN sales s ON si.sale_id = s.id
                WHERE 1=1
            """
            top_params: list = []
            if user_id is not None:
                top_query += " AND (s.user_id = ? OR s.user_id IS NULL)"
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
                low_query += " AND (user_id = ? OR user_id IS NULL)"
                low_params.append(user_id)
            low_rows = conn.execute(low_query, low_params).fetchall()
            low_stock_items = [dict(r) for r in low_rows]

            # Total Expenses
            exp_query = "SELECT SUM(amount) as total_exp FROM expenses WHERE 1=1"
            exp_params: list = []
            if user_id is not None:
                exp_query += " AND (user_id = ? OR user_id IS NULL)"
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
    # GİDER VE FATURA İŞLEMLERİ
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
            cursor = conn.execute(
                """
                INSERT INTO expenses (user_id, title, amount, category, expense_date, note, synced_to_cloud, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 0, ?)
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
            "created_at": now,
        }

        synced = 0
        if self.firestore_db:
            try:
                exp_data["synced_to_cloud"] = 1
                self.firestore_db.collection("expenses").document(str(eid)).set(exp_data)
                synced = 1
            except Exception:
                synced = 0

        with self.db.get_connection() as conn:
            conn.execute("UPDATE expenses SET synced_to_cloud = ? WHERE id = ?", (synced, eid))

        exp_data["synced_to_cloud"] = synced
        return True, f"'{t_clean}' ({amount:.2f} ₺) gideri başarıyla eklendi.", exp_data

    def get_expenses(
        self,
        user_id: Optional[int] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        with self.db.get_connection() as conn:
            query = "SELECT * FROM expenses WHERE 1=1"
            params: list = []
            if user_id is not None:
                query += " AND (user_id = ? OR user_id IS NULL)"
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
        with self.db.get_connection() as conn:
            uid = user_id or 1
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
                self.firestore_db.collection("store_settings").document(str(uid)).set({
                    "user_id": uid,
                    "weekday_hours": weekday.strip(),
                    "weekend_hours": weekend.strip(),
                })
            except Exception:
                pass
        return True

    # ════════════════════════════════════════════════════════════════════
    # BULUT TABANLI KULLANICI ÜYELİK VE GİRİŞ İŞLEMLERİ (CLOUD AUTH)
    # ════════════════════════════════════════════════════════════════════
    def _clean_phone(self, phone: str) -> str:
        cleaned = "".join(c for c in phone if c.isdigit())
        if cleaned.startswith("0"):
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
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 1. Firestore kontrolü
        if self.firestore_db:
            try:
                users_col = self.firestore_db.collection("users")
                docs = list(users_col.stream())
                for d in docs:
                    data = d.to_dict()
                    d_phone = self._clean_phone(str(data.get("phone", "")))
                    d_email = str(data.get("email", "")).strip().lower()
                    if d_phone == p_clean or d_email == e_clean:
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
                            self.pull_all_from_firebase(user_id=uid)
                            return True, f"Mevcut hesabınız doğrulandı ve '{data['company_name']}' firmasıyla oturum açıldı!", ret_user
                        else:
                            return False, "Bu telefon veya e-posta ile kayıtlı bir hesap zaten var. Lütfen giriş yapınız.", None
            except Exception as e:
                logger.warning(f"Firestore kayıt kontrol uyarısı: {e}")

        # 2. SQLite kontrolü
        with self.db.get_connection() as conn:
            existing_p = conn.execute("SELECT id, password_hash FROM users WHERE phone = ? OR phone = ?", (p_raw, p_clean)).fetchone()
            if existing_p:
                if existing_p["password_hash"] == pass_hash:
                    pass
                else:
                    return False, "Bu telefon numarası ile kayıtlı bir hesap zaten var!", None

            existing_e = conn.execute("SELECT id, password_hash FROM users WHERE LOWER(email) = ?", (e_clean,)).fetchone()
            if existing_e:
                if existing_e["password_hash"] == pass_hash:
                    pass
                else:
                    return False, "Bu e-posta adresi ile kayıtlı bir hesap zaten var!", None

        if (existing_p and existing_p["password_hash"] == pass_hash) or (existing_e and existing_e["password_hash"] == pass_hash):
            return self.login_user(phone_or_email=p_clean if existing_p else e_clean, password=password)

        with self.db.get_connection() as conn:
            max_id = 0
            row = conn.execute("SELECT MAX(id) as max_id FROM users").fetchone()
            if row and row["max_id"]:
                max_id = row["max_id"]

            uid = max_id + 1

            cursor = conn.execute(
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
        if not query_val or not password:
            return False, "Telefon / E-posta ve şifre giriniz!", None

        pass_hash = self._hash_password(password)

        user_dict = None
        # 1. SQLite'da ara
        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE (phone = ? OR phone = ? OR LOWER(email) = LOWER(?)) AND password_hash = ?",
                (query_val, p_clean, query_val, pass_hash),
            ).fetchone()

            if row:
                user_dict = dict(row)
                token = user_dict.get("auth_token")
                if not token or remember_me:
                    token = self._generate_token()
                    conn.execute("UPDATE users SET auth_token = ? WHERE id = ?", (token, user_dict["id"]))
                    user_dict["auth_token"] = token

        if user_dict:
            if self.firestore_db:
                try:
                    self.firestore_db.collection("users").document(str(user_dict["id"])).update({
                        "auth_token": user_dict["auth_token"],
                        "password_hash": pass_hash,
                        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    })
                except Exception:
                    pass
            user_dict.pop("password_hash", None)
            self.pull_all_from_firebase(user_id=user_dict["id"])
            return True, f"Hoş geldiniz, {user_dict['full_name']} ({user_dict['company_name']})", user_dict

        # 2. SQLite'da bulunamazsa Firestore'dan sorgula
        if self.firestore_db:
            try:
                users_col = self.firestore_db.collection("users")
                docs = list(users_col.stream())
                for d in docs:
                    data = d.to_dict()
                    d_phone = self._clean_phone(str(data.get("phone", "")))
                    d_email = str(data.get("email", "")).strip().lower()
                    if d_phone in (p_clean, query_val) or d_email == query_val.lower():
                        saved_hash = data.get("password_hash")
                        if not saved_hash or saved_hash == pass_hash:
                            token = self._generate_token()
                            uid = data.get("id") or (int(d.id) if d.id.isdigit() else 1)
                            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
                            self.pull_all_from_firebase(user_id=uid)
                            return True, f"Hoş geldiniz, {user_dict.get('full_name', '')} ({user_dict.get('company_name', '')})", user_dict
            except Exception as e:
                logger.warning(f"Firestore oturum kontrol hatası: {e}")

        return False, "Telefon/E-posta veya şifre hatalı!", None

    def verify_token(self, token: str) -> Optional[Dict[str, Any]]:
        if not token or not token.strip():
            return None
        tok = token.strip()

        # 1. SQLite kontrolü
        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT id, company_name, full_name, phone, email, auth_token FROM users WHERE auth_token = ?",
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
                    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
