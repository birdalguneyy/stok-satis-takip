import unittest

from app.config import DATA_DIR
from app.database.cloud_db import CloudDatabase
from app.database.connection import Database
from app.database.migrations import run_migrations
from app.web.web_server import (
    EXPECTED_GATE_TOKEN,
    EXPECTED_STAFF_GATE_TOKEN,
    _sale_idempotency_cache,
    app,
    blocked_ips,
    failed_pin_attempts,
)


class TestCreditAccounts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_db_path = DATA_DIR / "test_credit_accounts.db"
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
                VALUES (1, 'Cari Test Market', 'Patron Hasan', '05554445566', 'cari@test.com', '', 'patron')
                """
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO products (
                    id, user_id, category_id, name, barcode, purchase_price, sale_price,
                    stock_quantity, critical_stock_level, unit, is_active, synced_to_cloud
                ) VALUES (801, 1, 1, 'Zeytinyağı 1L', '8690801', 150.0, 200.0, 100.0, 5.0, 'adet', 1, 1)
                """
            )
            conn.execute("DELETE FROM credit_transactions")
            conn.execute("DELETE FROM sale_items")
            conn.execute("DELETE FROM sales")
            conn.execute("DELETE FROM credit_customers")
            conn.execute("DELETE FROM expenses")
            conn.execute("DELETE FROM cash_movements")
            conn.execute("DELETE FROM cash_days")

        # Günü 250 TL ile aç
        r_open = self.client.post(
            "/api/cash-day/open",
            headers=self.patron_headers,
            json={"opening_amount": 250.0},
        )
        self.assertEqual(r_open.status_code, 200)

    def _create_customer(self, name, phone=""):
        r = self.client.post(
            "/api/credit/customers",
            headers=self.staff_headers,
            json={"name": name, "phone": phone},
        )
        self.assertEqual(r.status_code, 200)
        return r.get_json()["customer"]["id"]

    # SENARYO 1A: 200 TL SATIŞ / 300 TL ÖDEME -> 100 TL CARİ ALACAK
    def test_overpayment_200_300_recorded_as_customer_credit(self):
        cid = self._create_customer("Mehmet Yılmaz", "05321112233")
        r = self.client.post(
            "/api/sales",
            headers=self.staff_headers,
            json={
                "items": [{
                    "product_id": 801,
                    "product_name": "Zeytinyağı 1L",
                    "barcode": "8690801",
                    "unit_price": 200.0,
                    "quantity": 1,
                    "subtotal": 200.0,
                    "unit": "adet",
                }],
                "total_amount": 200.0,
                "payment_method": "karma",
                "customer_id": cid,
                "paid_cash_amount": 300.0,
                "paid_pos_amount": 0.0,
                "overpayment_mode": "credit",
                "idempotency_key": "ov_credit_1",
            },
        )
        self.assertEqual(r.status_code, 200)

        # Müşteri cari bakiyesi -100.00 TL (-10000 kuruş = 100 TL müşteri alacağı) olmalı
        detail = self.client.get(f"/api/credit/customers/{cid}", headers=self.staff_headers).get_json()
        self.assertEqual(detail["customer"]["balance_kurus"], -10000)
        self.assertEqual(detail["customer"]["balance"], -100.0)
        self.assertEqual(detail["customer"]["balance_status"], "alacakli")

        # Kasada fiilen 300 TL nakit alındığı için beklenen nakit: 250 + 300 = 550 TL
        sm = self.client.get("/api/cash-day/summary", headers=self.patron_headers).get_json()["summary"]
        self.assertEqual(sm["expected_cash"], 550.0)

    # SENARYO 1B: 200 TL SATIŞ / 300 TL ÖDEME -> 100 TL PARA ÜSTÜ KASADAN ÇIKIŞ
    def test_overpayment_200_300_recorded_as_cash_change_out(self):
        cid = self._create_customer("Fatma Kaya")
        r = self.client.post(
            "/api/sales",
            headers=self.staff_headers,
            json={
                "items": [{
                    "product_id": 801,
                    "product_name": "Zeytinyağı 1L",
                    "barcode": "8690801",
                    "unit_price": 200.0,
                    "quantity": 1,
                    "subtotal": 200.0,
                    "unit": "adet",
                }],
                "total_amount": 200.0,
                "payment_method": "karma",
                "customer_id": cid,
                "paid_cash_amount": 300.0,
                "paid_pos_amount": 0.0,
                "overpayment_mode": "change",
                "idempotency_key": "ov_change_1",
            },
        )
        self.assertEqual(r.status_code, 200)

        # Cari bakiye 0 TL olmalı (100 TL para üstü olarak iade edildi)
        detail = self.client.get(f"/api/credit/customers/{cid}", headers=self.staff_headers).get_json()
        self.assertEqual(detail["customer"]["balance_kurus"], 0)
        self.assertEqual(detail["customer"]["balance"], 0.0)

        # Kasa: Açılış 250 + 300 nakit alındı - 100 para üstü çıkışı = 450 TL
        sm = self.client.get("/api/cash-day/summary", headers=self.patron_headers).get_json()["summary"]
        self.assertEqual(sm["cash_sales"], 300.0)
        self.assertEqual(sm["other_cash_out"], 100.0)
        self.assertEqual(sm["expected_cash"], 450.0)

    # SENARYO 2 & 3: KISMİ (KARMA) ÖDEME + SADECE TAHSİLAT (NAKİT & POS) + GÜN SONU MUTABAKAT
    def test_partial_karma_payment_and_collections_in_reconciliation(self):
        cid = self._create_customer("Ahmet Demir")

        # 500 TL'lik satış: 150 TL Nakit + 100 TL POS + 250 TL Veresiye
        r_sale = self.client.post(
            "/api/sales",
            headers=self.staff_headers,
            json={
                "items": [{
                    "product_id": 801,
                    "product_name": "Zeytinyağı 1L",
                    "barcode": "8690801",
                    "unit_price": 250.0,
                    "quantity": 2,
                    "subtotal": 500.0,
                    "unit": "adet",
                }],
                "total_amount": 500.0,
                "payment_method": "karma",
                "customer_id": cid,
                "paid_cash_amount": 150.0,
                "paid_pos_amount": 100.0,
                "idempotency_key": "karma_1",
            },
        )
        self.assertEqual(r_sale.status_code, 200)

        # Cari bakiye = +250.00 TL borç (25000 kuruş)
        d1 = self.client.get(f"/api/credit/customers/{cid}", headers=self.staff_headers).get_json()
        self.assertEqual(d1["customer"]["balance_kurus"], 25000)

        # Sadece tahsilat: 100 TL Nakit + 50 TL POS
        r_col_cash = self.client.post(
            "/api/credit/collections",
            headers=self.staff_headers,
            json={"customer_id": cid, "amount": 100.0, "payment_method": "nakit", "note": "Nakit borç ödemesi"},
        )
        self.assertEqual(r_col_cash.status_code, 200)
        col_cash_id = r_col_cash.get_json()["collection"]["id"]

        r_col_pos = self.client.post(
            "/api/credit/collections",
            headers=self.staff_headers,
            json={"customer_id": cid, "amount": 50.0, "payment_method": "kart", "note": "POS borç ödemesi"},
        )
        self.assertEqual(r_col_pos.status_code, 200)

        # Güncel bakiye: 250 - 100 - 50 = 100.00 TL (10000 kuruş)
        d2 = self.client.get(f"/api/credit/customers/{cid}", headers=self.staff_headers).get_json()
        self.assertEqual(d2["customer"]["balance_kurus"], 10000)
        self.assertEqual(d2["customer"]["balance"], 100.0)

        # Gün sonu mutabakat formülü:
        # Beklenen Nakit = 250 (açılış) + 150 (nakit satış) + 100 (nakit tahsilat) = 500 TL
        # Beklenen POS   = 100 (POS satış) + 50 (POS tahsilat) = 150 TL
        rec = self.client.post(
            "/api/cash-day/reconciliation",
            headers=self.patron_headers,
            json={"counted_cash": 500.0, "entered_pos_total": 150.0},
        ).get_json()["reconciliation"]
        self.assertEqual(rec["credit_collections_cash"], 100.0)
        self.assertEqual(rec["credit_collections_pos"], 50.0)
        self.assertEqual(rec["expected_cash"], 500.0)
        self.assertEqual(rec["expected_pos"], 150.0)
        self.assertFalse(rec["has_difference"])
        self.assertEqual(rec["comparison"]["cash"]["status"], "match")
        self.assertEqual(rec["comparison"]["pos"]["status"], "match")

        # Çalışan tahsilat iptal edemez (403), Patron nedenle iptal edebilir (200)
        self.assertEqual(
            self.client.post(
                f"/api/credit/collections/{col_cash_id}/cancel",
                headers=self.staff_headers,
                json={"cancel_reason": "Yanlış tutar / miktar girişi"},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                f"/api/credit/collections/{col_cash_id}/cancel",
                headers=self.patron_headers,
                json={"cancel_reason": "Yanlış tutar / miktar girişi"},
            ).status_code,
            200,
        )
        # 100 TL nakit tahsilat iptal edilince bakiye tekrar 200 TL, beklenen nakit 400 TL olmalı
        d3 = self.client.get(f"/api/credit/customers/{cid}", headers=self.patron_headers).get_json()
        self.assertEqual(d3["customer"]["balance"], 200.0)
        sm3 = self.client.get("/api/cash-day/summary", headers=self.patron_headers).get_json()["summary"]
        self.assertEqual(sm3["expected_cash"], 400.0)

    # SENARYO 4: İPTAL EDİLEN VERESİYE SATIŞIN BAKİYEYİ GERİ ALMASI + BORÇ SIRALAMASI
    def test_cancelled_credit_sale_reverses_balance_and_debt_sorting(self):
        cid_a = self._create_customer("Ali Veli")
        cid_b = self._create_customer("Zeynep Arslan")

        # Ali'ye 400 TL veresiye, Zeynep'e 900 TL veresiye
        self.assertEqual(
            self.client.post(
                "/api/sales",
                headers=self.staff_headers,
                json={
                    "items": [{"product_id": 801, "product_name": "Zeytinyağı 1L", "barcode": "8690801", "unit_price": 200.0, "quantity": 2, "subtotal": 400.0}],
                    "total_amount": 400.0,
                    "payment_method": "veresiye",
                    "customer_id": cid_a,
                    "idempotency_key": "ver_a",
                },
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.post(
                "/api/sales",
                headers=self.staff_headers,
                json={
                    "items": [{"product_id": 801, "product_name": "Zeytinyağı 1L", "barcode": "8690801", "unit_price": 300.0, "quantity": 3, "subtotal": 900.0}],
                    "total_amount": 900.0,
                    "payment_method": "veresiye",
                    "customer_id": cid_b,
                    "idempotency_key": "ver_b",
                },
            ).status_code,
            200,
        )

        # Toplam borç listesi en çok borçlu başta olmalı: Zeynep (900) -> Ali (400)
        list1 = self.client.get("/api/credit/customers", headers=self.staff_headers).get_json()
        self.assertEqual(list1["customers"][0]["id"], cid_b)
        self.assertEqual(list1["customers"][0]["balance"], 900.0)
        self.assertEqual(list1["customers"][1]["id"], cid_a)
        self.assertEqual(list1["customers"][1]["balance"], 400.0)
        self.assertEqual(list1["summary"]["total_receivable"], 1300.0)

        # Zeynep'in 900 TL'lik veresiye satışını bul ve iptal et
        with self.db.get_connection() as conn:
            sale_b_id = conn.execute("SELECT id FROM sales WHERE customer_id = ? ORDER BY id DESC LIMIT 1", (cid_b,)).fetchone()["id"]

        r_canc = self.client.post(
            f"/api/sales/{sale_b_id}/cancel",
            headers=self.patron_headers,
            json={"cancel_reason": "Müşteri iadesi", "cancel_note": "Ürünler geri geldi"},
        )
        self.assertEqual(r_canc.status_code, 200)

        # Zeynep'in bakiyesi 0.00 TL'ye dönmeli, Ali (400 TL) ilk sıraya geçmeli
        list2 = self.client.get("/api/credit/customers", headers=self.patron_headers).get_json()
        self.assertEqual(list2["customers"][0]["id"], cid_a)
        self.assertEqual(list2["customers"][0]["balance"], 400.0)
        self.assertEqual(list2["customers"][1]["id"], cid_b)
        self.assertEqual(list2["customers"][1]["balance"], 0.0)
        self.assertEqual(list2["summary"]["total_receivable"], 400.0)


if __name__ == "__main__":
    unittest.main()
