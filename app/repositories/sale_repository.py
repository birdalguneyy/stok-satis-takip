from typing import List, Optional, Tuple

from app.config import get_turkey_now_str
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
        sold_at: Optional[str] = None,
    ) -> Sale:
        calc_total = sum(item.subtotal for item in cart_items)
        total_amount = round(total_amount_override, 2) if total_amount_override is not None else round(calc_total, 2)
        item_count = sum(item.quantity for item in cart_items)
        ch = (channel or "magaza").strip().lower()
        cust_name = customer_name.strip() if customer_name and str(customer_name).strip() else None
        now = sold_at.strip() if sold_at and str(sold_at).strip() else get_turkey_now_str()

        with self.db.get_connection() as conn:
            cursor = conn.execute(
                "INSERT INTO sales (total_amount, item_count, sold_at, note, channel, customer_name) VALUES (?, ?, ?, ?, ?, ?)",
                (total_amount, item_count, now, note, ch, cust_name),
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

        # Dükkan satışındaki yumurtaları otomatik Çiftlik kaydına aktar
        try:
            from app.database.cloud_db import CloudDatabase
            from app.services.farm_service import parse_egg_product
            cloud_db = CloudDatabase()
            discount_ratio = (total_amount / calc_total) if (total_amount_override is not None and calc_total > 0) else 1.0
            for item in cart_items:
                p_name = str(item.product_name or "").strip()
                if "yumurta" in p_name.lower():
                    orig_unit_price = float(item.unit_price)
                    qty = float(item.quantity)
                    effective_item_total = round(orig_unit_price * qty * discount_ratio, 2)

                    u_type, egg_boxes, egg_pieces = parse_egg_product(p_name, qty)
                    if u_type == "adet" and egg_pieces > 0:
                        effective_price = round(effective_item_total / egg_pieces, 2)
                    elif egg_boxes > 0:
                        effective_price = round(effective_item_total / egg_boxes, 2)
                    else:
                        effective_price = round(orig_unit_price * discount_ratio, 2)

                    egg_cust = cust_name or "Dükkan Müşterisi"
                    egg_note = f"Dükkan Satışı (Fiş #{sale_id}) - {p_name}"
                    cloud_db.add_farm_egg_sale(
                        customer_name=egg_cust,
                        box_count=egg_boxes,
                        unit_price=effective_price,
                        source="Dükkan",
                        note=egg_note,
                        unit_type=u_type,
                        piece_count=egg_pieces,
                        total_amount=effective_item_total,
                    )
        except Exception:
            pass

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
        query += " ORDER BY sold_at DESC, id DESC LIMIT 500"

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

            affected_product_ids = set()
            if restore_stock:
                v_placeholders = ",".join("?" for _ in valid_ids)
                items = conn.execute(f"SELECT product_id, barcode, quantity FROM sale_items WHERE sale_id IN ({v_placeholders})", valid_ids).fetchall()
                for it in items:
                    pid = it["product_id"]
                    barcode = it["barcode"]
                    qty = float(it["quantity"] or 0)
                    if qty <= 0:
                        continue

                    updated = False
                    if pid:
                        cur = conn.execute(
                            """
                            UPDATE products
                            SET stock_quantity = stock_quantity + ?, updated_at = datetime('now', 'localtime'), synced_to_cloud = 0
                            WHERE id = ?
                            """,
                            (qty, pid),
                        )
                        if cur.rowcount > 0:
                            updated = True
                            affected_product_ids.add(pid)

                    if not updated and barcode:
                        cur = conn.execute(
                            """
                            UPDATE products
                            SET stock_quantity = stock_quantity + ?, updated_at = datetime('now', 'localtime'), synced_to_cloud = 0
                            WHERE barcode = ?
                            """,
                            (qty, barcode),
                        )
                        if cur.rowcount > 0:
                            p_row = conn.execute("SELECT id FROM products WHERE barcode = ?", (barcode,)).fetchone()
                            if p_row:
                                affected_product_ids.add(p_row["id"])

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

                if restore_stock and affected_product_ids:
                    with self.db.get_connection() as conn:
                        for pid in affected_product_ids:
                            p_row = conn.execute("SELECT stock_quantity, unit FROM products WHERE id = ?", (pid,)).fetchone()
                            if p_row:
                                try:
                                    cloud_db.firestore_db.collection("products").document(f"u1_p{pid}").update({
                                        "stock_quantity": float(p_row["stock_quantity"]),
                                        "unit": p_row["unit"],
                                        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                    })
                                    conn.execute("UPDATE products SET synced_to_cloud = 1 WHERE id = ?", (pid,))
                                except Exception:
                                    pass
        except Exception:
            pass

        return True, f"{len(valid_ids)} satış silindi ve stoklar iade edildi", len(valid_ids)

    def update_sale_datetime(self, sale_id: int, new_sold_at: str) -> Tuple[bool, str]:
        """Satış kaydının tarih/saatini günceller ve buluta yansıtır."""
        if not new_sold_at or not str(new_sold_at).strip():
            return False, "Geçersiz tarih ve saat."

        raw = str(new_sold_at).strip().replace("T", " ")
        try:
            if len(raw) == 10:
                formatted_dt = f"{raw} 12:00:00"
            elif len(raw) == 16:
                formatted_dt = f"{raw}:00"
            elif len(raw) >= 19:
                formatted_dt = raw[:19]
            else:
                formatted_dt = raw
        except Exception:
            return False, "Geçersiz tarih formatı."

        with self.db.get_connection() as conn:
            cur = conn.execute("UPDATE sales SET sold_at = ?, synced_to_cloud = 0 WHERE id = ?", (formatted_dt, sale_id))
            if cur.rowcount == 0:
                return False, "Satış kaydı bulunamadı."

        try:
            from app.database.cloud_db import CloudDatabase
            cloud_db = CloudDatabase()
            if cloud_db.firestore_db:
                cloud_db.firestore_db.collection("sales").document(f"u1_s{sale_id}").update({
                    "sold_at": formatted_dt,
                    "updated_at": get_turkey_now_str(),
                })
                with self.db.get_connection() as conn:
                    conn.execute("UPDATE sales SET synced_to_cloud = 1 WHERE id = ?", (sale_id,))
        except Exception:
            pass

        return True, "Satış tarihi güncellendi."

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
