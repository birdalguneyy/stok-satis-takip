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
        # Clean orphan rows from older single-user versions by attaching them to primary user 1
        try:
            with self.db.get_connection() as conn:
                conn.execute("UPDATE products SET user_id = 1 WHERE user_id IS NULL")
                conn.execute("UPDATE categories SET user_id = 1 WHERE user_id IS NULL")
                conn.execute("UPDATE sales SET user_id = 1 WHERE user_id IS NULL")
                conn.execute("UPDATE expenses SET user_id = 1 WHERE user_id IS NULL")
                conn.execute("UPDATE store_settings SET user_id = 1 WHERE user_id IS NULL")
        except Exception:
            pass

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
        """Firestore üzerindeki verileri yerel SQLite veritabanına aktarır.
        Eğer user_id belirtilmişse SADECE o kullanıcıya ait verileri çeker (Kullanıcılar Arası Tam İzolasyon!).
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
                        cid = d.get("id")
                        if not cid and doc.id.isdigit():
                            cid = int(doc.id)
                        elif not cid and "_c" in doc.id:
                            try:
                                cid = int(doc.id.split("_c")[-1])
                            except Exception:
                                pass
                        doc_user_id = d.get("user_id")
                        if user_id is not None and doc_user_id is not None and doc_user_id != user_id:
                            continue
                        final_uid = doc_user_id if doc_user_id is not None else (user_id or 1)
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
                        d = doc.to_dict()
                        pid = d.get("id")
                        if not pid and doc.id.isdigit():
                            pid = int(doc.id)
                        elif not pid and "_p" in doc.id:
                            try:
                                pid = int(doc.id.split("_p")[-1])
                            except Exception:
                                pass
                        doc_user_id = d.get("user_id")
                        if user_id is not None and doc_user_id is not None and doc_user_id != user_id:
                            continue
                        final_uid = doc_user_id if doc_user_id is not None else (user_id or 1)
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
                                    final_uid,
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
                        sid = d.get("id")
                        if not sid and doc.id.isdigit():
                            sid = int(doc.id)
                        elif not sid and "_s" in doc.id:
                            try:
                                sid = int(doc.id.split("_s")[-1])
                            except Exception:
                                pass
                        doc_user_id = d.get("user_id")
                        if user_id is not None and doc_user_id is not None and doc_user_id != user_id:
                            continue
                        final_uid = doc_user_id if doc_user_id is not None else (user_id or 1)
                        if sid:
                            conn.execute(
                                """
                                INSERT OR REPLACE INTO sales (
                                    id, user_id, total_amount, item_count, sold_at, note, channel, synced_to_cloud
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                                """,
                                (
                                    sid,
                                    final_uid,
                                    float(d.get("total_amount", 0)),
                                    int(d.get("item_count", 0)),
                                    d.get("sold_at", now),
                                    d.get("note", "Satış"),
                                    d.get("channel", "magaza"),
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
                        eid = d.get("id")
                        if not eid and doc.id.isdigit():
                            eid = int(doc.id)
                        elif not eid and "_e" in doc.id:
                            try:
                                eid = int(doc.id.split("_e")[-1])
                            except Exception:
                                pass
                        doc_user_id = d.get("user_id")
                        if user_id is not None and doc_user_id is not None and doc_user_id != user_id:
                            continue
                        final_uid = doc_user_id if doc_user_id is not None else (user_id or 1)
                        if eid and d.get("title"):
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
                                    d.get("expense_date", now[:10]),
                                    d.get("note", ""),
                                    d.get("created_at", now),
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
                        if user_id is not None and suid != user_id:
                            continue
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

            logger.info(f"Firestore'dan {pulled_count} adet kayıt yerel SQLite veritabanına başarıyla senkronize edildi.")
        except Exception as exc:
            logger.error(f"Firestore tam indirme (pull) hatası: {exc}")

        return pulled_count

    # ════════════════════════════════════════════════════════════════════
    # FİREBASE OTOMATİK SENKRONİZASYON (2-WAY OFFLINE-FIRST SYNC)
    # ════════════════════════════════════════════════════════════════════
    def sync_offline_data_with_firebase(self, user_id: Optional[int] = None) -> Dict[str, Any]:
        """Yerel SQLite veritabanında henüz buluta gönderilmemiş (synced_to_cloud = 0) kayıtları
        Firebase Firestore'a aktarır ve Firestore'daki güncel verileri yerelle senkronize eder.
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

                # 2. PUSH UN-SYNCED CATEGORIES (User Scoped Doc ID: u{uid}_c{cid})
                cat_query = "SELECT * FROM categories WHERE synced_to_cloud = 0"
                cat_params = []
                if user_id is not None:
                    cat_query += " AND user_id = ?"
                    cat_params.append(user_id)
                un_cats = conn.execute(cat_query, cat_params).fetchall()
                for c in un_cats:
                    c_dict = dict(c)
                    c_id = c_dict["id"]
                    c_uid = c_dict.get("user_id") or 1
                    c_dict["synced_to_cloud"] = 1
                    doc_id = f"u{c_uid}_c{c_id}"
                    try:
                        self.firestore_db.collection("categories").document(doc_id).set(c_dict)
                        conn.execute("UPDATE categories SET synced_to_cloud = 1 WHERE id = ?", (c_id,))
                        pushed_count += 1
                    except Exception as e:
                        logger.warning(f"Kategori {c_id} bulut senkronizasyon hatası: {e}")

                # 3. PUSH UN-SYNCED PRODUCTS (User Scoped Doc ID: u{uid}_p{pid})
                prod_query = """
                    SELECT p.*, c.name as category_name 
                    FROM products p 
                    LEFT JOIN categories c ON p.category_id = c.id 
                    WHERE p.synced_to_cloud = 0
                """
                prod_params = []
                if user_id is not None:
                    prod_query += " AND p.user_id = ?"
                    prod_params.append(user_id)
                un_prods = conn.execute(prod_query, prod_params).fetchall()
                for p in un_prods:
                    p_dict = dict(p)
                    p_id = p_dict["id"]
                    p_uid = p_dict.get("user_id") or 1
                    p_dict["synced_to_cloud"] = 1
                    doc_id = f"u{p_uid}_p{p_id}"
                    try:
                        self.firestore_db.collection("products").document(doc_id).set(p_dict)
                        conn.execute("UPDATE products SET synced_to_cloud = 1 WHERE id = ?", (p_id,))
                        pushed_count += 1
                    except Exception as e:
                        logger.warning(f"Ürün {p_id} bulut senkronizasyon hatası: {e}")

                # 4. PUSH UN-SYNCED SALES (User Scoped Doc ID: u{uid}_s{sid})
                sales_query = "SELECT * FROM sales WHERE synced_to_cloud = 0"
                sales_params = []
                if user_id is not None:
                    sales_query += " AND user_id = ?"
                    sales_params.append(user_id)
                un_sales = conn.execute(sales_query, sales_params).fetchall()
                for s in un_sales:
                    s_dict = dict(s)
                    s_id = s_dict["id"]
                    s_uid = s_dict.get("user_id") or 1
                    items_rows = conn.execute("SELECT * FROM sale_items WHERE sale_id = ?", (s_id,)).fetchall()
                    s_dict["items"] = [dict(i) for i in items_rows]
                    s_dict["synced_to_cloud"] = 1
                    doc_id = f"u{s_uid}_s{s_id}"
                    try:
                        self.firestore_db.collection("sales").document(doc_id).set(s_dict)
                        conn.execute("UPDATE sales SET synced_to_cloud = 1 WHERE id = ?", (s_id,))
                        pushed_count += 1
                    except Exception as e:
                        logger.warning(f"Satış {s_id} bulut senkronizasyon hatası: {e}")

                # 5. PUSH UN-SYNCED EXPENSES (User Scoped Doc ID: u{uid}_e{eid})
                exp_query = "SELECT * FROM expenses WHERE synced_to_cloud = 0"
                exp_params = []
                if user_id is not None:
                    exp_query += " AND user_id = ?"
                    exp_params.append(user_id)
                un_exp = conn.execute(exp_query, exp_params).fetchall()
                for e in un_exp:
                    e_dict = dict(e)
                    e_id = e_dict["id"]
                    e_uid = e_dict.get("user_id") or 1
                    e_dict["synced_to_cloud"] = 1
                    doc_id = f"u{e_uid}_e{e_id}"
                    try:
                        self.firestore_db.collection("expenses").document(doc_id).set(e_dict)
                        conn.execute("UPDATE expenses SET synced_to_cloud = 1 WHERE id = ?", (e_id,))
                        pushed_count += 1
                    except Exception as ex:
                        logger.warning(f"Gider {e_id} bulut senkronizasyon hatası: {ex}")

                # 6. PUSH GEMINI API KEY IF LOCAL EXISTS
                key_file = DATA_DIR / "gemini_key.txt"
                if key_file.exists():
                    try:
                        k = key_file.read_text(encoding="utf-8").strip()
                        if k:
                            self.firestore_db.collection("system_settings").document("gemini").set({
                                "api_key": k,
                                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            }, merge=True)
                            pushed_count += 1
                    except Exception as ex:
                        logger.warning(f"Gemini API key Firestore aktarma uyarısı: {ex}")

            # 7. PULL REMOTE CHANGES (Strictly for this user!)
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

    def _resolve_user_id(self, user_id: Optional[int] = None) -> Optional[int]:
        if user_id is not None:
            try:
                with self.db.get_connection() as conn:
                    row = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
                    if row:
                        return user_id
            except Exception:
                return user_id
        try:
            with self.db.get_connection() as conn:
                row = conn.execute("SELECT id FROM users ORDER BY id ASC LIMIT 1").fetchone()
                if row:
                    return row["id"]
        except Exception:
            pass
        return None

    # ════════════════════════════════════════════════════════════════════
    # KATEGORİ İŞLEMLERİ (KULLANICIYA ÖZEL İZOLE)
    # ════════════════════════════════════════════════════════════════════
    def get_categories(self, user_id: Optional[int] = None) -> List[Dict[str, Any]]:
        uid = self._resolve_user_id(user_id) or 1
        with self.db.get_connection() as conn:
            rows = conn.execute(
                "SELECT id, name FROM categories WHERE user_id = ? ORDER BY name ASC",
                (uid,),
            ).fetchall()
            return [{"id": r["id"], "name": r["name"]} for r in rows]

    def add_category(self, name: str, user_id: Optional[int] = None) -> Optional[int]:
        name_clean = name.strip()
        if not name_clean:
            return None
        uid = user_id or 1
        with self.db.get_connection() as conn:
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

        if cat_id and self.firestore_db:
            try:
                doc_id = f"u{uid}_c{cat_id}"
                self.firestore_db.collection("categories").document(doc_id).set({
                    "id": cat_id,
                    "name": name_clean,
                    "user_id": uid,
                    "synced_to_cloud": 1,
                })
                with self.db.get_connection() as conn:
                    conn.execute("UPDATE categories SET synced_to_cloud = 1 WHERE id = ?", (cat_id,))
            except Exception:
                pass
        return cat_id

    # ════════════════════════════════════════════════════════════════════
    # ÜRÜN İŞLEMLERİ (KULLANICIYA ÖZEL İZOLE & STOK KORUMALI)
    # ════════════════════════════════════════════════════════════════════
    def get_products(self, user_id: Optional[int] = None, search: str = "") -> List[Dict[str, Any]]:
        uid = user_id or 1
        query = """
            SELECT p.*, c.name as category_name
            FROM products p
            LEFT JOIN categories c ON p.category_id = c.id
            WHERE p.is_active = 1 AND p.user_id = ?
        """
        params: list = [uid]

        if search and search.strip():
            s = f"%{search.strip()}%"
            query += " AND (p.name LIKE ? OR p.barcode LIKE ? OR c.name LIKE ?)"
            params.extend([s, s, s])

        query += " ORDER BY p.name ASC"

        with self.db.get_connection() as conn:
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def get_product_by_barcode(self, barcode: str, user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        uid = user_id or 1
        query = """
            SELECT p.*, c.name as category_name
            FROM products p
            LEFT JOIN categories c ON p.category_id = c.id
            WHERE p.barcode = ? AND p.is_active = 1 AND p.user_id = ?
        """
        params: list = [barcode.strip(), uid]

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

        uid = user_id or 1
        cat_id = self.add_category(category_name, user_id=uid) or 1
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with self.db.get_connection() as conn:
            if product_id:
                # Update existing product for this user
                existing = conn.execute(
                    "SELECT id, image_path, barcode FROM products WHERE id = ? AND user_id = ?",
                    (product_id, uid)
                ).fetchone()

                if not existing:
                    return False, "Düzenlenecek ürün bulunamadı!", None

                # Check duplicate barcode for ANOTHER active product
                dup = conn.execute(
                    "SELECT id FROM products WHERE barcode = ? AND user_id = ? AND id != ? AND is_active = 1",
                    (barcode, uid, product_id)
                ).fetchone()
                if dup:
                    return False, f"'{barcode}' barkodlu başka bir ürününüz zaten mevcut!", None

                # Fotoğraf koruma mantığı:
                # Eğer image_path verilmemişse (None) mevcut fotoğraf korunur.
                # Eğer '__REMOVE__' verilmişse fotoğraf temizlenir.
                if image_path == "__REMOVE__":
                    final_image_path = ""
                elif image_path is not None:
                    final_image_path = image_path
                else:
                    final_image_path = existing["image_path"] or ""

                conn.execute(
                    """
                    UPDATE products
                    SET category_id = ?, name = ?, barcode = ?, purchase_price = ?,
                        sale_price = ?, stock_quantity = ?, critical_stock_level = ?,
                        image_path = ?, updated_at = ?, is_active = 1
                    WHERE id = ? AND user_id = ?
                    """,
                    (
                        cat_id,
                        name,
                        barcode,
                        purchase_price,
                        sale_price,
                        stock_quantity,
                        critical_stock_level,
                        final_image_path,
                        now,
                        product_id,
                        uid,
                    ),
                )
                pid = product_id
                msg = f"'{name}' ürünü başarıyla güncellendi."
            else:
                final_image_path = image_path or ""
                # Check duplicate barcode for this user
                existing = conn.execute(
                    "SELECT id FROM products WHERE barcode = ? AND user_id = ? AND is_active = 1",
                    (barcode, uid)
                ).fetchone()

                if existing:
                    return False, f"'{barcode}' barkodlu bir ürününüz zaten mevcut!", None

                cursor = conn.execute(
                    """
                    INSERT INTO products (
                        user_id, category_id, name, barcode, purchase_price, sale_price,
                        stock_quantity, critical_stock_level, image_path, is_active, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        uid,
                        cat_id,
                        name,
                        barcode,
                        purchase_price,
                        sale_price,
                        stock_quantity,
                        critical_stock_level,
                        final_image_path,
                        now,
                        now,
                    ),
                )
                pid = cursor.lastrowid
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
            "stock_quantity": stock_quantity,
            "critical_stock_level": critical_stock_level,
            "image_path": final_image_path,
            "is_active": 1,
            "created_at": now,
            "updated_at": now,
        }
        synced = 0
        if self.firestore_db:
            try:
                prod_data["synced_to_cloud"] = 1
                doc_id = f"u{uid}_p{pid}"
                self.firestore_db.collection("products").document(doc_id).set(prod_data)
                synced = 1
            except Exception as e:
                logger.warning(f"Firestore ürün bulut kaydı uyarısı: {e}")
                synced = 0

        with self.db.get_connection() as conn:
            conn.execute("UPDATE products SET synced_to_cloud = ? WHERE id = ?", (synced, pid))

        prod_data["synced_to_cloud"] = synced
        return True, msg, prod_data

    def update_product_image(
        self, product_id: int, image_path: str, user_id: Optional[int] = None
    ) -> tuple[bool, str, Optional[str]]:
        """Bir ürünün fotoğrafını anında günceller veya temizler ve Firestore'a senkronize eder."""
        uid = user_id or 1
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        clean_img = "" if image_path == "__REMOVE__" else (image_path or "")

        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT id, name FROM products WHERE id = ? AND user_id = ? AND is_active = 1",
                (product_id, uid)
            ).fetchone()
            if not row:
                return False, "Fotoğrafı güncellenecek ürün bulunamadı!", None

            conn.execute(
                "UPDATE products SET image_path = ?, synced_to_cloud = 0, updated_at = ? WHERE id = ? AND user_id = ?",
                (clean_img, now, product_id, uid)
            )

        if self.firestore_db:
            try:
                doc_id = f"u{uid}_p{product_id}"
                self.firestore_db.collection("products").document(doc_id).update({
                    "image_path": clean_img,
                    "updated_at": now,
                    "synced_to_cloud": 1
                })
                with self.db.get_connection() as conn:
                    conn.execute("UPDATE products SET synced_to_cloud = 1 WHERE id = ?", (product_id,))
            except Exception as e:
                logger.warning(f"Firestore ürün görsel güncelleme uyarısı: {e}")

        msg = "Ürün görseli başarıyla kaldırıldı." if not clean_img else f"'{row['name']}' ürün fotoğrafı başarıyla güncellendi."
        return True, msg, clean_img

    def update_product_price(
        self, product_id: int, sale_price: float, purchase_price: Optional[float] = None, user_id: Optional[int] = None
    ) -> tuple[bool, str]:
        """Bir ürünün satış veya alış fiyatını anında günceller ve Firestore'a senkronize eder."""
        if sale_price < 0:
            return False, "Satış fiyatı negatif olamaz!"

        uid = user_id or 1
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

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
            try:
                doc_id = f"u{uid}_p{product_id}"
                self.firestore_db.collection("products").document(doc_id).update({
                    "sale_price": sale_price,
                    "purchase_price": p_price,
                    "updated_at": now,
                    "synced_to_cloud": 1
                })
                with self.db.get_connection() as conn:
                    conn.execute("UPDATE products SET synced_to_cloud = 1 WHERE id = ?", (product_id,))
            except Exception as e:
                logger.warning(f"Firestore fiyat güncelleme uyarısı: {e}")

        return True, f"'{row['name']}' ürününün yeni satış fiyatı {sale_price:.2f} ₺ olarak güncellendi."

    def delete_product(self, product_id: int, user_id: Optional[int] = None) -> tuple[bool, str]:
        uid = user_id or 1
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.db.get_connection() as conn:
            conn.execute(
                "UPDATE products SET is_active = 0, synced_to_cloud = 0, updated_at = ? WHERE id = ? AND user_id = ?",
                (now, product_id, uid)
            )

        if self.firestore_db:
            try:
                doc_id = f"u{uid}_p{product_id}"
                self.firestore_db.collection("products").document(doc_id).update({
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

        uid = user_id or 1
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.db.get_connection() as conn:
            conn.execute(
                "UPDATE products SET stock_quantity = ?, synced_to_cloud = 0, updated_at = ? WHERE id = ? AND user_id = ? AND is_active = 1",
                (stock_quantity, now, product_id, uid)
            )

        if self.firestore_db:
            try:
                doc_id = f"u{uid}_p{product_id}"
                self.firestore_db.collection("products").document(doc_id).update({
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
    ) -> tuple[bool, str]:
        if not cart_items:
            return False, "Sepet boş!"

        uid = self._resolve_user_id(user_id)
        ch = (channel or "magaza").strip().lower()
        if ch not in ("magaza", "internet"):
            ch = "magaza"

        cust_name = customer_name.strip() if customer_name and str(customer_name).strip() else None

        calculated_total = sum(float(item.get("subtotal", item.get("unit_price", 0) * item.get("quantity", 1))) for item in cart_items)
        if total_amount_override is not None:
            try:
                total_amount = round(float(total_amount_override), 2)
            except (ValueError, TypeError):
                total_amount = round(calculated_total, 2)
        else:
            total_amount = round(calculated_total, 2)

        item_count = sum(int(item.get("quantity", 1)) for item in cart_items)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with self.db.get_connection() as conn:
            cursor = conn.execute(
                "INSERT INTO sales (user_id, total_amount, item_count, sold_at, note, channel, customer_name) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (uid, total_amount, item_count, now, note, ch, cust_name),
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
                # Deduct stock ONLY for active products of this user
                conn.execute(
                    """
                    UPDATE products
                    SET stock_quantity = MAX(0, stock_quantity - ?), updated_at = ?
                    WHERE id = ? AND user_id = ? AND is_active = 1
                    """,
                    (item["quantity"], now, item["product_id"], uid),
                )

        synced = 0
        if self.firestore_db:
            try:
                doc_id = f"u{uid}_s{sale_id}"
                self.firestore_db.collection("sales").document(doc_id).set({
                    "id": sale_id,
                    "user_id": uid,
                    "total_amount": total_amount,
                    "item_count": item_count,
                    "sold_at": now,
                    "note": note,
                    "channel": ch,
                    "customer_name": cust_name,
                    "items": cart_items,
                    "synced_to_cloud": 1,
                })
                synced = 1
            except Exception:
                synced = 0

        with self.db.get_connection() as conn:
            conn.execute("UPDATE sales SET synced_to_cloud = ? WHERE id = ?", (synced, sale_id))

        return True, f"Satış başarıyla tamamlandı. Toplam: {total_amount:.2f} ₺"

    def delete_sale(
        self,
        sale_id: int,
        user_id: Optional[int] = None,
        restore_stock: bool = True,
    ) -> tuple[bool, str]:
        uid = user_id or 1
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with self.db.get_connection() as conn:
            # Satışın bu kullanıcıya ait olduğunu doğrula
            sale = conn.execute(
                "SELECT id, total_amount FROM sales WHERE id = ? AND user_id = ?",
                (sale_id, uid)
            ).fetchone()

            if not sale:
                return False, "Satış kaydı bulunamadı veya bu hesaba ait değil!"

            if restore_stock:
                # Satıştaki ürünlerin stoklarını geri yükle (aktif ürünler için)
                items = conn.execute(
                    "SELECT product_id, quantity FROM sale_items WHERE sale_id = ?",
                    (sale_id,)
                ).fetchall()
                for it in items:
                    pid = it["product_id"]
                    qty = it["quantity"]
                    if pid:
                        conn.execute(
                            """
                            UPDATE products
                            SET stock_quantity = stock_quantity + ?, updated_at = ?
                            WHERE id = ? AND user_id = ? AND is_active = 1
                            """,
                            (qty, now, pid, uid),
                        )

            # Satış kalemlerini ve ana satış kaydını sil
            conn.execute("DELETE FROM sale_items WHERE sale_id = ?", (sale_id,))
            conn.execute("DELETE FROM sales WHERE id = ? AND user_id = ?", (sale_id, uid))

        if self.firestore_db:
            try:
                doc_id = f"u{uid}_s{sale_id}"
                self.firestore_db.collection("sales").document(doc_id).delete()
                # Eski ID ile de silmeyi dene
                self.firestore_db.collection("sales").document(str(sale_id)).delete()
            except Exception as e:
                logger.warning(f"Firestore satış silme uyarısı: {e}")

        return True, "Satış başarıyla silindi ve ürün stokları geri yüklendi."

    def delete_sales_bulk(
        self,
        sale_ids: List[int],
        user_id: Optional[int] = None,
        restore_stock: bool = True,
    ) -> tuple[bool, str, int]:
        """Birden fazla satışı tek işlemde siler ve istenirse satılan stokları depoya iade eder."""
        if not sale_ids:
            return False, "Silinecek satış seçilmedi!", 0

        uid = self._resolve_user_id(user_id)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cleaned_ids = [int(sid) for sid in sale_ids if sid]
        if not cleaned_ids:
            return False, "Geçersiz satış listesi!", 0

        with self.db.get_connection() as conn:
            placeholders = ",".join("?" for _ in cleaned_ids)
            valid_sales = conn.execute(
                f"SELECT id FROM sales WHERE id IN ({placeholders}) AND (user_id = ? OR user_id IS NULL)",
                (*cleaned_ids, uid),
            ).fetchall()
            valid_ids = [r["id"] for r in valid_sales]

            if not valid_ids:
                return False, "Seçilen satışlar bulunamadı veya bu hesaba ait değil!", 0

            if restore_stock:
                v_placeholders = ",".join("?" for _ in valid_ids)
                items = conn.execute(
                    f"SELECT product_id, quantity FROM sale_items WHERE sale_id IN ({v_placeholders})",
                    valid_ids,
                ).fetchall()
                for it in items:
                    pid = it["product_id"]
                    qty = it["quantity"]
                    if pid:
                        conn.execute(
                            """
                            UPDATE products
                            SET stock_quantity = stock_quantity + ?, updated_at = ?
                            WHERE id = ? AND (user_id = ? OR user_id IS NULL) AND is_active = 1
                            """,
                            (qty, now, pid, uid),
                        )

            v_placeholders = ",".join("?" for _ in valid_ids)
            conn.execute(f"DELETE FROM sale_items WHERE sale_id IN ({v_placeholders})", valid_ids)
            conn.execute(f"DELETE FROM sales WHERE id IN ({v_placeholders}) AND (user_id = ? OR user_id IS NULL)", (*valid_ids, uid))
            deleted_count = len(valid_ids)

        if self.firestore_db:
            for sid in valid_ids:
                try:
                    self.firestore_db.collection("sales").document(f"u{uid}_s{sid}").delete()
                    self.firestore_db.collection("sales").document(str(sid)).delete()
                except Exception:
                    pass

        return True, f"{deleted_count} adet satış başarıyla silindi ve ürün stokları geri yüklendi.", deleted_count

    def get_sales_history(
        self,
        user_id: Optional[int] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        customer_name: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        uid = self._resolve_user_id(user_id)
        query = "SELECT * FROM sales WHERE (user_id = ? OR user_id IS NULL)"
        params: list = [uid]
        if start_date:
            query += " AND sold_at >= ?"
            params.append(start_date + " 00:00:00")
        if end_date:
            query += " AND sold_at <= ?"
            params.append(end_date + " 23:59:59")
        if customer_name and str(customer_name).strip():
            query += " AND customer_name LIKE ?"
            params.append(f"%{customer_name.strip()}%")
        query += " ORDER BY id DESC LIMIT 500"

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
        uid = self._resolve_user_id(user_id)
        with self.db.get_connection() as conn:
            sales_query = "SELECT COUNT(*) as total_transactions, SUM(total_amount) as total_revenue, SUM(item_count) as total_items FROM sales WHERE (user_id = ? OR user_id IS NULL)"
            s_params: list = [uid]
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

            # Top 5 Best Selling Products for this user
            top_query = """
                SELECT si.product_name, SUM(si.quantity) as total_qty, SUM(si.subtotal) as total_sales_amount
                FROM sale_items si
                JOIN sales s ON si.sale_id = s.id
                WHERE (s.user_id = ? OR s.user_id IS NULL)
            """
            top_params: list = [uid]
            if start_date:
                top_query += " AND s.sold_at >= ?"
                top_params.append(start_date + " 00:00:00")
            if end_date:
                top_query += " AND s.sold_at <= ?"
                top_params.append(end_date + " 23:59:59")
            top_query += " GROUP BY si.product_name ORDER BY total_qty DESC LIMIT 5"
            top_rows = conn.execute(top_query, top_params).fetchall()
            top_products = [dict(r) for r in top_rows]

            # Low Stock Items for this user
            low_query = "SELECT id, name, barcode, stock_quantity, critical_stock_level FROM products WHERE is_active = 1 AND (user_id = ? OR user_id IS NULL) AND stock_quantity <= critical_stock_level"
            low_rows = conn.execute(low_query, [uid]).fetchall()
            low_stock_items = [dict(r) for r in low_rows]

            # Total Expenses for this user
            exp_query = "SELECT SUM(amount) as total_exp FROM expenses WHERE (user_id = ? OR user_id IS NULL)"
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

            # Channel Breakdown (Mağaza vs İnternet)
            ch_query = """
                SELECT COALESCE(channel, 'magaza') as channel,
                       COUNT(*) as tx_count,
                       SUM(total_amount) as revenue,
                       SUM(item_count) as items
                FROM sales
                WHERE (user_id = ? OR user_id IS NULL)
            """
            ch_params: list = [uid]
            if start_date:
                ch_query += " AND sold_at >= ?"
                ch_params.append(start_date + " 00:00:00")
            if end_date:
                ch_query += " AND sold_at <= ?"
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

            # Monthly Breakdown (Her Ayın Toplam Cirosu)
            month_query = """
                SELECT strftime('%Y-%m', sold_at) as month_key,
                       COUNT(*) as tx_count,
                       SUM(total_amount) as revenue,
                       SUM(item_count) as items,
                       SUM(CASE WHEN COALESCE(channel, 'magaza') = 'magaza' THEN total_amount ELSE 0 END) as store_revenue,
                       SUM(CASE WHEN COALESCE(channel, 'magaza') = 'internet' THEN total_amount ELSE 0 END) as net_revenue
                FROM sales
                WHERE (user_id = ? OR user_id IS NULL)
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

            # Weekly Breakdown (Her Haftanın Toplam Cirosu)
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
                WHERE (user_id = ? OR user_id IS NULL)
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
    ) -> tuple[bool, str, Optional[Dict[str, Any]]]:
        t_clean = title.strip()
        if not t_clean or amount <= 0:
            return False, "Lütfen geçerli bir gider adı ve tutar giriniz!", None

        uid = user_id or 1
        exp_date = expense_date or datetime.now().strftime("%Y-%m-%d")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with self.db.get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO expenses (user_id, title, amount, category, expense_date, note, synced_to_cloud, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (uid, t_clean, amount, category.strip(), exp_date, note.strip(), now),
            )
            eid = cursor.lastrowid

        exp_data = {
            "id": eid,
            "user_id": uid,
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

    def get_expenses(
        self,
        user_id: Optional[int] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        uid = user_id or 1
        with self.db.get_connection() as conn:
            query = "SELECT * FROM expenses WHERE user_id = ?"
            params: list = [uid]
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
                    "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
                            self.pull_all_from_firebase(user_id=uid)
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
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        user_dict = None

        # 1. SQLite'da ara
        with self.db.get_connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM users 
                WHERE phone = ? OR phone = ? OR phone = ? OR phone = ? OR phone = ? OR LOWER(email) = ?
                """,
                (query_val, p_clean, p_zero, p_90, p_plus90, e_clean),
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
            self.pull_all_from_firebase(user_id=user_dict["id"])
            return True, f"Hoş geldiniz, {user_dict.get('full_name', '')} ({user_dict.get('company_name', '')})", user_dict

        # 2. SQLite'da bulunamazsa Firestore'dan sorgula
        if self.firestore_db:
            try:
                users_col = self.firestore_db.collection("users")
                docs = list(users_col.stream())
                for d in docs:
                    data = d.to_dict()
                    d_phone = self._clean_phone(str(data.get("phone", "")))
                    d_email = str(data.get("email", "")).strip().lower()
                    if (p_clean and d_phone == p_clean) or d_email == e_clean or str(data.get("phone", "")) == query_val:
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
                            self.pull_all_from_firebase(user_id=uid)
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
