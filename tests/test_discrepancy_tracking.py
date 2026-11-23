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


class TestDiscrepancyTracking(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_db_path = DATA_DIR / "test_discrepancy.db"
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
                VALUES (1, 'Fark Test Market', 'Patron Selim', '05556667788', 'fark@test.com', '', 'patron')
                """
            )
            conn.execute("DELETE FROM cash_days")
            conn.execute("DELETE FROM sales")
            conn.execute("DELETE FROM expenses")
            conn.execute("DELETE FROM cash_movements")

    def _open_and_close_day(self, b_date, cashier_name, opening=250.0, counted=250.0, pos_entered=0.0, note=""):
        actor = {"name": cashier_name, "role": "calisan", "user_id": 1}
        ok_o, msg_o, _ = self.cloud_db.open_cash_day(
            opening_amount=opening,
            business_date=b_date,
            user_id=1,
            actor=actor,
        )
        self.assertTrue(ok_o, msg_o)
        ok_c, msg_c, summary = self.cloud_db.close_cash_day(
            counted_cash=counted,
            entered_pos_total=pos_entered,
            business_date=b_date,
            closing_note=note,
            user_id=1,
            actor=actor,
        )
        self.assertTrue(ok_c, msg_c)
        return summary

    def test_three_consecutive_days_repeat_alert_and_patron_only_access(self):
        # Çalışan 'Ayşe': 1. ve 2. gün fark -> henüz tekrar uyarısı yok
        s1 = self._open_and_close_day("2026-11-20", "Ayşe", opening=250.0, counted=230.0, note="1. gün bozuk eksik")
        self.assertNotIn("repeat_alert", s1)
        s2 = self._open_and_close_day("2026-11-21", "Ayşe", opening=250.0, counted=260.0, note="2. gün fazla çıktı")
        self.assertNotIn("repeat_alert", s2)

        # 3. gün üst üste fark -> TEKRAR UYARISI tetiklenmeli!
        s3 = self._open_and_close_day("2026-11-22", "Ayşe", opening=250.0, counted=235.0, note="3. gün eksik")
        self.assertIn("repeat_alert", s3)
        self.assertEqual(s3["repeat_alert"]["cashier_name"], "Ayşe")
        self.assertEqual(s3["repeat_alert"]["consecutive_days"], 3)

        # Çalışan fark geçmişini GÖREMEZ (403 + UNAUTHORIZED_ATTEMPT)
        r_staff = self.client.get("/api/reports/discrepancies", headers=self.staff_headers)
        self.assertEqual(r_staff.status_code, 403)

        # Patron fark geçmişini ve tekrar uyarısını görür
        r_patron = self.client.get("/api/reports/discrepancies", headers=self.patron_headers)
        self.assertEqual(r_patron.status_code, 200)
        rep = r_patron.get_json()["report"]
        self.assertEqual(rep["total_discrepancy_days"], 3)
        self.assertEqual(len(rep["repeat_alerts"]), 1)
        self.assertEqual(rep["repeat_alerts"][0]["cashier_name"], "Ayşe")
        self.assertEqual(rep["repeat_alerts"][0]["consecutive_days"], 3)

        # Audit log'da DISCREPANCY_ALERT ve REPEATED_DISCREPANCY_ALERT kayıtları olmalı
        r_rep_logs = self.client.get("/api/audit-logs?action=REPEATED_DISCREPANCY_ALERT", headers=self.patron_headers)
        self.assertGreaterEqual(len(r_rep_logs.get_json()["logs"]), 1)

    def test_streak_resets_on_matching_day_and_monthly_employee_report(self):
        # Çalışan 'Mehmet': Ekim'de 2 gün fark, 3. gün TUTUYOR (seri sıfırlanır), Kasım'da 1 gün fark
        self._open_and_close_day("2026-10-28", "Mehmet", opening=250.0, counted=240.0, note="Ekim 1")
        self._open_and_close_day("2026-10-29", "Mehmet", opening=250.0, counted=230.0, note="Ekim 2")
        self._open_and_close_day("2026-10-30", "Mehmet", opening=250.0, counted=250.0, note="")  # Tutuyor!
        s_nov = self._open_and_close_day("2026-11-01", "Mehmet", opening=250.0, counted=245.0, pos_entered=10.0, note="Kasım 1")
        self.assertNotIn("repeat_alert", s_nov)

        rep = self.client.get("/api/reports/discrepancies", headers=self.patron_headers).get_json()["report"]
        self.assertEqual(len(rep["repeat_alerts"]), 0)
        self.assertEqual(rep["total_discrepancy_days"], 3)

        # Çalışan bazında toplam rapor
        emp = next(e for e in rep["by_employee"] if e["cashier_name"] == "Mehmet")
        self.assertEqual(emp["discrepancy_days"], 3)
        self.assertEqual(emp["current_streak"], 1)
        self.assertEqual(emp["max_streak"], 2)
        self.assertFalse(emp["has_repeat_alert"])
        # Nakit farkları: -10 + -20 + -5 = -35; POS farkı: +10
        self.assertEqual(emp["total_cash_diff"], -35.0)
        self.assertEqual(emp["total_pos_diff"], 10.0)
        self.assertEqual(emp["abs_total_diff"], 45.0)

        # Aylık çalışan raporu (2026-10 ve 2026-11 ayrı satırlarda)
        months = {m["month_key"]: m for m in rep["monthly_by_employee"]}
        self.assertIn("2026-10", months)
        self.assertIn("2026-11", months)
        self.assertEqual(months["2026-10"]["discrepancy_days"], 2)
        self.assertEqual(months["2026-10"]["total_cash_diff"], -30.0)
        self.assertEqual(months["2026-11"]["discrepancy_days"], 1)
        self.assertEqual(months["2026-11"]["total_pos_diff"], 10.0)


if __name__ == "__main__":
    unittest.main()
