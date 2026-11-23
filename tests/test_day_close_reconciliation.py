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
            conn.execute("DELETE FROM credit_transactions")
            conn.execute("DELETE FROM sale_items")
            conn.execute("DELETE FROM sales")
            conn.execute("DELETE FROM credit_customers")
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

    def test_morning_and_evening_cash_revisions_logged_with_timestamps(self):
        """Sabah/Akşam kasa ve POS giriş + değişiklikleri saatli olarak kaydedilmeli, nakit farkı ve giderler hesaplanmalı."""
        # 1) Sabah Kasa: 250 TL ile gün açılışı
        self._open_day(250.0)
        self._add_sale(200.0, "nakit", "rev_nakit_1")
        self._add_sale(150.0, "kart", "rev_kart_1")

        # Canlı önizlemede opening_amount override çalışmalı (DB değişmeden)
        r_prev = self.client.post(
            "/api/cash-day/reconciliation",
            headers=self.patron_headers,
            json={"opening_amount": 320.0, "counted_cash": 500.0, "entered_pos_total": 150.0},
        )
        self.assertEqual(r_prev.status_code, 200)
        prev_rec = r_prev.get_json()["reconciliation"]
        self.assertAlmostEqual(prev_rec["opening_amount"], 320.0, places=2)
        self.assertAlmostEqual(prev_rec["expected_cash"], 520.0, places=2)
        self.assertAlmostEqual(prev_rec["comparison"]["cash"]["diff"], -20.0, places=2)

        # 2) İlk güncelleme: Sabah Kasa 250 -> 300, Akşam Kasa 400, POS 150, +50 TL nakit gider
        r_upd1 = self.client.post(
            "/api/cash-day/update-values",
            headers=self.patron_headers,
            json={
                "opening_amount": 300.0,
                "counted_cash": 400.0,
                "entered_pos_total": 150.0,
                "reason": "Sabah sayım düzeltmesi",
                "new_expenses": [{"title": "Poşet ve su", "amount": 50.0, "payment_method": "nakit"}],
            },
        )
        self.assertEqual(r_upd1.status_code, 200)
        body1 = r_upd1.get_json()
        self.assertTrue(body1["ok"])
        sum1 = body1["summary"]
        # Beklenen nakit = 300 + 200 - 50 = 450; girilen 400 -> -50 TL eksik
        self.assertAlmostEqual(sum1["opening_amount"], 300.0, places=2)
        self.assertAlmostEqual(sum1["expected_cash"], 450.0, places=2)
        self.assertAlmostEqual(sum1["comparison"]["cash"]["entered"], 400.0, places=2)
        self.assertAlmostEqual(sum1["comparison"]["cash"]["diff"], -50.0, places=2)
        self.assertEqual(sum1["comparison"]["cash"]["status"], "shortage")
        self.assertEqual(sum1["comparison"]["pos"]["status"], "match")

        # 3) İkinci güncelleme: Akşam Kasa 400 -> 450 (artık tutuyor), POS 150 -> 155
        r_upd2 = self.client.post(
            "/api/cash-day/update-values",
            headers=self.patron_headers,
            json={
                "counted_cash": 450.0,
                "entered_pos_total": 155.0,
                "reason": "Kasa tekrar sayıldı",
            },
        )
        self.assertEqual(r_upd2.status_code, 200)
        sum2 = r_upd2.get_json()["summary"]
        self.assertAlmostEqual(sum2["comparison"]["cash"]["diff"], 0.0, places=2)
        self.assertEqual(sum2["comparison"]["cash"]["status"], "match")
        self.assertAlmostEqual(sum2["comparison"]["pos"]["diff"], 5.0, places=2)

        # 4) Saatli değişiklik geçmişi kontrolü
        r_hist = self.client.get("/api/cash-day/history", headers=self.patron_headers)
        self.assertEqual(r_hist.status_code, 200)
        hist = r_hist.get_json()["history"]
        actions = [h["action"] for h in hist]
        self.assertIn("CASH_DAY_OPEN", actions)
        self.assertIn("MORNING_CASH_UPDATE", actions)
        self.assertIn("EVENING_CASH_SET", actions)
        self.assertIn("EVENING_POS_SET", actions)
        self.assertIn("EVENING_CASH_UPDATE", actions)
        self.assertIn("EVENING_POS_UPDATE", actions)

        morning_upd = [h for h in hist if h["action"] == "MORNING_CASH_UPDATE"][0]
        self.assertAlmostEqual(morning_upd["old_amount"], 250.0, places=2)
        self.assertAlmostEqual(morning_upd["new_amount"], 300.0, places=2)
        self.assertTrue(bool(morning_upd["time"]))

        evening_upd = [h for h in hist if h["action"] == "EVENING_CASH_UPDATE"][0]
        self.assertAlmostEqual(evening_upd["old_amount"], 400.0, places=2)
        self.assertAlmostEqual(evening_upd["new_amount"], 450.0, places=2)
        self.assertTrue(bool(evening_upd["time"]))

        # Summary içinde de value_history dönmeli
        self.assertGreaterEqual(len(sum2.get("value_history") or []), 6)

    def test_cash_day_history_role_and_closed_day_guards(self):
        """Çalışan geçmiş gün tarihçesini göremez; kapanan günde değer güncellenemez."""
        self._open_day(200.0)

        # Çalışan geçmiş gün history erişimi -> 403
        r_forb = self.client.get(
            "/api/cash-day/history?date=2025-01-01",
            headers=self.staff_headers,
        )
        self.assertEqual(r_forb.status_code, 403)

        # Günü kapat
        r_close = self.client.post(
            "/api/cash-day/close",
            headers=self.patron_headers,
            json={"counted_cash": 200.0, "entered_pos_total": 0.0},
        )
        self.assertEqual(r_close.status_code, 200)

        # Kapanan günde update-values engellenmeli
        r_locked = self.client.post(
            "/api/cash-day/update-values",
            headers=self.patron_headers,
            json={"opening_amount": 250.0, "reason": "Kapanmış gün denemesi"},
        )
        self.assertIn(r_locked.status_code, (400, 409))

    def test_full_employee_workday_scenario_end_to_end(self):
        """Uçtan uca çalışan senaryosu: Sabah kasa -> Nakit/POS/Veresiye satış -> Gün içi gider -> Sabah dahil akşam nakit & POS oto-kontrol -> Patron özeti."""
        # 1) Çalışan sabah kasayı 300 TL ile açar
        r_open = self.client.post(
            "/api/cash-day/open",
            headers=self.staff_headers,
            json={"opening_amount": 300.0, "opening_note": "Sabah açılış sayımı"},
        )
        self.assertEqual(r_open.status_code, 200)

        # 2) Çalışan gün içinde Nakit (250), POS (400) ve Veresiye (180) satış girer
        r_s1 = self.client.post(
            "/api/sales",
            headers=self.staff_headers,
            json={
                "items": [{"product_id": 701, "product_name": "Kedi Maması 15kg", "barcode": "8697001", "unit_price": 250.0, "quantity": 1, "subtotal": 250.0}],
                "total_amount": 250.0,
                "payment_method": "nakit",
                "idempotency_key": "e2e_nakit_1",
            },
        )
        self.assertEqual(r_s1.status_code, 200)

        r_s2 = self.client.post(
            "/api/sales",
            headers=self.staff_headers,
            json={
                "items": [{"product_id": 701, "product_name": "Kedi Maması 15kg", "barcode": "8697001", "unit_price": 400.0, "quantity": 1, "subtotal": 400.0}],
                "total_amount": 400.0,
                "payment_method": "kart",
                "idempotency_key": "e2e_pos_1",
            },
        )
        self.assertEqual(r_s2.status_code, 200)

        r_s3 = self.client.post(
            "/api/sales",
            headers=self.staff_headers,
            json={
                "items": [{"product_id": 701, "product_name": "Kedi Maması 15kg", "barcode": "8697001", "unit_price": 180.0, "quantity": 1, "subtotal": 180.0}],
                "total_amount": 180.0,
                "payment_method": "veresiye",
                "customer_name": "Mehmet Yılmaz",
                "idempotency_key": "e2e_veresiye_1",
            },
        )
        self.assertEqual(r_s3.status_code, 200)

        # 3) Çalışan POS'tan gün içi gider yazar (70 TL Nakit, 50 TL Kart)
        r_exp = self.client.post(
            "/api/cash-day/update-values",
            headers=self.staff_headers,
            json={
                "new_expenses": [
                    {"title": "Su ve Poşet", "amount": 70.0, "payment_method": "nakit"},
                    {"title": "Kargo", "amount": 50.0, "payment_method": "kart"},
                ],
                "reason": "Gün içi giderler",
            },
        )
        self.assertEqual(r_exp.status_code, 200)
        mid_sum = r_exp.get_json()["summary"]
        # Sabah 300 + Nakit Satış 250 - Nakit Gider 70 = 480 TL kasada olmalı (Veresiye 180 kasaya girmez)
        self.assertAlmostEqual(mid_sum["expected_cash"], 480.0, places=2)
        self.assertAlmostEqual(mid_sum["expected_pos"], 400.0, places=2)
        self.assertAlmostEqual(mid_sum["credit_sales"], 180.0, places=2)
        self.assertAlmostEqual(mid_sum["total_revenue"], 830.0, places=2)
        self.assertAlmostEqual(mid_sum["total_expenses"], 120.0, places=2)

        # 4) Akşam Gün Sonu Al oto-kontrol: Çalışan kasadaki toplam nakiti (sabah dahil) 460 TL, POS'u 400 TL yazar
        r_check = self.client.post(
            "/api/cash-day/reconciliation",
            headers=self.staff_headers,
            json={"counted_cash": 460.0, "entered_pos_total": 400.0},
        )
        self.assertEqual(r_check.status_code, 200)
        rec = r_check.get_json()["reconciliation"]
        self.assertTrue(rec["has_difference"])
        self.assertEqual(rec["comparison"]["cash"]["status"], "shortage")
        self.assertAlmostEqual(rec["comparison"]["cash"]["diff"], -20.0, places=2)
        self.assertEqual(rec["comparison"]["pos"]["status"], "match")

        # 5) Çalışan fark açıklamasıyla günü kapatır
        r_close = self.client.post(
            "/api/cash-day/close",
            headers=self.staff_headers,
            json={
                "counted_cash": 460.0,
                "entered_pos_total": 400.0,
                "closing_note": "Bozuk para sayım farkı",
            },
        )
        self.assertEqual(r_close.status_code, 200)
        self.assertTrue(r_close.get_json()["ok"])

        # 6) Patron tek bakışta tüm özeti, cari bakiyeyi ve fark raporunu görür
        r_patron_sum = self.client.get("/api/cash-day/summary", headers=self.patron_headers)
        self.assertEqual(r_patron_sum.status_code, 200)
        ps = r_patron_sum.get_json()["summary"]
        self.assertTrue(ps["is_closed"])
        self.assertAlmostEqual(ps["opening_amount"], 300.0, places=2)
        self.assertAlmostEqual(ps["expected_cash"], 480.0, places=2)
        self.assertAlmostEqual(ps["comparison"]["cash"]["entered"], 460.0, places=2)
        self.assertAlmostEqual(ps["comparison"]["cash"]["diff"], -20.0, places=2)
        self.assertAlmostEqual(ps["credit_sales"], 180.0, places=2)
        self.assertAlmostEqual(ps["total_revenue"], 830.0, places=2)

        r_cust = self.client.get("/api/credit/customers?search=Mehmet", headers=self.patron_headers)
        self.assertEqual(r_cust.status_code, 200)
        custs = r_cust.get_json()["customers"]
        self.assertEqual(len(custs), 1)
        self.assertAlmostEqual(custs[0]["balance"], 180.0, places=2)


if __name__ == "__main__":
    unittest.main()
