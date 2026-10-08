import unittest
from unittest.mock import MagicMock
from app.database.connection import Database
from app.database.cloud_db import CloudDatabase
from app.database.migrations import run_migrations


class TestFirebaseSyncResilience(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        run_migrations()
        cls.db = Database()
        cls.cloud_db = CloudDatabase()

    def setUp(self):
        # Create a mock Firestore client
        self.mock_firestore = MagicMock()
        self.cloud_db.firestore_db = self.mock_firestore

        # Ensure user 1 exists
        with self.db.get_connection() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO users (id, company_name, full_name, phone, email, password_hash)
                VALUES (1, 'Test İşletme', 'Test Yönetici', '05550001122', 'test@example.com', 'hash123')
                """
            )
            # Create a sample product
            conn.execute(
                """
                INSERT OR REPLACE INTO products (id, user_id, category_id, name, barcode, purchase_price, sale_price, stock_quantity, critical_stock_level, unit, is_active, synced_to_cloud)
                VALUES (101, 1, 1, 'Örnek Ürün', 'BAR101', 50.0, 100.0, 50.0, 5.0, 'adet', 1, 1)
                """
            )
            conn.execute("DELETE FROM sale_items")
            conn.execute("DELETE FROM sales")

    def test_add_sale_instant_firestore_sync(self):
        """add_sale should immediately set the sale document in Firestore and mark synced_to_cloud = 1."""
        cart_items = [
            {
                "product_id": 101,
                "product_name": "Örnek Ürün",
                "barcode": "BAR101",
                "unit_price": 100.0,
                "quantity": 2.0,
                "subtotal": 200.0,
                "unit": "adet",
            }
        ]
        ok, msg = self.cloud_db.add_sale(
            cart_items,
            note="Test Satışı",
            user_id=1,
            channel="magaza",
            total_amount_override=200.0,
            customer_name="Ali Veli",
        )
        self.assertTrue(ok)

        # Check Firestore collection("sales").document(...).set was called
        sales_col = self.mock_firestore.collection("sales")
        self.assertTrue(sales_col.document.called)

        # Verify sale in SQLite is marked as synced_to_cloud = 1
        with self.db.get_connection() as conn:
            sale_row = conn.execute("SELECT * FROM sales WHERE user_id = 1 ORDER BY id DESC LIMIT 1").fetchone()
            self.assertIsNotNone(sale_row)
            self.assertEqual(sale_row["total_amount"], 200.0)
            self.assertEqual(sale_row["customer_name"], "Ali Veli")
            self.assertEqual(sale_row["synced_to_cloud"], 1)

    def test_pull_all_from_firebase_restores_sales_and_customer_name(self):
        """Simulating container wake-up: SQLite is empty, pull_all_from_firebase restores sales and customer names."""
        # Setup mock Firestore sales docs
        mock_doc = MagicMock()
        mock_doc.id = "u1_s999"
        mock_doc.to_dict.return_value = {
            "id": 999,
            "user_id": 1,
            "total_amount": 5250.0,
            "item_count": 5,
            "sold_at": "2026-10-08 14:30:00",
            "note": "Bugünkü 5250 TL Satış",
            "channel": "magaza",
            "customer_name": "Değerli Müşteri",
            "items": [
                {
                    "product_id": 101,
                    "product_name": "Örnek Ürün",
                    "barcode": "BAR101",
                    "unit_price": 1050.0,
                    "quantity": 5.0,
                    "subtotal": 5250.0,
                    "unit": "adet",
                }
            ],
        }
        self.mock_firestore.collection.return_value.stream.return_value = [mock_doc]

        # Ensure SQLite has no sales (simulating fresh container)
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM sale_items")
            conn.execute("DELETE FROM sales")

        count = self.cloud_db.pull_all_from_firebase(user_id=1)
        self.assertGreater(count, 0)

        # Verify sale in SQLite
        with self.db.get_connection() as conn:
            sale = conn.execute("SELECT * FROM sales WHERE id = 999").fetchone()
            self.assertIsNotNone(sale)
            self.assertEqual(sale["total_amount"], 5250.0)
            self.assertEqual(sale["customer_name"], "Değerli Müşteri")
            self.assertEqual(sale["synced_to_cloud"], 1)

            # Check items
            items = conn.execute("SELECT * FROM sale_items WHERE sale_id = 999").fetchall()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["unit"], "adet")
            self.assertEqual(items[0]["quantity"], 5.0)

            # Check sequence updated
            seq_row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'sales'").fetchone()
            self.assertIsNotNone(seq_row)
            self.assertGreaterEqual(seq_row[0], 999)

    def test_get_sales_history_auto_hydrates_when_empty(self):
        """get_sales_history should automatically pull from Firestore if SQLite has no sales."""
        # Setup mock Firestore sales
        mock_doc = MagicMock()
        mock_doc.id = "u1_s888"
        mock_doc.to_dict.return_value = {
            "id": 888,
            "user_id": 1,
            "total_amount": 5250.0,
            "item_count": 1,
            "sold_at": "2026-10-08 15:00:00",
            "note": "Günün Satışı",
            "channel": "magaza",
            "customer_name": "Müşteri Ahmet",
            "items": [],
        }
        self.mock_firestore.collection.return_value.stream.return_value = [mock_doc]

        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM sale_items")
            conn.execute("DELETE FROM sales")

        history = self.cloud_db.get_sales_history(user_id=1)
        self.assertGreaterEqual(len(history), 1)
        self.assertEqual(history[0]["total_amount"], 5250.0)
        self.assertEqual(history[0]["customer_name"], "Müşteri Ahmet")


if __name__ == "__main__":
    unittest.main()
