DEMO_PRODUCTS = [
    {
        "name": "Su 500ml",
        "category": "İçecek",
        "barcode": "869000000001",
        "purchase_price": 2.0,
        "sale_price": 5.0,
        "stock_quantity": 50,
        "critical_stock_level": 5,
    },
    {
        "name": "Ekmek",
        "category": "Gıda",
        "barcode": "869000000002",
        "purchase_price": 5.0,
        "sale_price": 12.0,
        "stock_quantity": 3,
        "critical_stock_level": 5,
    },
    {
        "name": "Kola 330ml",
        "category": "İçecek",
        "barcode": "869000000003",
        "purchase_price": 8.0,
        "sale_price": 15.0,
        "stock_quantity": 24,
        "critical_stock_level": 5,
    },
    {
        "name": "Bulaşık Deterjanı",
        "category": "Temizlik",
        "barcode": "869000000004",
        "purchase_price": 45.0,
        "sale_price": 79.0,
        "stock_quantity": 2,
        "critical_stock_level": 5,
    },
    {
        "name": "USB Kablo",
        "category": "Elektronik",
        "barcode": "869000000005",
        "purchase_price": 25.0,
        "sale_price": 49.0,
        "stock_quantity": 0,
        "critical_stock_level": 3,
    },
    {
        "name": "Sütlü Çikolata",
        "category": "Gıda",
        "barcode": "869000000006",
        "purchase_price": 6.0,
        "sale_price": 14.0,
        "stock_quantity": 18,
        "critical_stock_level": 5,
    },
]


def seed_demo_products(conn, force: bool = False) -> int:
    if not force:
        count = conn.execute("SELECT COUNT(*) AS cnt FROM products").fetchone()["cnt"]
        if count > 0:
            return 0

    added_count = 0
    for item in DEMO_PRODUCTS:
        # Check if barcode exists
        existing = conn.execute(
            "SELECT id FROM products WHERE barcode = ?",
            (item["barcode"],),
        ).fetchone()
        if existing:
            continue

        category = conn.execute(
            "SELECT id FROM categories WHERE name = ?",
            (item["category"],),
        ).fetchone()
        if not category:
            cursor = conn.execute(
                "INSERT INTO categories (name) VALUES (?)",
                (item["category"],),
            )
            category_id = cursor.lastrowid
        else:
            category_id = category["id"]

        conn.execute(
            """
            INSERT INTO products (
                category_id, name, barcode, purchase_price, sale_price,
                stock_quantity, critical_stock_level
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                category_id,
                item["name"],
                item["barcode"],
                item["purchase_price"],
                item["sale_price"],
                item["stock_quantity"],
                item["critical_stock_level"],
            ),
        )
        added_count += 1
    return added_count

