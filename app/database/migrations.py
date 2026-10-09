from pathlib import Path

from app.database.connection import Database
from app.database.seed_data import seed_demo_products

SCHEMA_VERSION = 1

DEFAULT_CATEGORIES = ("Genel", "Gıda", "İçecek", "Temizlik", "Elektronik")


def _seed_categories(conn) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO users (id, company_name, full_name, phone, email, password_hash)
        VALUES (1, 'Yerel İşletme', 'Yönetici', '05000000000', 'admin@yerel.com', '')
        """
    )
    for cat in DEFAULT_CATEGORIES:
        conn.execute(
            "INSERT OR IGNORE INTO categories (name, user_id, synced_to_cloud) VALUES (?, 1, 1)",
            (cat,),
        )


def run_migrations() -> None:
    schema_path = Path(__file__).parent / "schema.sql"
    schema_sql = schema_path.read_text(encoding="utf-8")

    db = Database()
    with db.get_connection() as conn:
        current = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).fetchone()

        if current is None:
            conn.executescript(schema_sql)
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
            _seed_categories(conn)
            seed_demo_products(conn)
        else:
            # Ensure image_path column exists in existing database
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(products)").fetchall()]
            if "image_path" not in cols:
                try:
                    conn.execute("ALTER TABLE products ADD COLUMN image_path TEXT")
                except Exception:
                    pass

            if "user_id" not in cols:
                try:
                    conn.execute("ALTER TABLE products ADD COLUMN user_id INTEGER")
                except Exception:
                    pass

            if "synced_to_cloud" not in cols:
                try:
                    conn.execute("ALTER TABLE products ADD COLUMN synced_to_cloud INTEGER NOT NULL DEFAULT 0")
                except Exception:
                    pass

            cat_cols = [r["name"] for r in conn.execute("PRAGMA table_info(categories)").fetchall()]
            if "user_id" not in cat_cols:
                try:
                    conn.execute("ALTER TABLE categories ADD COLUMN user_id INTEGER")
                except Exception:
                    pass

            if "synced_to_cloud" not in cat_cols:
                try:
                    conn.execute("ALTER TABLE categories ADD COLUMN synced_to_cloud INTEGER NOT NULL DEFAULT 0")
                except Exception:
                    pass

            sales_cols = [r["name"] for r in conn.execute("PRAGMA table_info(sales)").fetchall()]
            if "user_id" not in sales_cols:
                try:
                    conn.execute("ALTER TABLE sales ADD COLUMN user_id INTEGER")
                except Exception:
                    pass

            if "synced_to_cloud" not in sales_cols:
                try:
                    conn.execute("ALTER TABLE sales ADD COLUMN synced_to_cloud INTEGER NOT NULL DEFAULT 0")
                except Exception:
                    pass

            if "channel" not in sales_cols:
                try:
                    conn.execute("ALTER TABLE sales ADD COLUMN channel TEXT NOT NULL DEFAULT 'magaza'")
                except Exception:
                    pass
            try:
                conn.execute("CREATE INDEX IF NOT EXISTS idx_sales_channel ON sales(channel)")
            except Exception:
                pass

            if "customer_name" not in sales_cols:
                try:
                    conn.execute("ALTER TABLE sales ADD COLUMN customer_name TEXT")
                except Exception:
                    pass
            try:
                conn.execute("CREATE INDEX IF NOT EXISTS idx_sales_customer_name ON sales(customer_name)")
            except Exception:
                pass

            # Ensure users table exists in existing database
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    company_name    TEXT NOT NULL,
                    full_name       TEXT NOT NULL,
                    phone           TEXT NOT NULL UNIQUE,
                    email           TEXT NOT NULL UNIQUE,
                    password_hash   TEXT NOT NULL,
                    auth_token      TEXT UNIQUE,
                    synced_to_cloud INTEGER NOT NULL DEFAULT 0,
                    created_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                    updated_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
                )
                """
            )
            users_cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
            if "synced_to_cloud" not in users_cols:
                try:
                    conn.execute("ALTER TABLE users ADD COLUMN synced_to_cloud INTEGER NOT NULL DEFAULT 0")
                except Exception:
                    pass

            # Ensure expenses table exists and has synced_to_cloud
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS expenses (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id         INTEGER,
                    title           TEXT NOT NULL,
                    amount          REAL NOT NULL,
                    category        TEXT DEFAULT 'Fatura',
                    expense_date    TEXT NOT NULL,
                    note            TEXT,
                    synced_to_cloud INTEGER NOT NULL DEFAULT 0,
                    created_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
                )
                """
            )
            exp_cols = [r["name"] for r in conn.execute("PRAGMA table_info(expenses)").fetchall()]
            if "synced_to_cloud" not in exp_cols:
                try:
                    conn.execute("ALTER TABLE expenses ADD COLUMN synced_to_cloud INTEGER NOT NULL DEFAULT 0")
                except Exception:
                    pass

            # Rebuild products table if old global barcode UNIQUE constraint is present
            has_unique_barcode = False
            try:
                auto_indexes = conn.execute("PRAGMA index_list(products)").fetchall()
                for idx in auto_indexes:
                    if idx["unique"]:
                        idx_cols = [c["name"] for c in conn.execute(f"PRAGMA index_info('{idx['name']}')").fetchall()]
                        if idx_cols == ["barcode"]:
                            has_unique_barcode = True
                            break
            except Exception:
                pass

            if has_unique_barcode:
                conn.execute("PRAGMA foreign_keys=OFF")
                conn.execute(
                    """
                    CREATE TABLE products_new (
                        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id              INTEGER REFERENCES users(id),
                        category_id          INTEGER NOT NULL REFERENCES categories(id),
                        name                 TEXT NOT NULL,
                        barcode              TEXT NOT NULL,
                        purchase_price       REAL NOT NULL CHECK (purchase_price >= 0),
                        sale_price           REAL NOT NULL CHECK (sale_price >= 0),
                        stock_quantity       INTEGER NOT NULL DEFAULT 0 CHECK (stock_quantity >= 0),
                        critical_stock_level INTEGER NOT NULL DEFAULT 5,
                        image_path           TEXT,
                        is_active            INTEGER NOT NULL DEFAULT 1,
                        created_at           TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                        updated_at           TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO products_new (
                        id, user_id, category_id, name, barcode, purchase_price, sale_price,
                        stock_quantity, critical_stock_level, image_path, is_active, created_at, updated_at
                    )
                    SELECT id, user_id, category_id, name, barcode, purchase_price, sale_price,
                           stock_quantity, critical_stock_level, image_path, is_active, created_at, updated_at
                    FROM products
                    """
                )
                conn.execute("DROP TABLE products")
                conn.execute("ALTER TABLE products_new RENAME TO products")
                conn.execute("PRAGMA foreign_keys=ON")

            # Ensure users table exists in existing database
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    company_name  TEXT NOT NULL,
                    full_name     TEXT NOT NULL,
                    phone         TEXT NOT NULL UNIQUE,
                    email         TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    auth_token    TEXT UNIQUE,
                    created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                    updated_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_phone ON users(phone)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_auth_token ON users(auth_token)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_products_user_id ON products(user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_categories_user_id ON categories(user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sales_user_id ON sales(user_id)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS store_settings (
                    user_id         INTEGER PRIMARY KEY,
                    weekday_hours   TEXT,
                    weekend_hours   TEXT
                )
                """
            )

            # Ensure farm tables exist
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS farm_customers (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id         INTEGER,
                    name            TEXT NOT NULL,
                    phone           TEXT,
                    synced_to_cloud INTEGER NOT NULL DEFAULT 0,
                    created_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_farm_customers_name ON farm_customers(name)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_farm_customers_user ON farm_customers(user_id)")

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS farm_egg_sales (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id         INTEGER,
                    customer_name   TEXT NOT NULL,
                    box_count       REAL NOT NULL CHECK (box_count > 0),
                    unit_price      REAL NOT NULL CHECK (unit_price >= 0),
                    total_amount    REAL NOT NULL CHECK (total_amount >= 0),
                    source          TEXT NOT NULL DEFAULT 'Ciftlik',
                    sale_date       TEXT NOT NULL,
                    note            TEXT,
                    synced_to_cloud INTEGER NOT NULL DEFAULT 0,
                    created_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_farm_egg_sales_user_date ON farm_egg_sales(user_id, sale_date)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_farm_egg_sales_cust ON farm_egg_sales(customer_name)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_farm_egg_sales_source ON farm_egg_sales(source)")

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS farm_feed_purchases (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id         INTEGER,
                    bag_count       REAL NOT NULL CHECK (bag_count > 0),
                    bag_weight_kg   REAL NOT NULL DEFAULT 50.0,
                    total_weight_kg REAL NOT NULL,
                    total_weight_ton REAL NOT NULL,
                    unit_price      REAL NOT NULL DEFAULT 0.0,
                    total_amount    REAL NOT NULL DEFAULT 0.0,
                    purchase_date   TEXT NOT NULL,
                    supplier        TEXT,
                    note            TEXT,
                    synced_to_cloud INTEGER NOT NULL DEFAULT 0,
                    created_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_farm_feed_purchases_user_date ON farm_feed_purchases(user_id, purchase_date)")

            # farm_egg_sales koli/adet sütunları ve veri güncellemesi
            cols_egg = [c["name"] for c in conn.execute("PRAGMA table_info(farm_egg_sales)").fetchall()]
            if "unit_type" not in cols_egg:
                conn.execute("ALTER TABLE farm_egg_sales ADD COLUMN unit_type TEXT DEFAULT 'koli'")
            if "piece_count" not in cols_egg:
                conn.execute("ALTER TABLE farm_egg_sales ADD COLUMN piece_count REAL DEFAULT 0")

            # Geçmiş dükkan kayıtlarını düzelt (30'lu koli dışındakileri adet yap)
            conn.execute("""
                UPDATE farm_egg_sales
                SET unit_type = 'adet',
                    piece_count = box_count,
                    box_count = ROUND(box_count / 30.0, 3)
                WHERE source = 'Dükkan' AND (note LIKE '%1 adet%' OR note LIKE '%adet%') AND (unit_type IS NULL OR unit_type = 'koli')
            """)
            conn.execute("""
                UPDATE farm_egg_sales
                SET unit_type = 'adet',
                    piece_count = box_count * 20.0,
                    box_count = ROUND((box_count * 20.0) / 30.0, 3)
                WHERE source = 'Dükkan' AND (note LIKE '%20''li%' OR note LIKE '%20li%') AND (unit_type IS NULL OR unit_type = 'koli')
            """)
            conn.execute("""
                UPDATE farm_egg_sales
                SET unit_type = 'koli',
                    piece_count = box_count * 30.0
                WHERE (unit_type IS NULL OR unit_type = 'koli') AND (piece_count = 0 OR piece_count IS NULL)
            """)

        for name in DEFAULT_CATEGORIES:
            conn.execute(
                "INSERT OR IGNORE INTO categories (name) VALUES (?)",
                (name,),
            )
