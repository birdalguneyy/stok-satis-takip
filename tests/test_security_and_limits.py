import unittest
import time
from app.web.web_server import (
    app,
    EXPECTED_GATE_TOKEN,
    failed_pin_attempts,
    blocked_ips,
)
from app.database.cloud_db import CloudDatabase
from app.database.connection import Database
from app.database.migrations import run_migrations


class TestSecurityAndLimits(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.config import DATA_DIR
        cls.test_db_path = DATA_DIR / "test_sec_limits.db"
        if cls.test_db_path.exists():
            try:
                cls.test_db_path.unlink()
            except Exception:
                pass
        cls.db = Database.reset_instance(cls.test_db_path)
        run_migrations()
        cls.cloud_db = CloudDatabase()
        cls.cloud_db.db = cls.db
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
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM farm_egg_sales")
            conn.execute("DELETE FROM farm_feed_purchases")

    def test_robots_txt_served_and_disallows_all(self):
        res = self.client.get("/robots.txt")
        self.assertEqual(res.status_code, 200)
        body = res.get_data(as_text=True)
        self.assertIn("User-agent: *", body)
        self.assertIn("Disallow: /", body)
        self.assertIn("GPTBot", body)

    def test_bot_user_agents_blocked_with_403(self):
        bot_uas = [
            "Mozilla/5.0 (compatible; GPTBot/1.0; +https://openai.com/gptbot)",
            "ClaudeBot/1.0; +https://www.anthropic.com/claudebot",
            "Mozilla/5.0 (compatible; PerplexityBot/1.0; +https://perplexity.ai/perplexitybot)",
            "Bytespider; spider-feedback@bytedance.com",
            "Scrapy/2.11.0 (+https://scrapy.org)",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/118.0.0.0 Safari/537.36",
        ]
        for ua in bot_uas:
            res = self.client.get("/", headers={"User-Agent": ua})
            self.assertEqual(res.status_code, 403, f"Expected 403 for {ua}")
            data = res.get_json()
            self.assertFalse(data["ok"])
            self.assertIn("Güvenlik Politikası", data["message"])

    def test_normal_browser_allowed(self):
        normal_ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
        res = self.client.get("/", headers={"User-Agent": normal_ua})
        self.assertEqual(res.status_code, 200)

    def test_security_headers_present(self):
        res = self.client.get("/")
        self.assertIn("X-Robots-Tag", res.headers)
        self.assertEqual(res.headers["X-Robots-Tag"], "noindex, nofollow, noarchive, nosnippet, noimageindex")
        self.assertEqual(res.headers["X-Frame-Options"], "SAMEORIGIN")
        self.assertEqual(res.headers["X-Content-Type-Options"], "nosniff")

    def test_pin_brute_force_protection(self):
        # 4 yanlış deneme -> 401
        for i in range(4):
            res = self.client.post(
                "/api/gate/verify",
                json={"pin": "wrong"},
                environ_overrides={"REMOTE_ADDR": "192.168.1.100"},
            )
            self.assertEqual(res.status_code, 401)
            data = res.get_json()
            self.assertFalse(data["ok"])
            self.assertIn("deneme hakkı kaldı", data["message"])

        # 5. yanlış deneme -> 429 ve engellendi
        res5 = self.client.post(
            "/api/gate/verify",
            json={"pin": "wrong"},
            environ_overrides={"REMOTE_ADDR": "192.168.1.100"},
        )
        self.assertEqual(res5.status_code, 429)
        data5 = res5.get_json()
        self.assertTrue(data5.get("blocked"))

        # 6. deneme (doğru PIN bile girse engelli kalmalı)
        res6 = self.client.post(
            "/api/gate/verify",
            json={"pin": "2805"},
            environ_overrides={"REMOTE_ADDR": "192.168.1.100"},
        )
        self.assertEqual(res6.status_code, 429)

        # Farklı bir IP doğru PIN ile girebilmeli
        res_other = self.client.post(
            "/api/gate/verify",
            json={"pin": "2805"},
            environ_overrides={"REMOTE_ADDR": "192.168.1.101"},
        )
        self.assertEqual(res_other.status_code, 200)

    def test_farm_egg_sales_limit_endpoint(self):
        headers = {
            "X-Gate-Token": EXPECTED_GATE_TOKEN,
            "User-Agent": "Mozilla/5.0",
        }
        # 15 satış ekle
        for i in range(15):
            self.cloud_db.add_farm_egg_sale(
                customer_name=f"Müşteri {i}",
                box_count=1.0,
                unit_price=100.0,
                total_amount=100.0,
                sale_date=f"2026-10-10 10:{i:02d}:00",
            )

        res = self.client.get("/api/farm/egg-sales?limit=10", headers=headers)
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["sales"]), 10)

        # Limitsiz
        res_all = self.client.get("/api/farm/egg-sales", headers=headers)
        self.assertEqual(len(res_all.get_json()["sales"]), 15)

    def test_farm_feed_purchases_limit_and_summary_endpoint(self):
        headers = {
            "X-Gate-Token": EXPECTED_GATE_TOKEN,
            "User-Agent": "Mozilla/5.0",
        }
        # 12 alım ekle
        for i in range(12):
            self.cloud_db.add_farm_feed_purchase(
                bag_count=10.0,
                unit_price=450.0,
                total_amount=4500.0,
                purchase_date="2026-10-10",
                supplier=f"Tedarikçi {i}",
            )

        res = self.client.get("/api/farm/feed-purchases?limit=10", headers=headers)
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["purchases"]), 10)
        self.assertIn("summary", data)
        self.assertEqual(data["summary"]["total_bags"], 120.0)
        self.assertEqual(data["summary"]["total_tons"], 6.0)
        self.assertEqual(data["summary"]["total_cost"], 54000.0)


if __name__ == "__main__":
    unittest.main()
