import sqlite3
import unittest

from app.config import DATA_DIR, GATE_PIN, STAFF_PIN, get_turkey_now_str
from app.database.cloud_db import CloudDatabase
from app.database.connection import Database
from app.database.migrations import run_migrations
from app.services.security_service import log_audit
from app.web.web_server import (
    EXPECTED_GATE_TOKEN,
    EXPECTED_STAFF_GATE_TOKEN,
    _sale_idempotency_cache,
    app,
    blocked_ips,
    failed_pin_attempts,
)


class TestSecurityInfrastructure(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_db_path = DATA_DIR / "test_security_infra.db"
        if cls.test_db_path.exists():
            cls.test_db_path.unlink()
        cls.db = Database.reset_instance(cls.test_db_path)
        run_migrations()
        cls.cloud_db = CloudDatabase()
        cls.cloud_db.db = cls.db
        cls.cloud_db.firestore_db = None
        cls.client = app.test_client()

    @classmethod
    def tearDownClass(cls):
        Database.reset_instance()
        if hasattr(cls, "test_db_path") and cls.test_db_path.exists():
            try:
                cls.test_db_path.unlink()
            except Exception:
                pass

    def setUp(self):
        failed_pin_attempts.clear()
        blocked_ips.clear()
        _sale_idempotency_cache.clear()
        self.patron_headers = {
            "X-Gate-Token": EXPECTED_GATE_TOKEN,
            "User-Agent": "Mozilla/5.0",
        }
        self.staff_headers = {
            "X-Gate-Token": EXPECTED_STAFF_GATE_TOKEN,
            "User-Agent": "Mozilla/5.0",
        }
        with self.db.get_connection() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO users (id, company_name, full_name, phone, email, password_hash, role)
                VALUES (1, 'Test Market', 'Patron Ahmet', '05551112233', 'patron@test.com', '', 'patron')
                """
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO products (
                    id, user_id, category_id, name, barcode, purchase_price, sale_price,
                    stock_quantity, critical_stock_level, unit, is_active, synced_to_cloud
                ) VALUES (501, 1, 1, 'Süt 1L', '8690001', 25.0, 40.0, 20.0, 5.0, 'adet', 1, 1)
                """
            )
            conn.execute("DELETE FROM sale_items")
            conn.execute("DELETE FROM sales")
            conn.execute("DELETE FROM expenses")
            conn.execute("DELETE FROM cash_movements")
            conn.execute("DELETE FROM cash_days")
            conn.execute("DELETE FROM farm_egg_sales")
            conn.execute("DELETE FROM farm_feed_purchases")
        # Satış testlerinin çalışabilmesi için bugünün kasasını aç
        self.cloud_db.open_cash_day(opening_amount=250.0, user_id=1, actor={"name": "Patron Ahmet", "role": "patron"})

    # 1) DENETİM KAYDI (AUDIT LOG) VE DEĞİŞMEZLİK TESTİ
    def test_audit_log_immutability_and_central_helper(self):
        audit_id = log_audit(
            action="CUSTOM_MODULE_TEST",
            entity_type="future_module",
            entity_id="FM-42",
            old_value={"balance": 100},
            new_value={"balance": 250},
            reason="Gelecek modül testi",
            actor={"name": "Patron Ahmet", "role": "patron"},
            user_id=1,
            db=self.db,
        )
        self.assertIsNotNone(audit_id)

        # UPDATE engellenmeli
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.get_connection() as conn:
                conn.execute("UPDATE audit_logs SET reason = 'Değiştirildi' WHERE id = ?", (audit_id,))

        # DELETE engellenmeli
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.get_connection() as conn:
                conn.execute("DELETE FROM audit_logs WHERE id = ?", (audit_id,))

    # 2) SİLME YERİNE İPTAL AKIŞI + ZORUNLU NEDEN + TOPLAM/BAKİYE HARİÇ TUTMA
    def test_soft_cancel_sale_expense_cash_and_cancelled_report(self):
        # 2a. Satış oluştur (2 adet Süt = 80 TL, stok 20 -> 18)
        res_sale = self.client.post(
            "/api/sales",
            headers=self.patron_headers,
            json={
                "items": [{
                    "product_id": 501,
                    "product_name": "Süt 1L",
                    "barcode": "8690001",
                    "unit_price": 40.0,
                    "quantity": 2,
                    "subtotal": 80.0,
                    "unit": "adet",
                }],
                "total_amount": 80.0,
                "customer_name": "Ayşe Hanım",
            },
        )
        self.assertEqual(res_sale.status_code, 200)
        self.assertTrue(res_sale.get_json()["ok"])

        with self.db.get_connection() as conn:
            sale_id = conn.execute("SELECT id FROM sales ORDER BY id DESC LIMIT 1").fetchone()["id"]
            stock_after_sale = conn.execute("SELECT stock_quantity FROM products WHERE id = 501").fetchone()["stock_quantity"]
        self.assertEqual(stock_after_sale, 18.0)

        # 2b. Nedensiz iptal reddedilmeli (400)
        res_no_reason = self.client.post(
            f"/api/sales/{sale_id}/cancel",
            headers=self.patron_headers,
            json={"cancel_reason": ""},
        )
        self.assertEqual(res_no_reason.status_code, 400)

        # 2c. 'Diğer' seçilip not boş bırakılırsa reddedilmeli (400)
        res_other_empty = self.client.post(
            f"/api/sales/{sale_id}/cancel",
            headers=self.patron_headers,
            json={"cancel_reason": "Diğer", "cancel_note": ""},
        )
        self.assertEqual(res_other_empty.status_code, 400)

        # 2d. Geçerli neden + not ile iptal et
        res_cancel = self.client.post(
            f"/api/sales/{sale_id}/cancel",
            headers=self.patron_headers,
            json={
                "cancel_reason": "Müşteri iadesi",
                "cancel_note": "Süt paketi delik çıktı",
                "restore_stock": True,
            },
        )
        self.assertEqual(res_cancel.status_code, 200)
        self.assertTrue(res_cancel.get_json()["ok"])

        # Fiziksel silinmemiş, status='cancelled' ve stok 20'ye dönmüş olmalı
        with self.db.get_connection() as conn:
            s_row = conn.execute("SELECT * FROM sales WHERE id = ?", (sale_id,)).fetchone()
            stock_after_cancel = conn.execute("SELECT stock_quantity FROM products WHERE id = 501").fetchone()["stock_quantity"]
        self.assertIsNotNone(s_row)
        self.assertEqual(s_row["status"], "cancelled")
        self.assertEqual(s_row["cancel_reason"], "Müşteri iadesi")
        self.assertEqual(s_row["cancel_note"], "Süt paketi delik çıktı")
        self.assertEqual(stock_after_cancel, 20.0)

        # İkinci kez iptal reddedilmeli ve stok çift eklenmemeli
        res_double = self.client.post(
            f"/api/sales/{sale_id}/cancel",
            headers=self.patron_headers,
            json={"cancel_reason": "Müşteri iadesi"},
        )
        self.assertEqual(res_double.status_code, 400)
        with self.db.get_connection() as conn:
            self.assertEqual(conn.execute("SELECT stock_quantity FROM products WHERE id = 501").fetchone()["stock_quantity"], 20.0)

        # 2e. Gider ekle ve iptal et
        res_exp = self.client.post(
            "/api/expenses",
            headers=self.patron_headers,
            json={"title": "Elektrik Faturası", "amount": 500.0, "category": "Fatura"},
        )
        self.assertEqual(res_exp.status_code, 200)
        exp_id = res_exp.get_json()["expense"]["id"]
        res_exp_cancel = self.client.post(
            f"/api/expenses/{exp_id}/cancel",
            headers=self.patron_headers,
            json={"cancel_reason": "Mükerrer (çift) kayıt", "cancel_note": "İki kez girilmiş"},
        )
        self.assertEqual(res_exp_cancel.status_code, 200)

        # 2f. Tahsilat ekle ve iptal et
        res_cash = self.client.post(
            "/api/cash-movements",
            headers=self.patron_headers,
            json={"movement_type": "tahsilat", "amount": 300.0, "description": "Veresiye tahsilatı"},
        )
        self.assertEqual(res_cash.status_code, 200)
        cash_id = res_cash.get_json()["movement"]["id"]
        res_cash_cancel = self.client.post(
            f"/api/cash-movements/{cash_id}/cancel",
            headers=self.patron_headers,
            json={"cancel_reason": "Yanlış tutar / miktar girişi"},
        )
        self.assertEqual(res_cash_cancel.status_code, 200)

        # 2g. İptal edilenler hiçbir toplam/bakiye hesabına dahil edilmemeli
        res_an = self.client.get("/api/sales/analytics", headers=self.patron_headers)
        an = res_an.get_json()["analytics"]
        self.assertEqual(an["total_revenue"], 0.0)
        self.assertEqual(an["total_transactions"], 0)
        self.assertEqual(an["total_expenses"], 0.0)
        self.assertEqual(an["cancelled_sales_count"], 1)
        self.assertEqual(an["cancelled_sales_amount"], 80.0)

        res_cash_list = self.client.get("/api/cash-movements", headers=self.patron_headers)
        self.assertEqual(res_cash_list.get_json()["summary"]["net_cash"], 0.0)

        # 2h. Patron için İptal Edilenler Raporu 3 kaydı da göstermeli
        res_rep = self.client.get("/api/reports/cancelled", headers=self.patron_headers)
        self.assertEqual(res_rep.status_code, 200)
        rep = res_rep.get_json()["report"]
        self.assertEqual(rep["total_count"], 3)
        self.assertEqual(rep["total_amount"], 880.0)

    # 3) KULLANICI ROLLERİ (ÇALIŞAN vs PATRON) VE YETKİSİZ ERİŞİM LOGLAMA
    def test_roles_employee_vs_patron_and_unauthorized_audit(self):
        # PIN girişleri doğru rolleri döndürmeli
        r_staff_pin = self.client.post("/api/gate/verify", json={"pin": STAFF_PIN}, environ_overrides={"REMOTE_ADDR": "10.0.0.5"})
        self.assertEqual(r_staff_pin.status_code, 200)
        self.assertEqual(r_staff_pin.get_json()["role"], "calisan")

        r_patron_pin = self.client.post("/api/gate/verify", json={"pin": GATE_PIN}, environ_overrides={"REMOTE_ADDR": "10.0.0.6"})
        self.assertEqual(r_patron_pin.status_code, 200)
        self.assertEqual(r_patron_pin.get_json()["role"], "patron")

        # Çalışan satış girebilir
        r_sale = self.client.post(
            "/api/sales",
            headers=self.staff_headers,
            json={
                "items": [{
                    "product_id": 501,
                    "product_name": "Süt 1L",
                    "barcode": "8690001",
                    "unit_price": 40.0,
                    "quantity": 1,
                    "subtotal": 40.0,
                    "unit": "adet",
                }],
                "total_amount": 40.0,
            },
        )
        self.assertEqual(r_sale.status_code, 200)
        with self.db.get_connection() as conn:
            sale_id = conn.execute("SELECT id FROM sales ORDER BY id DESC LIMIT 1").fetchone()["id"]

        # Çalışan ürünleri görebilir ama alış fiyatını (purchase_price) GÖREMEZ
        r_prods = self.client.get("/api/products", headers=self.staff_headers)
        self.assertEqual(r_prods.status_code, 200)
        self.assertNotIn("purchase_price", r_prods.get_json()[0])

        # Çalışan bugünün satışlarını görebilir
        today_str = get_turkey_now_str("%Y-%m-%d")
        r_today = self.client.get(f"/api/sales/history?start_date={today_str}&end_date={today_str}", headers=self.staff_headers)
        self.assertEqual(r_today.status_code, 200)

        # Çalışan için yasaklı tüm işlemler 403 dönmeli ve audit_logs'a UNAUTHORIZED_ATTEMPT yazmalı
        forbidden_calls = [
            ("GET", "/api/sales/analytics", None),
            ("GET", "/api/sales/forecast", None),
            ("GET", "/api/sales/history?start_date=2025-01-01&end_date=2025-01-01", None),
            ("POST", f"/api/sales/{sale_id}/cancel", {"cancel_reason": "Müşteri iadesi"}),
            ("POST", f"/api/sales/{sale_id}/datetime", {"sold_at": "2025-01-01 10:00:00"}),
            ("GET", "/api/reports/cancelled", None),
            ("GET", "/api/audit-logs", None),
            ("POST", "/api/day/reopen", {"date": today_str, "reason": "Deneme"}),
            ("POST", "/api/expenses", {"title": "Test", "amount": 50}),
        ]
        for method, path, payload in forbidden_calls:
            if method == "GET":
                resp = self.client.get(path, headers=self.staff_headers)
            else:
                resp = self.client.post(path, headers=self.staff_headers, json=payload)
            self.assertEqual(resp.status_code, 403, f"Expected 403 for Çalışan on {method} {path}")

        # Audit log'da yetkisiz denemeler kayıtlı olmalı
        r_logs = self.client.get("/api/audit-logs?action=UNAUTHORIZED_ATTEMPT", headers=self.patron_headers)
        self.assertEqual(r_logs.status_code, 200)
        unauth_logs = r_logs.get_json()["logs"]
        self.assertGreaterEqual(len(unauth_logs), len(forbidden_calls))

        # Patron günü yeniden açabilir
        r_reopen = self.client.post(
            "/api/day/reopen",
            headers=self.patron_headers,
            json={"date": today_str, "reason": "Kasa sayım düzeltmesi"},
        )
        self.assertEqual(r_reopen.status_code, 200)
        self.assertTrue(r_reopen.get_json()["ok"])

    # 4) NEGATİF / AŞIRI TUTAR DOĞRULAMA VE ONAY UYARISI
    def test_negative_and_high_amount_validation(self):
        # Negatif satış tutarı -> 400
        r_neg = self.client.post(
            "/api/sales",
            headers=self.patron_headers,
            json={
                "items": [{"product_id": 501, "product_name": "Süt 1L", "barcode": "8690001", "unit_price": 40.0, "quantity": 1, "subtotal": 40.0}],
                "total_amount": -100.0,
            },
        )
        self.assertEqual(r_neg.status_code, 400)

        # Aşırı tutar (75.000 TL) onaysız -> 409 + requires_confirmation=True
        r_high = self.client.post(
            "/api/sales",
            headers=self.patron_headers,
            json={
                "items": [{"product_id": 501, "product_name": "Süt 1L", "barcode": "8690001", "unit_price": 75000.0, "quantity": 1, "subtotal": 75000.0}],
                "total_amount": 75000.0,
            },
        )
        self.assertEqual(r_high.status_code, 409)
        self.assertTrue(r_high.get_json().get("requires_confirmation"))

        # Onaylı tekrar gönderim -> 200
        r_high_ok = self.client.post(
            "/api/sales",
            headers=self.patron_headers,
            json={
                "items": [{"product_id": 501, "product_name": "Süt 1L", "barcode": "8690001", "unit_price": 75000.0, "quantity": 1, "subtotal": 75000.0}],
                "total_amount": 75000.0,
                "confirm_high_amount": True,
                "idempotency_key": "high_confirmed_1",
            },
        )
        self.assertEqual(r_high_ok.status_code, 200)
        self.assertTrue(r_high_ok.get_json()["ok"])

        # Negatif gider -> 400
        r_neg_exp = self.client.post(
            "/api/expenses",
            headers=self.patron_headers,
            json={"title": "Hatalı Gider", "amount": -50.0},
        )
        self.assertEqual(r_neg_exp.status_code, 400)

    # 5) MEVCUT VERİLERİ BOZMAYAN MIGRATION TESTİ
    def test_non_destructive_migration_from_v1(self):
        legacy_db_path = DATA_DIR / "test_legacy_v1.db"
        if legacy_db_path.exists():
            legacy_db_path.unlink()
        try:
            conn = sqlite3.connect(str(legacy_db_path))
            conn.executescript(
                """
                CREATE TABLE schema_version (version INTEGER NOT NULL);
                INSERT INTO schema_version VALUES (1);
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    company_name TEXT NOT NULL, full_name TEXT NOT NULL,
                    phone TEXT NOT NULL UNIQUE, email TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL, auth_token TEXT UNIQUE
                );
                INSERT INTO users (id, company_name, full_name, phone, email, password_hash)
                VALUES (1, 'Eski Market', 'Sahip', '05001112233', 'eski@market.com', 'h');
                CREATE TABLE categories (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL);
                CREATE TABLE products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, category_id INTEGER NOT NULL,
                    name TEXT NOT NULL, barcode TEXT NOT NULL,
                    purchase_price REAL NOT NULL, sale_price REAL NOT NULL,
                    stock_quantity REAL NOT NULL DEFAULT 0, critical_stock_level REAL NOT NULL DEFAULT 5,
                    is_active INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE sales (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, total_amount REAL NOT NULL,
                    item_count INTEGER NOT NULL, sold_at TEXT NOT NULL, note TEXT
                );
                INSERT INTO sales (id, total_amount, item_count, sold_at, note)
                VALUES (10, 250.0, 2, '2026-09-01 12:00:00', 'Eski satış');
                CREATE TABLE sale_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, sale_id INTEGER NOT NULL,
                    product_id INTEGER NOT NULL, product_name TEXT NOT NULL,
                    barcode TEXT NOT NULL, unit_price REAL NOT NULL,
                    quantity REAL NOT NULL, subtotal REAL NOT NULL
                );
                CREATE TABLE expenses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
                    amount REAL NOT NULL, category TEXT, expense_date TEXT NOT NULL, note TEXT
                );
                INSERT INTO expenses (id, title, amount, category, expense_date)
                VALUES (5, 'Kira', 10000.0, 'Kira', '2026-09-01');
                """
            )
            conn.commit()
            conn.close()

            # Migration çalıştır
            migrated_db = Database.reset_instance(legacy_db_path)
            run_migrations()

            with migrated_db.get_connection() as c:
                sale = c.execute("SELECT * FROM sales WHERE id = 10").fetchone()
                exp = c.execute("SELECT * FROM expenses WHERE id = 5").fetchone()
                user = c.execute("SELECT * FROM users WHERE id = 1").fetchone()
                self.assertEqual(sale["total_amount"], 250.0)
                self.assertEqual(sale["status"], "active")
                self.assertEqual(exp["amount"], 10000.0)
                self.assertEqual(exp["status"], "active")
                self.assertEqual(user["role"], "patron")
                # audit_logs ve cash_movements oluşturulmuş olmalı
                tables = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                self.assertIn("audit_logs", tables)
                self.assertIn("cash_movements", tables)
        finally:
            Database.reset_instance(self.test_db_path)
            self.cloud_db.db = self.db
            if legacy_db_path.exists():
                legacy_db_path.unlink()


if __name__ == "__main__":
    unittest.main()
