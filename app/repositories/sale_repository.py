from typing import List, Optional, Tuple

from app.database.connection import Database
from app.models.cart_item import CartItem
from app.models.sale import Sale, SaleItem


class SaleRepository:
    def __init__(self, db: Optional[Database] = None) -> None:
        self.db = db or Database()

    def create_sale(self, cart_items: List[CartItem], note: Optional[str] = None) -> Sale:
        total_amount = sum(item.subtotal for item in cart_items)
        item_count = sum(item.quantity for item in cart_items)

        with self.db.get_connection() as conn:
            cursor = conn.execute(
                "INSERT INTO sales (total_amount, item_count, note) VALUES (?, ?, ?)",
                (total_amount, item_count, note),
            )
            sale_id = cursor.lastrowid

            for item in cart_items:
                conn.execute(
                    """
                    INSERT INTO sale_items (
                        sale_id, product_id, product_name, barcode,
                        unit_price, quantity, subtotal
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sale_id,
                        item.product_id,
                        item.product_name,
                        item.barcode,
                        item.unit_price,
                        item.quantity,
                        item.subtotal,
                    ),
                )
                conn.execute(
                    """
                    UPDATE products
                    SET stock_quantity = stock_quantity - ?,
                        updated_at = datetime('now', 'localtime')
                    WHERE id = ? AND stock_quantity >= ?
                    """,
                    (item.quantity, item.product_id, item.quantity),
                )
                if conn.total_changes == 0:
                    raise ValueError(f"Stok yetersiz: {item.product_name}")

            row = conn.execute("SELECT * FROM sales WHERE id = ?", (sale_id,)).fetchone()

        return Sale.from_row(row)

    def count_today_sales(self) -> int:
        with self.db.get_connection() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(item_count), 0) AS total
                FROM sales
                WHERE date(sold_at) = date('now', 'localtime')
                """
            ).fetchone()
        return int(row["total"])

    def get_top_selling_products(self, limit: int = 5) -> List[Tuple[str, int]]:
        with self.db.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT product_name, SUM(quantity) AS total_qty
                FROM sale_items
                GROUP BY product_id, product_name
                ORDER BY total_qty DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [(row["product_name"], int(row["total_qty"])) for row in rows]
