import unittest

from app.config import DATA_DIR, get_turkey_now_str
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


class TestCashManagement(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_db_path = DATA_DIR / "test_cash_mgmt.db"
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
                VALUES (1, 'Kasa Test Market', 'Patron Ali', '05552223344', 'kasa@test.com', '', 'patron')
                """
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO products (
                    id, user_id, category_id, name, barcode, purchase_price, sale_price,
                    stock_quantity, critical_stock_level, unit, is_active, synced_to_cloud
                ) VALUES (601, 1, 1, 'Ekmek', '8690601', 8.0, 15.0, 100.0, 5.0, 'adet', 1, 1)
                """
            )
            conn.execute("DELETE FROM sale_items")
            conn.execute("DELETE FROM sales")
            conn.execute("DELETE FROM cash_movements")
            conn.execute("DELETE FROM cash_days")

    def _sale_payload(self, total=60.0, payment_method="nakit", key="k1"):
        return {
            "items": [{
                "product_id": 601,
                "product_name": "Ekmek",
                "barcode": "8690601",
                "unit_price": total,
                "quantity": 1,
                "subtotal": total,
                "unit": "adet",
            }],
            "total_amount": total,
            "payment_method": payment_method,
            "idempotency_key": key,
        }

    def test_sale_blocked_until_cash_day_opened_and_audit_logged(self):
        # 1. Gün açılmadan satış girişi engellenmeli (409 day_not_open)
        r_blocked = self.client.post("/api/sales", headers=self.staff_headers, json=self._sale_payload(60.0, "nakit", "b1"))
        self.assertEqual(r_blocked.status_code, 409)
        self.assertTrue(r_blocked.get_json().get("day_not_open"))

        # 2. Negatif açılış kasası reddedilmeli
        r_neg = self.client.post("/api/cash-day/open", headers=self.staff_headers, json={"opening_amount": -50})
        self.assertEqual(r_neg.status_code, 400)

        # 3. Gün açılışı (250 TL) Çalışan tarafından açılabilir; kim + saat + audit kaydedilmeli
        r_open = self.client.post(
            "/api/cash-day/open",
            headers=self.staff_headers,
            json={"opening_amount": 250.0, "opening_note": "Sabah bozuk para kasası"},
        )
        self.assertEqual(r_open.status_code, 200)
        day = r_open.get_json()["day"]
        self.assertEqual(day["opening_amount"], 250.0)
        self.assertEqual(day["opened_by"], "Çalışan")
        self.assertEqual(day["opened_by_role"], "calisan")
        self.assertTrue(day["opened_at"])

        # Aynı gün ikinci kez açılamamalı
        r_dup = self.client.post("/api/cash-day/open", headers=self.patron_headers, json={"opening_amount": 300.0})
        self.assertEqual(r_dup.status_code, 400)

        # Audit log'da CASH_DAY_OPEN kaydı olmalı
        r_logs = self.client.get("/api/audit-logs?action=CASH_DAY_OPEN", headers=self.patron_headers)
        self.assertGreaterEqual(len(r_logs.get_json()["logs"]), 1)

        # 4. Gün açıldıktan sonra satış artık başarılı olmalı
        r_ok = self.client.post("/api/sales", headers=self.staff_headers, json=self._sale_payload(60.0, "nakit", "ok1"))
        self.assertEqual(r_ok.status_code, 200)
        self.assertTrue(r_ok.get_json()["ok"])

    def test_cash_movements_validations_roles_formula_and_soft_cancel(self):
        # Günü 250 TL ile aç
        self.assertEqual(
            self.client.post("/api/cash-day/open", headers=self.patron_headers, json={"opening_amount": 250.0}).status_code,
            200,
        )

        # Nakit satış (120 TL) + Kart satış (80 TL - beklenen nakite DAHİL EDİLMEMELİ)
        self.assertEqual(
            self.client.post("/api/sales", headers=self.staff_headers, json=self._sale_payload(120.0, "nakit", "s_nakit")).status_code,
            200,
        )
        self.assertEqual(
            self.client.post("/api/sales", headers=self.staff_headers, json=self._sale_payload(80.0, "kart", "s_kart")).status_code,
            200,
        )

        # Çalışan kasaya para üstü (nakit giriş: 50 TL) ekleyebilir, ama boş açıklama reddedilir
        r_empty_in = self.client.post(
            "/api/cash-movements",
            headers=self.staff_headers,
            json={"movement_type": "kasa_giris", "category": "para_ustu", "amount": 50.0, "description": ""},
        )
        self.assertEqual(r_empty_in.status_code, 400)

        r_in_ok = self.client.post(
            "/api/cash-movements",
            headers=self.staff_headers,
            json={"movement_type": "kasa_giris", "category": "para_ustu", "amount": 50.0, "description": "Bozuk para eklendi"},
        )
        self.assertEqual(r_in_ok.status_code, 200)

        # Çalışan "Patron nakit çekti" yapamaz -> 403 + UNAUTHORIZED_ATTEMPT
        r_staff_withdraw = self.client.post(
            "/api/cash-movements",
            headers=self.staff_headers,
            json={"movement_type": "kasa_cikis", "category": "patron_cekis", "amount": 100.0, "description": "Patron aldı"},
        )
        self.assertEqual(r_staff_withdraw.status_code, 403)

        # Patron nakit çekiminde boş açıklama -> 400
        r_empty_out = self.client.post(
            "/api/cash-movements",
            headers=self.patron_headers,
            json={"movement_type": "kasa_cikis", "category": "patron_cekis", "amount": 100.0, "description": "   "},
        )
        self.assertEqual(r_empty_out.status_code, 400)

        # Patron nakit çekiminde çok kısa açıklama (< 5 karakter) -> 409 short_description uyarısı
        r_short_out = self.client.post(
            "/api/cash-movements",
            headers=self.patron_headers,
            json={"movement_type": "kasa_cikis", "category": "patron_cekis", "amount": 100.0, "description": "aldı"},
        )
        self.assertEqual(r_short_out.status_code, 409)
        self.assertEqual(r_short_out.get_json().get("warning_type"), "short_description")

        # Açıklayıcı metin ile Patron nakit çekimi (100 TL) -> 200
        r_out_ok = self.client.post(
            "/api/cash-movements",
            headers=self.patron_headers,
            json={"movement_type": "kasa_cikis", "category": "patron_cekis", "amount": 100.0, "description": "Patron tedarikçi ödemesi için elden çekti"},
        )
        self.assertEqual(r_out_ok.status_code, 200)
        withdraw_id = r_out_ok.get_json()["movement"]["id"]

        # BEKLENEN NAKİT FORMÜLÜ: 250 (açılış) + 120 (nakit satış) - 100 (çıkış) + 50 (giriş) = 320 TL
        r_sum = self.client.get("/api/cash-day/summary", headers=self.patron_headers)
        self.assertEqual(r_sum.status_code, 200)
        sm = r_sum.get_json()["summary"]
        self.assertEqual(sm["opening_amount"], 250.0)
        self.assertEqual(sm["cash_sales"], 120.0)
        self.assertEqual(sm["card_sales"], 80.0)
        self.assertEqual(sm["cash_in"], 50.0)
        self.assertEqual(sm["cash_out"], 100.0)
        self.assertEqual(sm["expected_cash"], 320.0)

        # Çalışan bugünün kasa özetini görebilir ama geçmiş günü GÖREMEZ (403)
        self.assertEqual(self.client.get("/api/cash-day/summary", headers=self.staff_headers).status_code, 200)
        self.assertEqual(self.client.get("/api/cash-day/summary?date=2025-01-01", headers=self.staff_headers).status_code, 403)

        # Hareket silinemez, sadece nedenle iptal edilebilir (ve Çalışan iptal edemez)
        self.assertEqual(
            self.client.post(
                f"/api/cash-movements/{withdraw_id}/cancel",
                headers=self.staff_headers,
                json={"cancel_reason": "Yanlış tutar / miktar girişi"},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                f"/api/cash-movements/{withdraw_id}/cancel",
                headers=self.patron_headers,
                json={"cancel_reason": ""},
            ).status_code,
            400,
        )
        r_canc = self.client.post(
            f"/api/cash-movements/{withdraw_id}/cancel",
            headers=self.patron_headers,
            json={"cancel_reason": "Yanlış tutar / miktar girişi", "cancel_note": "Çekim iptal edildi"},
        )
        self.assertEqual(r_canc.status_code, 200)

        # İptal sonrası beklenen nakit: 250 + 120 - 0 + 50 = 420 TL (kayıt silinmedi, status='cancelled')
        sm_after = self.client.get("/api/cash-day/summary", headers=self.patron_headers).get_json()["summary"]
        self.assertEqual(sm_after["cash_out"], 0.0)
        self.assertEqual(sm_after["expected_cash"], 420.0)
        cancelled_rows = [m for m in sm_after["movements"] if m["id"] == withdraw_id]
        self.assertEqual(len(cancelled_rows), 1)
        self.assertEqual(cancelled_rows[0]["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
