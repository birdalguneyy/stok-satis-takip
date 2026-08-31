PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

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
);

CREATE INDEX IF NOT EXISTS idx_users_phone ON users(phone);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_auth_token ON users(auth_token);

CREATE TABLE IF NOT EXISTS categories (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER REFERENCES users(id),
    name            TEXT NOT NULL,
    synced_to_cloud INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_categories_user_id ON categories(user_id);

CREATE TABLE IF NOT EXISTS products (
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
    synced_to_cloud      INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at           TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_products_user_id ON products(user_id);
CREATE INDEX IF NOT EXISTS idx_products_barcode ON products(barcode);
CREATE INDEX IF NOT EXISTS idx_products_name ON products(name);
CREATE INDEX IF NOT EXISTS idx_products_stock ON products(stock_quantity);

CREATE TABLE IF NOT EXISTS sales (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER REFERENCES users(id),
    total_amount    REAL NOT NULL CHECK (total_amount >= 0),
    item_count      INTEGER NOT NULL CHECK (item_count > 0),
    sold_at         TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    note            TEXT,
    synced_to_cloud INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_sales_user_id ON sales(user_id);
CREATE INDEX IF NOT EXISTS idx_sales_sold_at ON sales(sold_at);

CREATE TABLE IF NOT EXISTS sale_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    sale_id      INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
    product_id   INTEGER NOT NULL REFERENCES products(id),
    product_name TEXT NOT NULL,
    barcode      TEXT NOT NULL,
    unit_price   REAL NOT NULL,
    quantity     INTEGER NOT NULL CHECK (quantity > 0),
    subtotal     REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sale_items_sale_id ON sale_items(sale_id);
CREATE INDEX IF NOT EXISTS idx_sale_items_product_id ON sale_items(product_id);

CREATE TABLE IF NOT EXISTS expenses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER REFERENCES users(id),
    title           TEXT NOT NULL,
    amount          REAL NOT NULL,
    category        TEXT DEFAULT 'Fatura',
    expense_date    TEXT NOT NULL,
    note            TEXT,
    synced_to_cloud INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_expenses_user_id ON expenses(user_id);
CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses(expense_date);

CREATE TABLE IF NOT EXISTS store_settings (
    user_id         INTEGER PRIMARY KEY REFERENCES users(id),
    weekday_hours   TEXT,
    weekend_hours   TEXT
);

