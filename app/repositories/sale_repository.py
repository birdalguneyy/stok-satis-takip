from typing import List, Optional, Tuple

from app.database.connection import Database
from app.models.cart_item import CartItem
from app.models.sale import Sale, SaleItem


class SaleRepository:
    def __init__(self, db: Optional[Database] = None) -> None:
        self.db = db or Database()

    def create_sale(
        self,
        cart_items: List[CartItem],
        note: Optional[str] = None,
        channel: str = "magaza",
        total_amount_override: Optional[float] = None,
        customer_name: Optional[str] = None,
    ) -> Sale:
        calc_total = sum(item.subtotal for item in cart_items)
        total_amount = round(total_amount_override, 2) if total_amount_override is not None else round(calc_total, 2)
        item_count = sum(item.quantity for item in cart_items)
        ch = (channel or "magaza").strip().lower()
        cust_name = customer_name.strip() if customer_name and str(customer_name).strip() else None

        with self.db.get_connection() as conn:
            cursor = conn.execute(
                "INSERT INTO sales (total_amount, item_count, note, channel, customer_name) VALUES (?, ?, ?, ?, ?)",
                (total_amount, item_count, note, ch, cust_name),
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

    def get_sales_history(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        customer_name: Optional[str] = None,
    ) -> List[dict]:
        query = "SELECT * FROM sales WHERE 1=1"
        params: list = []
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

    def delete_sale(self, sale_id: int, restore_stock: bool = True) -> Tuple[bool, str]:
        ok, msg, _ = self.delete_sales_bulk([sale_id], restore_stock=restore_stock)
        return ok, msg

    def delete_sales_bulk(self, sale_ids: List[int], restore_stock: bool = True) -> Tuple[bool, str, int]:
        if not sale_ids:
            return False, "Satış seçilmedi", 0

        cleaned_ids = [int(sid) for sid in sale_ids if sid]
        if not cleaned_ids:
            return False, "Geçersiz satış listesi", 0

        with self.db.get_connection() as conn:
            placeholders = ",".join("?" for _ in cleaned_ids)
            valid_sales = conn.execute(f"SELECT id FROM sales WHERE id IN ({placeholders})", cleaned_ids).fetchall()
            valid_ids = [r["id"] for r in valid_sales]

            if not valid_ids:
                return False, "Seçilen satışlar bulunamadı", 0

            if restore_stock:
                v_placeholders = ",".join("?" for _ in valid_ids)
                items = conn.execute(f"SELECT product_id, quantity FROM sale_items WHERE sale_id IN ({v_placeholders})", valid_ids).fetchall()
                for it in items:
                    if it["product_id"]:
                        conn.execute(
                            """
                            UPDATE products
                            SET stock_quantity = stock_quantity + ?, updated_at = datetime('now', 'localtime')
                            WHERE id = ?
                            """,
                            (it["quantity"], it["product_id"]),
                        )

            v_placeholders = ",".join("?" for _ in valid_ids)
            conn.execute(f"DELETE FROM sale_items WHERE sale_id IN ({v_placeholders})", valid_ids)
            conn.execute(f"DELETE FROM sales WHERE id IN ({v_placeholders})", valid_ids)

        try:
            from app.database.cloud_db import CloudDatabase
            cloud_db = CloudDatabase()
            if cloud_db.firestore_db:
                for sid in valid_ids:
                    try:
                        cloud_db.firestore_db.collection("sales").document(f"u1_s{sid}").delete()
                        cloud_db.firestore_db.collection("sales").document(str(sid)).delete()
                    except Exception:
                        pass
        except Exception:
            pass

        return True, f"{len(valid_ids)} satış silindi ve stoklar iade edildi", len(valid_ids)

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
