from pathlib import Path

from app.database.connection import Database
from app.database.seed_data import seed_demo_products

SCHEMA_VERSION = 1

DEFAULT_CATEGORIES = ("Genel", "Gıda", "İçecek", "Temizlik", "Elektronik")


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



def _seed_categories(conn) -> None:
    for name in DEFAULT_CATEGORIES:
        conn.execute(
            "INSERT OR IGNORE INTO categories (name) VALUES (?)",
            (name,),
        )
