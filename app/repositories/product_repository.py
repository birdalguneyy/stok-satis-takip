from typing import List, Optional

from app.database.connection import Database
from app.models.product import Product

_PRODUCT_SELECT = """
    SELECT p.*, c.name AS category_name
    FROM products p
    JOIN categories c ON c.id = p.category_id
    WHERE p.is_active = 1
"""


class ProductRepository:
    def __init__(self, db: Optional[Database] = None) -> None:
        self.db = db or Database()

    def get_all(self, search: str = "", order_by: str = "name", ascending: bool = True) -> List[Product]:
        allowed_columns = {
            "name": "p.name",
            "category": "c.name",
            "barcode": "p.barcode",
            "purchase_price": "p.purchase_price",
            "sale_price": "p.sale_price",
            "stock_quantity": "p.stock_quantity",
        }
        column = allowed_columns.get(order_by, "p.name")
        direction = "ASC" if ascending else "DESC"

        query = f"{_PRODUCT_SELECT}"
        params: list = []

        if search.strip():
            query += " AND (p.name LIKE ? OR p.barcode LIKE ? OR c.name LIKE ?)"
            term = f"%{search.strip()}%"
            params.extend([term, term, term])

        query += f" ORDER BY {column} {direction}"

        with self.db.get_connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [Product.from_row(row) for row in rows]

    def get_by_id(self, product_id: int) -> Optional[Product]:
        with self.db.get_connection() as conn:
            row = conn.execute(
                f"{_PRODUCT_SELECT} AND p.id = ?",
                (product_id,),
            ).fetchone()
        return Product.from_row(row) if row else None

    def get_by_barcode(self, barcode: str) -> Optional[Product]:
        with self.db.get_connection() as conn:
            row = conn.execute(
                f"{_PRODUCT_SELECT} AND p.barcode = ?",
                (barcode.strip(),),
            ).fetchone()
        return Product.from_row(row) if row else None

    def search(self, term: str) -> List[Product]:
        with self.db.get_connection() as conn:
            rows = conn.execute(
                f"""{_PRODUCT_SELECT}
                AND (p.barcode = ? OR p.name LIKE ?)
                ORDER BY p.name
                LIMIT 10""",
                (term.strip(), f"%{term.strip()}%"),
            ).fetchall()
        return [Product.from_row(row) for row in rows]

    def create(self, product: Product) -> Product:
        with self.db.get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO products (
                    category_id, name, barcode, purchase_price, sale_price,
                    stock_quantity, critical_stock_level, image_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    product.category_id,
                    product.name.strip(),
                    product.barcode.strip(),
                    product.purchase_price,
                    product.sale_price,
                    product.stock_quantity,
                    product.critical_stock_level,
                    product.image_path or "",
                ),
            )
            return self.get_by_id(cursor.lastrowid)

    def update(self, product: Product) -> Product:
        with self.db.get_connection() as conn:
            conn.execute(
                """
                UPDATE products SET
                    category_id = ?,
                    name = ?,
                    barcode = ?,
                    purchase_price = ?,
                    sale_price = ?,
                    stock_quantity = ?,
                    critical_stock_level = ?,
                    image_path = ?,
                    updated_at = datetime('now', 'localtime')
                WHERE id = ?
                """,
                (
                    product.category_id,
                    product.name.strip(),
                    product.barcode.strip(),
                    product.purchase_price,
                    product.sale_price,
                    product.stock_quantity,
                    product.critical_stock_level,
                    product.image_path or "",
                    product.id,
                ),
            )
        return self.get_by_id(product.id)

    def delete(self, product_id: int) -> None:
        with self.db.get_connection() as conn:
            conn.execute(
                "UPDATE products SET is_active = 0, updated_at = datetime('now', 'localtime') WHERE id = ?",
                (product_id,),
            )

    def count_all(self) -> int:
        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM products WHERE is_active = 1"
            ).fetchone()
        return int(row["cnt"])

    def count_critical(self, default_level: int) -> int:
        with self.db.get_connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS cnt FROM products
                WHERE is_active = 1 AND stock_quantity <= critical_stock_level
                """
            ).fetchone()
        return int(row["cnt"])

    def get_critical_products(self, limit: int = 8) -> List[Product]:
        with self.db.get_connection() as conn:
            rows = conn.execute(
                f"""{_PRODUCT_SELECT}
                AND p.stock_quantity <= p.critical_stock_level
                ORDER BY p.stock_quantity ASC, p.name ASC
                LIMIT ?""",
                (limit,),
            ).fetchall()
        return [Product.from_row(row) for row in rows]

    def decrease_stock(self, conn, product_id: int, quantity: int) -> None:
        conn.execute(
            """
            UPDATE products
            SET stock_quantity = stock_quantity - ?,
                updated_at = datetime('now', 'localtime')
            WHERE id = ? AND stock_quantity >= ?
            """,
            (quantity, product_id, quantity),
        )
        if conn.total_changes == 0:
            raise ValueError("Stok yetersiz")
