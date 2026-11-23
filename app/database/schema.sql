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
    role            TEXT NOT NULL DEFAULT 'patron',
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
    stock_quantity       REAL NOT NULL DEFAULT 0 CHECK (stock_quantity >= 0),
    critical_stock_level REAL NOT NULL DEFAULT 5,
    unit                 TEXT NOT NULL DEFAULT 'adet',
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
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER REFERENCES users(id),
    total_amount      REAL NOT NULL CHECK (total_amount >= 0),
    item_count        INTEGER NOT NULL CHECK (item_count > 0),
    sold_at           TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    note              TEXT,
    channel           TEXT NOT NULL DEFAULT 'magaza',
    payment_method    TEXT NOT NULL DEFAULT 'nakit',
    customer_id       INTEGER REFERENCES credit_customers(id),
    customer_name     TEXT,
    paid_cash_amount  REAL NOT NULL DEFAULT 0,
    paid_pos_amount   REAL NOT NULL DEFAULT 0,
    credit_amount     REAL NOT NULL DEFAULT 0,
    overpayment_mode  TEXT,
    status            TEXT NOT NULL DEFAULT 'active',
    cancelled_at      TEXT,
    cancelled_by      TEXT,
    cancelled_by_role TEXT,
    cancel_reason     TEXT,
    cancel_note       TEXT,
    created_by        TEXT,
    created_by_role   TEXT,
    synced_to_cloud   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_sales_user_id ON sales(user_id);
CREATE INDEX IF NOT EXISTS idx_sales_sold_at ON sales(sold_at);
CREATE INDEX IF NOT EXISTS idx_sales_channel ON sales(channel);
CREATE INDEX IF NOT EXISTS idx_sales_customer_name ON sales(customer_name);
CREATE INDEX IF NOT EXISTS idx_sales_status ON sales(status);

CREATE TABLE IF NOT EXISTS sale_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    sale_id      INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
    product_id   INTEGER NOT NULL REFERENCES products(id),
    product_name TEXT NOT NULL,
    barcode      TEXT NOT NULL,
    unit_price   REAL NOT NULL,
    quantity     REAL NOT NULL CHECK (quantity > 0),
    unit         TEXT NOT NULL DEFAULT 'adet',
    subtotal     REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sale_items_sale_id ON sale_items(sale_id);
CREATE INDEX IF NOT EXISTS idx_sale_items_product_id ON sale_items(product_id);

CREATE TABLE IF NOT EXISTS expenses (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER REFERENCES users(id),
    title             TEXT NOT NULL,
    amount            REAL NOT NULL CHECK (amount >= 0),
    category          TEXT DEFAULT 'Fatura',
    payment_method    TEXT NOT NULL DEFAULT 'nakit',
    expense_date      TEXT NOT NULL,
    note              TEXT,
    status            TEXT NOT NULL DEFAULT 'active',
    cancelled_at      TEXT,
    cancelled_by      TEXT,
    cancelled_by_role TEXT,
    cancel_reason     TEXT,
    cancel_note       TEXT,
    created_by        TEXT,
    created_by_role   TEXT,
    synced_to_cloud   INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_expenses_user_id ON expenses(user_id);
CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses(expense_date);
CREATE INDEX IF NOT EXISTS idx_expenses_status ON expenses(status);

CREATE TABLE IF NOT EXISTS store_settings (
    user_id         INTEGER PRIMARY KEY REFERENCES users(id),
    weekday_hours   TEXT,
    weekend_hours   TEXT
);

CREATE TABLE IF NOT EXISTS farm_customers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER REFERENCES users(id),
    name            TEXT NOT NULL,
    phone           TEXT,
    synced_to_cloud INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_farm_customers_name ON farm_customers(name);
CREATE INDEX IF NOT EXISTS idx_farm_customers_user ON farm_customers(user_id);

CREATE TABLE IF NOT EXISTS farm_egg_sales (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER REFERENCES users(id),
    customer_name     TEXT NOT NULL,
    box_count         REAL NOT NULL CHECK (box_count > 0),
    unit_type         TEXT NOT NULL DEFAULT 'koli',
    piece_count       REAL NOT NULL DEFAULT 0,
    unit_price        REAL NOT NULL CHECK (unit_price >= 0),
    total_amount      REAL NOT NULL CHECK (total_amount >= 0),
    source            TEXT NOT NULL DEFAULT 'Ciftlik',
    sale_date         TEXT NOT NULL,
    note              TEXT,
    status            TEXT NOT NULL DEFAULT 'active',
    cancelled_at      TEXT,
    cancelled_by      TEXT,
    cancelled_by_role TEXT,
    cancel_reason     TEXT,
    cancel_note       TEXT,
    synced_to_cloud   INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_farm_egg_sales_user_date ON farm_egg_sales(user_id, sale_date);
CREATE INDEX IF NOT EXISTS idx_farm_egg_sales_cust ON farm_egg_sales(customer_name);
CREATE INDEX IF NOT EXISTS idx_farm_egg_sales_source ON farm_egg_sales(source);
CREATE INDEX IF NOT EXISTS idx_farm_egg_sales_status ON farm_egg_sales(status);

CREATE TABLE IF NOT EXISTS farm_feed_purchases (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER REFERENCES users(id),
    bag_count         REAL NOT NULL CHECK (bag_count > 0),
    bag_weight_kg     REAL NOT NULL DEFAULT 50.0,
    total_weight_kg   REAL NOT NULL,
    total_weight_ton  REAL NOT NULL,
    unit_price        REAL NOT NULL DEFAULT 0.0,
    total_amount      REAL NOT NULL DEFAULT 0.0,
    purchase_date     TEXT NOT NULL,
    supplier          TEXT,
    note              TEXT,
    status            TEXT NOT NULL DEFAULT 'active',
    cancelled_at      TEXT,
    cancelled_by      TEXT,
    cancelled_by_role TEXT,
    cancel_reason     TEXT,
    cancel_note       TEXT,
    synced_to_cloud   INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_farm_feed_purchases_user_date ON farm_feed_purchases(user_id, purchase_date);
CREATE INDEX IF NOT EXISTS idx_farm_feed_purchases_status ON farm_feed_purchases(status);

CREATE TABLE IF NOT EXISTS cash_days (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER REFERENCES users(id),
    business_date   TEXT NOT NULL,
    opening_amount  REAL NOT NULL DEFAULT 0 CHECK (opening_amount >= 0),
    opened_at       TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    opened_by       TEXT NOT NULL,
    opened_by_role  TEXT NOT NULL DEFAULT 'patron',
    opening_note    TEXT,
    status                  TEXT NOT NULL DEFAULT 'open',
    closed_at               TEXT,
    closed_by               TEXT,
    closed_by_role          TEXT,
    closing_amount          REAL,
    expected_cash           REAL,
    expected_pos            REAL,
    expected_expenses       REAL,
    counted_cash            REAL,
    entered_pos_total       REAL,
    entered_expenses_total  REAL,
    cash_diff               REAL,
    pos_diff                REAL,
    expense_diff            REAL,
    closing_note            TEXT,
    reopened_at             TEXT,
    reopened_by             TEXT,
    reopen_reason           TEXT,
    UNIQUE(user_id, business_date)
);

CREATE INDEX IF NOT EXISTS idx_cash_days_user_date ON cash_days(user_id, business_date);

CREATE TABLE IF NOT EXISTS cash_movements (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER REFERENCES users(id),
    cash_day_id       INTEGER REFERENCES cash_days(id),
    movement_type     TEXT NOT NULL CHECK (movement_type IN ('tahsilat', 'kasa_giris', 'kasa_cikis')),
    category          TEXT NOT NULL DEFAULT 'genel',
    amount            REAL NOT NULL CHECK (amount >= 0),
    description       TEXT NOT NULL,
    customer_name     TEXT,
    movement_date     TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    note              TEXT,
    status            TEXT NOT NULL DEFAULT 'active',
    cancelled_at      TEXT,
    cancelled_by      TEXT,
    cancelled_by_role TEXT,
    cancel_reason     TEXT,
    cancel_note       TEXT,
    created_by        TEXT,
    created_by_role   TEXT,
    synced_to_cloud   INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_cash_movements_user_date ON cash_movements(user_id, movement_date);
CREATE INDEX IF NOT EXISTS idx_cash_movements_type ON cash_movements(movement_type);
CREATE INDEX IF NOT EXISTS idx_cash_movements_status ON cash_movements(status);

CREATE TABLE IF NOT EXISTS credit_customers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER REFERENCES users(id),
    name       TEXT NOT NULL,
    phone      TEXT,
    note       TEXT,
    is_active  INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_credit_customers_user_name ON credit_customers(user_id, name);

CREATE TABLE IF NOT EXISTS credit_transactions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER REFERENCES users(id),
    customer_id       INTEGER NOT NULL REFERENCES credit_customers(id),
    sale_id           INTEGER REFERENCES sales(id),
    tx_type           TEXT NOT NULL CHECK (tx_type IN ('borc', 'odeme', 'tahsilat')),
    payment_method    TEXT,
    amount_kurus      INTEGER NOT NULL CHECK (amount_kurus > 0),
    signed_kurus      INTEGER NOT NULL,
    description       TEXT NOT NULL,
    product_summary   TEXT,
    tx_date           TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    status            TEXT NOT NULL DEFAULT 'active',
    cancelled_at      TEXT,
    cancelled_by      TEXT,
    cancelled_by_role TEXT,
    cancel_reason     TEXT,
    cancel_note       TEXT,
    created_by        TEXT,
    created_by_role   TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_credit_tx_customer ON credit_transactions(customer_id, tx_date);
CREATE INDEX IF NOT EXISTS idx_credit_tx_sale ON credit_transactions(sale_id);
CREATE INDEX IF NOT EXISTS idx_credit_tx_status ON credit_transactions(status);
CREATE INDEX IF NOT EXISTS idx_credit_tx_type_date ON credit_transactions(tx_type, tx_date);

CREATE TABLE IF NOT EXISTS audit_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER,
    actor_name  TEXT NOT NULL,
    actor_role  TEXT NOT NULL DEFAULT 'patron',
    action      TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   TEXT,
    old_value   TEXT,
    new_value   TEXT,
    reason      TEXT,
    ip_address  TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at ON audit_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_logs_entity ON audit_logs(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_action ON audit_logs(action);

CREATE TRIGGER IF NOT EXISTS trg_audit_logs_no_update
BEFORE UPDATE ON audit_logs
BEGIN
    SELECT RAISE(ABORT, 'Audit logs are immutable and cannot be updated');
END;

CREATE TRIGGER IF NOT EXISTS trg_audit_logs_no_delete
BEFORE DELETE ON audit_logs
BEGIN
    SELECT RAISE(ABORT, 'Audit logs are immutable and cannot be deleted');
END;


