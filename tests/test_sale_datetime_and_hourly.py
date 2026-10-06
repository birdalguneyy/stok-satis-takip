import unittest
from app.database.migrations import run_migrations
from app.database.connection import Database
from app.database.cloud_db import CloudDatabase
from app.repositories.sale_repository import SaleRepository
from app.services.forecast_service import ForecastService


class TestSaleDatetimeAndHourly(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        run_migrations()
        cls.db = Database()
        cls.cloud_db = CloudDatabase()
        cls.sale_repo = SaleRepository(cls.db)

    def setUp(self):
        with self.db.get_connection() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO users (id, company_name, full_name, phone, email, password_hash)
                VALUES (1, 'Test Corp', 'Test User', '05551112233', 'test@example.com', 'hash123')
                """
            )
            conn.execute("DELETE FROM sale_items")
            conn.execute("DELETE FROM sales")

            # Create a test sale
            cursor = conn.execute(
                """
                INSERT INTO sales (user_id, total_amount, item_count, sold_at, note, channel, customer_name)
                VALUES (1, 300.0, 2, '2026-10-06 14:00:00', 'Öğleden sonra satışı', 'magaza', 'Ahmet Bey')
                """
            )
            self.sale_id = cursor.lastrowid

    def test_update_sale_datetime_cloud_db(self):
        """CloudDatabase.update_sale_datetime should update sold_at in SQLite."""
        new_dt = "2026-10-05 18:30:00"
        ok, msg = self.cloud_db.update_sale_datetime(self.sale_id, new_dt, user_id=1)
        self.assertTrue(ok, msg)

        with self.db.get_connection() as conn:
            row = conn.execute("SELECT sold_at FROM sales WHERE id = ?", (self.sale_id,)).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["sold_at"], new_dt)

    def test_update_sale_datetime_iso_format(self):
        """update_sale_datetime should normalize HTML datetime-local format (with T)."""
        new_dt_iso = "2026-10-04T09:15"
        ok, msg = self.cloud_db.update_sale_datetime(self.sale_id, new_dt_iso, user_id=1)
        self.assertTrue(ok, msg)

        with self.db.get_connection() as conn:
            row = conn.execute("SELECT sold_at FROM sales WHERE id = ?", (self.sale_id,)).fetchone()
            self.assertEqual(row["sold_at"], "2026-10-04 09:15:00")

    def test_update_sale_datetime_sale_repo(self):
        """SaleRepository.update_sale_datetime should update sold_at properly."""
        new_dt = "2026-10-03 11:20:00"
        ok, msg = self.sale_repo.update_sale_datetime(self.sale_id, new_dt)
        self.assertTrue(ok, msg)

        with self.db.get_connection() as conn:
            row = conn.execute("SELECT sold_at FROM sales WHERE id = ?", (self.sale_id,)).fetchone()
            self.assertEqual(row["sold_at"], new_dt)

    def test_forecast_service_hourly_analysis(self):
        """ForecastService should generate rich 24h hourly distribution and peak window analysis."""
        with self.db.get_connection() as conn:
            for hour in [16, 16, 17, 17, 17, 18, 10, 11]:
                conn.execute(
                    """
                    INSERT INTO sales (user_id, total_amount, item_count, sold_at, channel)
                    VALUES (1, 100.0, 1, ?, 'magaza')
                    """,
                    (f"2026-10-06 {hour:02d}:15:00",)
                )

        service = ForecastService(self.cloud_db)
        forecast = service.generate_comprehensive_forecast(user_id=1)
        self.assertTrue(forecast["ok"])

        busy = forecast["busy_days"]
        self.assertIn("hourly_analysis", busy)
        hourly = busy["hourly_analysis"]

        self.assertIn("peak_window_label", hourly)
        self.assertIn("periods", hourly)
        self.assertIn("hourly_chart", hourly)
        self.assertEqual(len(hourly["hourly_chart"]), 24)

        self.assertGreater(hourly["total_transactions"], 0)
        self.assertGreater(hourly["peak_window_share"], 0)

        advice = forecast["ai_insights"]["strategic_advice"]
        self.assertIn("saat analizine göre", advice.lower())


if __name__ == "__main__":
    unittest.main()
