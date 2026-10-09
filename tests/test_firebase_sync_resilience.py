import unittest
from unittest.mock import MagicMock
from app.database.connection import Database
from app.database.cloud_db import CloudDatabase
from app.database.migrations import run_migrations


class TestFirebaseSyncResilience(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.config import DATA_DIR
        cls.test_db_path = DATA_DIR / "test_resilience.db"
        if cls.test_db_path.exists():
            try:
                cls.test_db_path.unlink()
            except Exception:
                pass
        cls.db = Database.reset_instance(cls.test_db_path)
        run_migrations()
        cls.cloud_db = CloudDatabase()
        cls.cloud_db.db = cls.db

    @classmethod
    def tearDownClass(cls):
        Database.reset_instance()
        if hasattr(cls, "test_db_path") and cls.test_db_path.exists():
            try:
                cls.test_db_path.unlink()
            except Exception:
                pass

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

    def test_iso_date_with_t_matches_today_filters(self):
        """Sale with ISO format (2026-10-09T14:30:00) must match start_date and end_date queries without being dropped."""
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM sale_items")
            conn.execute("DELETE FROM sales")
            cursor = conn.execute(
                """
                INSERT INTO sales (id, user_id, total_amount, item_count, sold_at, note, channel, customer_name)
                VALUES (777, 1, 5250.0, 1, '2026-10-09T14:30:00', 'Bugünkü Satış', 'magaza', 'Mehmet Bey')
                """
            )
            conn.execute(
                """
                INSERT INTO sale_items (sale_id, product_id, product_name, barcode, unit_price, quantity, subtotal, unit)
                VALUES (777, 101, 'Örnek Ürün', 'BAR101', 5250.0, 1.0, 5250.0, 'adet')
                """
            )

        # 1. get_sales_history with today filter
        hist = self.cloud_db.get_sales_history(user_id=1, start_date="2026-10-09", end_date="2026-10-09")
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["total_amount"], 5250.0)
        self.assertEqual(hist[0]["customer_name"], "Mehmet Bey")

        # 2. get_sales_analytics with today filter
        analytics = self.cloud_db.get_sales_analytics(user_id=1, start_date="2026-10-09", end_date="2026-10-09")
        self.assertEqual(analytics["total_revenue"], 5250.0)
        self.assertEqual(analytics["total_transactions"], 1)

    def test_pull_all_from_firebase_type_mismatch_resilience(self):
        """Categories, products, and expenses with string '1' in Firestore must be pulled cleanly when user_id is integer 1."""
        cat_doc = MagicMock()
        cat_doc.id = "u1_c50"
        cat_doc.to_dict.return_value = {"id": 50, "user_id": "1", "name": "Bulut Kategori"}

        prod_doc = MagicMock()
        prod_doc.id = "u1_p200"
        prod_doc.to_dict.return_value = {
            "id": 200,
            "user_id": "1",
            "category_id": 50,
            "name": "Bulut Ürün",
            "barcode": "BAR200",
            "sale_price": 75.0,
            "stock_quantity": 20.0,
        }

        exp_doc = MagicMock()
        exp_doc.id = "u1_e60"
        exp_doc.to_dict.return_value = {
            "id": 60,
            "user_id": "1",
            "title": "Elektrik Faturası",
            "amount": 450.0,
            "category": "Fatura",
            "expense_date": "2026-10-09",
        }

        def mock_stream(col_name):
            if col_name == "categories":
                return [cat_doc]
            elif col_name == "products":
                return [prod_doc]
            elif col_name == "expenses":
                return [exp_doc]
            return []

        self.mock_firestore.collection.side_effect = lambda name: MagicMock(stream=lambda: mock_stream(name), document=MagicMock())

        pulled = self.cloud_db.pull_all_from_firebase(user_id=1)
        self.assertGreaterEqual(pulled, 3)

        # Verify in SQLite
        with self.db.get_connection() as conn:
            cat = conn.execute("SELECT * FROM categories WHERE id = 50").fetchone()
            self.assertIsNotNone(cat)
            self.assertEqual(cat["name"], "Bulut Kategori")

            prod = conn.execute("SELECT * FROM products WHERE id = 200").fetchone()
            self.assertIsNotNone(prod)
            self.assertEqual(prod["name"], "Bulut Ürün")

            exp = conn.execute("SELECT * FROM expenses WHERE id = 60").fetchone()
            self.assertIsNotNone(exp)
            self.assertEqual(exp["amount"], 450.0)

    def test_products_auto_hydration_when_empty(self):
        """get_products should auto-pull from Firestore if local products table is empty."""
        prod_doc = MagicMock()
        prod_doc.id = "u1_p300"
        prod_doc.to_dict.return_value = {
            "id": 300,
            "user_id": 1,
            "category_id": 1,
            "name": "Otomatik Çekilen Ürün",
            "barcode": "AUTO300",
            "sale_price": 120.0,
            "stock_quantity": 15.0,
        }
        self.mock_firestore.collection.side_effect = lambda name: MagicMock(
            stream=lambda: [prod_doc] if name == "products" else [],
            document=MagicMock()
        )

        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM products")

        products = self.cloud_db.get_products(user_id=1)
        self.assertGreaterEqual(len(products), 1)
        self.assertTrue(any(p["name"] == "Otomatik Çekilen Ürün" for p in products))

    def test_sync_friendly_toast_reason_when_zero_changes(self):
        """When pushed=0 and pulled=0, reason must be reassuring and positive rather than '0 veri'."""
        with self.db.get_connection() as conn:
            for table in ["users", "categories", "products", "sales", "expenses", "farm_customers", "farm_egg_sales", "farm_feed_purchases"]:
                try:
                    conn.execute(f"UPDATE {table} SET synced_to_cloud = 1")
                except Exception:
                    pass

        self.mock_firestore.collection.side_effect = lambda name: MagicMock(
            stream=lambda: [],
            document=MagicMock()
        )
        res = self.cloud_db.sync_offline_data_with_firebase(user_id=1)
        self.assertTrue(res["synced"])
        self.assertIn("✅", res["reason"])
        self.assertNotIn("0 veri aktarıldı, 0 veri indirildi", res["reason"])


if __name__ == "__main__":
    unittest.main()
