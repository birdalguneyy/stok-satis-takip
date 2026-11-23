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


class TestDayCloseReconciliation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_db_path = DATA_DIR / "test_day_close.db"
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
                VALUES (1, 'Mutabakat Market', 'Patron Kemal', '05553334455', 'mutabakat@test.com', '', 'patron')
                """
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO products (
                    id, user_id, category_id, name, barcode, purchase_price, sale_price,
                    stock_quantity, critical_stock_level, unit, is_active, synced_to_cloud
                ) VALUES (701, 1, 1, 'Kahve', '8690701', 50.0, 100.0, 100.0, 5.0, 'adet', 1, 1)
                """
            )
            conn.execute("DELETE FROM sale_items")
            conn.execute("DELETE FROM sales")
            conn.execute("DELETE FROM expenses")
            conn.execute("DELETE FROM cash_movements")
            conn.execute("DELETE FROM cash_days")

    def _open_day(self, opening_amount=250.0):
        res = self.client.post(
            "/api/cash-day/open",
            headers=self.patron_headers,
            json={"opening_amount": opening_amount},
        )
        self.assertEqual(res.status_code, 200)

    def _add_sale(self, total, payment_method="nakit", key="s1"):
        res = self.client.post(
            "/api/sales",
            headers=self.staff_headers,
            json={
                "items": [{
                    "product_id": 701,
                    "product_name": "Kahve",
                    "barcode": "8690701",
                    "unit_price": total,
                    "quantity": 1,
                    "subtotal": total,
                    "unit": "adet",
                }],
                "total_amount": total,
                "payment_method": payment_method,
                "idempotency_key": key,
            },
        )
        self.assertEqual(res.status_code, 200)

    # SENARYO 1: FARK YOK (TUTUYOR) + KALEM KALEM GİDERLER (NAKİT/KART)
    def test_reconciliation_no_difference_match(self):
        self._open_day(250.0)
        self._add_sale(200.0, "nakit", "n1")
        self._add_sale(300.0, "kart", "k1")

        # Patron çekimi: -150 TL
        self.assertEqual(
            self.client.post(
                "/api/cash-movements",
                headers=self.patron_headers,
                json={"movement_type": "kasa_cikis", "category": "patron_cekis", "amount": 150.0, "description": "Patron elden nakit çekti"},
            ).status_code,
            200,
        )
        # Diğer kasa girişi: +50 TL
        self.assertEqual(
            self.client.post(
                "/api/cash-movements",
                headers=self.staff_headers,
                json={"movement_type": "kasa_giris", "category": "para_ustu", "amount": 50.0, "description": "Bozuk para eklendi"},
            ).status_code,
            200,
        )

        # Gün sonu giderleri: 80 TL nakit + 40 TL kart (toplam gider 120 TL)
        # Beklenen nakit = 250 + 200 - 80 - 150 + 50 = 270 TL
        # Beklenen POS   = 300 TL
        new_expenses = [
            {"title": "Su damacana", "amount": 80.0, "payment_method": "nakit"},
            {"title": "İnternet faturası", "amount": 40.0, "payment_method": "kart"},
        ]
        res = self.client.post(
            "/api/cash-day/close",
            headers=self.staff_headers,
            json={
                "counted_cash": 270.0,
                "entered_pos_total": 300.0,
                "new_expenses": new_expenses,
                "closing_note": "",  # Fark yok -> not zorunlu değil
            },
        )
        self.assertEqual(res.status_code, 200)
        rec = res.get_json()["reconciliation"]
        self.assertFalse(rec["has_difference"])
        self.assertEqual(rec["expected_cash"], 270.0)
        self.assertEqual(rec["expected_pos"], 300.0)
        self.assertEqual(rec["total_expenses"], 120.0)
        self.assertEqual(rec["comparison"]["cash"]["status"], "match")
        self.assertEqual(rec["comparison"]["pos"]["status"], "match")
        self.assertEqual(rec["comparison"]["expenses"]["status"], "match")
        self.assertTrue(rec["is_closed"])

    # SENARYO 2: NAKİT FAZLA (+15 TL)
    def test_reconciliation_cash_surplus_requires_note(self):
        self._open_day(250.0)

        # Notsuz kapanış engellenmeli (400)
        r_no_note = self.client.post(
            "/api/cash-day/close",
            headers=self.patron_headers,
            json={"counted_cash": 265.0, "entered_pos_total": 0.0, "closing_note": ""},
        )
        self.assertEqual(r_no_note.status_code, 400)

        # Not ile kapanış başarılı (200)
        r_ok = self.client.post(
            "/api/cash-day/close",
            headers=self.patron_headers,
            json={"counted_cash": 265.0, "entered_pos_total": 0.0, "closing_note": "Müşteri para üstü bıraktı"},
        )
        self.assertEqual(r_ok.status_code, 200)
        cmp_cash = r_ok.get_json()["reconciliation"]["comparison"]["cash"]
        self.assertEqual(cmp_cash["diff"], 15.0)
        self.assertEqual(cmp_cash["status"], "surplus")

    # SENARYO 3: NAKİT EKSİK (-80 TL: Açılış 250 -> Kapanış 170)
    def test_reconciliation_cash_shortage(self):
        self._open_day(250.0)

        r_prev = self.client.post(
            "/api/cash-day/reconciliation",
            headers=self.patron_headers,
            json={"counted_cash": 170.0, "entered_pos_total": 0.0},
        )
        self.assertEqual(r_prev.status_code, 200)
        rec = r_prev.get_json()["reconciliation"]
        self.assertTrue(rec["has_difference"])
        self.assertEqual(rec["comparison"]["cash"]["diff"], -80.0)
        self.assertEqual(rec["comparison"]["cash"]["status"], "shortage")

        r_close = self.client.post(
            "/api/cash-day/close",
            headers=self.patron_headers,
            json={"counted_cash": 170.0, "entered_pos_total": 0.0, "closing_note": "Kurye ödemesi fişi eksik"},
        )
        self.assertEqual(r_close.status_code, 200)
        self.assertEqual(r_close.get_json()["reconciliation"]["day"]["closing_amount"], 170.0)

    # SENARYO 4: POS FARKI + KAPANAN GÜN KİLİDİ + SADECE PATRON YENİDEN AÇABİLİR
    def test_reconciliation_pos_diff_day_lock_and_patron_reopen(self):
        self._open_day(250.0)
        self._add_sale(300.0, "kart", "pos1")
        with self.db.get_connection() as conn:
            sale_id = conn.execute("SELECT id FROM sales ORDER BY id DESC LIMIT 1").fetchone()["id"]

        # Nakit tutuyor (250=250) ama POS eksik (285 vs 300 -> -15 TL) -> notsuz kapanamaz
        r_no_note = self.client.post(
            "/api/cash-day/close",
            headers=self.patron_headers,
            json={"counted_cash": 250.0, "entered_pos_total": 285.0, "closing_note": ""},
        )
        self.assertEqual(r_no_note.status_code, 400)

        # Not ile kapat
        r_close = self.client.post(
            "/api/cash-day/close",
            headers=self.patron_headers,
            json={"counted_cash": 250.0, "entered_pos_total": 285.0, "closing_note": "POS slip iptali ertesi güne yansıdı"},
        )
        self.assertEqual(r_close.status_code, 200)
        cmp_pos = r_close.get_json()["reconciliation"]["comparison"]["pos"]
        self.assertEqual(cmp_pos["diff"], -15.0)
        self.assertEqual(cmp_pos["status"], "shortage")

        # Kapanan günde hiçbir işlem/düzenleme yapılamamalı (409 day_closed)
        r_sale_locked = self.client.post(
            "/api/sales",
            headers=self.patron_headers,
            json={
                "items": [{"product_id": 701, "product_name": "Kahve", "barcode": "8690701", "unit_price": 100.0, "quantity": 1, "subtotal": 100.0}],
                "total_amount": 100.0,
                "idempotency_key": "after_close",
            },
        )
        self.assertEqual(r_sale_locked.status_code, 409)
        self.assertTrue(r_sale_locked.get_json().get("day_closed"))

        r_cancel_locked = self.client.post(
            f"/api/sales/{sale_id}/cancel",
            headers=self.patron_headers,
            json={"cancel_reason": "Müşteri iadesi"},
        )
        self.assertEqual(r_cancel_locked.status_code, 409)
        self.assertTrue(r_cancel_locked.get_json().get("day_closed"))

        r_exp_locked = self.client.post(
            "/api/expenses",
            headers=self.patron_headers,
            json={"title": "Geç gider", "amount": 50.0},
        )
        self.assertEqual(r_exp_locked.status_code, 409)

        r_mov_locked = self.client.post(
            "/api/cash-movements",
            headers=self.patron_headers,
            json={"movement_type": "kasa_giris", "amount": 50.0, "description": "Geç ekleme"},
        )
        self.assertEqual(r_mov_locked.status_code, 409)

        # Çalışan günü yeniden açamaz -> 403
        today_str = get_turkey_now_str("%Y-%m-%d")
        r_staff_reopen = self.client.post(
            "/api/day/reopen",
            headers=self.staff_headers,
            json={"date": today_str, "reason": "Düzeltme"},
        )
        self.assertEqual(r_staff_reopen.status_code, 403)

        # Patron günü yeniden açar -> 200 ve DAY_REOPEN audit kaydı düşer
        r_patron_reopen = self.client.post(
            "/api/day/reopen",
            headers=self.patron_headers,
            json={"date": today_str, "reason": "Eksik POS fişi düzeltilecek"},
        )
        self.assertEqual(r_patron_reopen.status_code, 200)

        r_logs = self.client.get("/api/audit-logs?action=DAY_REOPEN", headers=self.patron_headers)
        self.assertGreaterEqual(len(r_logs.get_json()["logs"]), 1)

        # Yeniden açıldıktan sonra satış tekrar girilebilir
        self._add_sale(100.0, "nakit", "after_reopen")


if __name__ == "__main__":
    unittest.main()
