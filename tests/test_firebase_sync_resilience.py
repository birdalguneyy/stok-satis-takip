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
        self.cloud_db._hydration_completed = False
        self.cloud_db._last_pull_timestamp = 0.0

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

    def test_push_offline_data_makes_zero_reads_when_no_unsynced(self):
        """When all local records have synced_to_cloud = 1, push_offline_data_to_firebase must not touch Firestore (0 READ / 0 WRITE)."""
        with self.db.get_connection() as conn:
            for table in ["users", "categories", "products", "sales", "expenses", "farm_customers", "farm_egg_sales", "farm_feed_purchases"]:
                try:
                    conn.execute(f"UPDATE {table} SET synced_to_cloud = 1")
                except Exception:
                    pass

        self.mock_firestore.reset_mock()
        res = self.cloud_db.push_offline_data_to_firebase(user_id=1)
        self.assertTrue(res["synced"])
        self.assertEqual(res["pushed"], 0)
        self.assertEqual(res["pulled"], 0)
        # Verify ZERO calls to Firestore collection
        self.assertEqual(self.mock_firestore.collection.call_count, 0)

    def test_initial_hydration_skips_when_database_already_has_data(self):
        """When SQLite already has products/sales, ensure_initial_hydration must not stream Firestore (0 READ)."""
        # We already have product 101 inserted in setUp
        self.mock_firestore.reset_mock()
        self.cloud_db._hydration_completed = False
        res = self.cloud_db.ensure_initial_hydration(force=False)
        self.assertTrue(res)
        self.assertTrue(self.cloud_db._hydration_completed)
        self.assertEqual(self.mock_firestore.collection.call_count, 0)

    def test_gatekeeper_pin_and_api_lock(self):
        """Requests to /api/* must be blocked with 401 until PIN 2805 is verified."""
        from app.web.web_server import app, EXPECTED_GATE_TOKEN
        client = app.test_client()

        # 1. Unauthenticated request to /api/products is rejected
        res = client.get("/api/products")
        self.assertEqual(res.status_code, 401)
        self.assertTrue(res.get_json().get("gate_locked"))

        # 2. Verifying wrong PIN fails
        res_wrong = client.post("/api/gate/verify", json={"pin": "9999"})
        self.assertEqual(res_wrong.status_code, 401)
        self.assertFalse(res_wrong.get_json()["ok"])

        # 3. Verifying correct PIN 2805 succeeds
        res_ok = client.post("/api/gate/verify", json={"pin": "2805"})
        self.assertEqual(res_ok.status_code, 200)
        token = res_ok.get_json()["token"]
        self.assertEqual(token, EXPECTED_GATE_TOKEN)

        # 4. Request with X-Gate-Token header succeeds
        res_auth = client.get("/api/products", headers={"X-Gate-Token": token})
        self.assertEqual(res_auth.status_code, 200)

    def test_parse_cred_dict_variations(self):
        """CloudDatabase._parse_cred_dict should cleanly parse JSON strings, base64, and dicts."""
        import base64
        import json

        sample = {
            "type": "service_account",
            "project_id": "test-project-123",
            "private_key": "-----BEGIN PRIVATE KEY-----\\nMIIEvgIBADANBgkqhkiG9w0BAQEFAASC\\n-----END PRIVATE KEY-----\\n",
            "client_email": "test@test-project-123.iam.gserviceaccount.com"
        }
        # 1. Plain dict
        parsed = self.cloud_db._parse_cred_dict(sample)
        self.assertIsNotNone(parsed)
        self.assertIn("\n", parsed["private_key"])
        self.assertNotIn("\\n", parsed["private_key"])

        # 2. JSON string
        json_str = json.dumps(sample)
        parsed_str = self.cloud_db._parse_cred_dict(json_str)
        self.assertIsNotNone(parsed_str)
        self.assertEqual(parsed_str["project_id"], "test-project-123")

        # 3. Quoted JSON string (Render env var format)
        quoted_str = f"'{json_str}'"
        parsed_quoted = self.cloud_db._parse_cred_dict(quoted_str)
        self.assertIsNotNone(parsed_quoted)
        self.assertEqual(parsed_quoted["project_id"], "test-project-123")

        # 4. Base64 encoded JSON
        b64_str = base64.b64encode(json_str.encode("utf-8")).decode("utf-8")
        parsed_b64 = self.cloud_db._parse_cred_dict(b64_str)
        self.assertIsNotNone(parsed_b64)
        self.assertEqual(parsed_b64["project_id"], "test-project-123")

        # 5. Invalid string returns None
        self.assertIsNone(self.cloud_db._parse_cred_dict("not-a-json-string"))
        self.assertIsNone(self.cloud_db._parse_cred_dict(""))

    def test_sync_status_endpoint_returns_diagnostics(self):
        """The /api/sync/status endpoint should return has_firebase, last_error, and diagnostics."""
        from app.web.web_server import app, EXPECTED_GATE_TOKEN
        client = app.test_client()

        res = client.get("/api/sync/status", headers={"X-Gate-Token": EXPECTED_GATE_TOKEN})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["ok"])
        self.assertIn("has_firebase", data)
        self.assertIn("last_error", data)
        self.assertIn("project_id", data)
        self.assertIn("cred_source", data)

    def test_firebase_config_endpoint_validation(self):
        """The /api/firebase/config endpoint should reject empty or invalid JSON."""
        from app.web.web_server import app, EXPECTED_GATE_TOKEN
        client = app.test_client()

        # Empty body
        res_empty = client.post("/api/firebase/config", json={}, headers={"X-Gate-Token": EXPECTED_GATE_TOKEN})
        self.assertEqual(res_empty.status_code, 400)

        # Invalid credentials format
        res_inv = client.post("/api/firebase/config", json={"credentials": "invalid"}, headers={"X-Gate-Token": EXPECTED_GATE_TOKEN})
        self.assertEqual(res_inv.status_code, 400)
        self.assertIn("Geçersiz kimlik formatı", res_inv.get_json()["message"])


    def test_ensure_initial_hydration_runs_even_with_demo_products(self):
        """ensure_initial_hydration must NOT skip hydration if only demo products exist and sales is 0."""
        # Ensure only demo products in DB
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM sale_items")
            conn.execute("DELETE FROM sales")
            conn.execute("DELETE FROM farm_egg_sales")
            conn.execute("DELETE FROM products WHERE barcode NOT LIKE '86900000000%'")

        # Mock firestore sales doc
        mock_doc = MagicMock()
        mock_doc.id = "u1_s777"
        mock_doc.to_dict.return_value = {
            "id": 777,
            "user_id": 1,
            "total_amount": 1500.0,
            "item_count": 1,
            "sold_at": "2026-10-10 10:00:00",
            "items": [],
        }
        self.mock_firestore.collection.return_value.stream.return_value = [mock_doc]
        self.cloud_db._hydration_completed = False
        self.cloud_db._last_pull_timestamp = 0

        res = self.cloud_db.ensure_initial_hydration(force=False)
        self.assertTrue(res)
        self.assertTrue(self.cloud_db._hydration_completed)

        # Check sale was pulled into SQLite
        with self.db.get_connection() as conn:
            sale = conn.execute("SELECT * FROM sales WHERE id = 777").fetchone()
            self.assertIsNotNone(sale)
            self.assertEqual(sale["total_amount"], 1500.0)

    def test_get_farm_egg_sales_auto_pulls_when_empty(self):
        """get_farm_egg_sales must auto-pull from Firestore when local farm_egg_sales table is 0."""
        mock_doc = MagicMock()
        mock_doc.id = "u1_es555"
        mock_doc.to_dict.return_value = {
            "id": 555,
            "user_id": 1,
            "customer_name": "Ahmet Çiftlik",
            "box_count": 2.0,
            "unit_type": "koli",
            "piece_count": 60.0,
            "unit_price": 100.0,
            "total_amount": 200.0,
            "source": "Ciftlik",
            "sale_date": "2026-10-10 11:00:00",
        }
        self.mock_firestore.collection.return_value.stream.return_value = [mock_doc]
        self.cloud_db._last_pull_timestamp = 0

        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM farm_egg_sales")

        egg_sales = self.cloud_db.get_farm_egg_sales(user_id=1)
        self.assertGreaterEqual(len(egg_sales), 1)
        self.assertEqual(egg_sales[0]["customer_name"], "Ahmet Çiftlik")

    def test_pull_sales_id_collision_protection(self):
        """If a pulled Firestore sale has an ID matching a different existing sale, a new ID is assigned to avoid data loss."""
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM sale_items")
            conn.execute("DELETE FROM sales")
            conn.execute(
                """
                INSERT INTO sales (id, user_id, total_amount, item_count, sold_at, note, channel, customer_name)
                VALUES (50, 1, 100.0, 1, '2026-10-01 10:00:00', 'Eski Satış', 'magaza', 'Müşteri 1')
                """
            )

        # Pull a different sale that also claims id 50
        mock_doc = MagicMock()
        mock_doc.id = "u1_s50"
        mock_doc.to_dict.return_value = {
            "id": 50,
            "user_id": 1,
            "total_amount": 999.0,
            "item_count": 3,
            "sold_at": "2026-10-10 12:00:00",
            "note": "Yeni Farklı Satış",
            "channel": "magaza",
            "customer_name": "Müşteri 2",
            "items": [],
        }
        self.mock_firestore.collection.return_value.stream.return_value = [mock_doc]
        self.cloud_db.pull_all_from_firebase(user_id=1)

        with self.db.get_connection() as conn:
            all_sales = conn.execute("SELECT * FROM sales ORDER BY id ASC").fetchall()
            # Both sales must exist! Neither was overwritten
            self.assertEqual(len(all_sales), 2)
            ids = [s["id"] for s in all_sales]
            self.assertIn(50, ids)
            self.assertIn(51, ids)


if __name__ == "__main__":
    unittest.main()
