import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.services import push_service


class TestPushAndIdleFeatures(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp_dir.name)
        self.orig_vapid_file = push_service._VAPID_FILE
        self.orig_subs_file = push_service._SUBS_FILE
        push_service._VAPID_FILE = self.tmp_path / "vapid_keys.json"
        push_service._SUBS_FILE = self.tmp_path / "push_subscriptions.json"

    def tearDown(self):
        push_service._VAPID_FILE = self.orig_vapid_file
        push_service._SUBS_FILE = self.orig_subs_file
        self.tmp_dir.cleanup()

    def test_vapid_key_generation_and_persistence(self):
        with patch.object(push_service, "_get_cloud_db", return_value=None):
            keys1 = push_service.get_vapid_keys()
            self.assertIn("public_key", keys1)
            self.assertIn("private_key", keys1)
            self.assertTrue(len(keys1["public_key"]) >= 80)
            self.assertIn("BEGIN PRIVATE KEY", keys1["private_key"])

            keys2 = push_service.get_vapid_keys()
            self.assertEqual(keys1, keys2)

    def test_subscription_crud_and_send(self):
        sub = {
            "endpoint": "https://fcm.googleapis.com/fcm/send/test-endpoint-123",
            "keys": {"p256dh": "test-p256dh-key", "auth": "test-auth-key"},
        }
        with patch.object(push_service, "_get_cloud_db", return_value=None):
            ok, _ = push_service.save_subscription(sub, user_id=1, device_label="TestPhone")
            self.assertTrue(ok)
            subs = push_service.list_subscriptions(user_id=1)
            self.assertEqual(len(subs), 1)
            self.assertEqual(subs[0]["endpoint"], sub["endpoint"])

            with patch("pywebpush.webpush") as mock_wp:
                res = push_service.send_push_to_all("Test", "Body", user_id=1)
                self.assertEqual(res["sent"], 1)
                self.assertEqual(res["failed"], 0)
                mock_wp.assert_called_once()

            removed = push_service.remove_subscription(sub["endpoint"])
            self.assertTrue(removed)
            self.assertEqual(push_service.list_subscriptions(user_id=1), [])

    def test_format_sale_notification(self):
        title, body = push_service.format_sale_notification(
            items=[{"product_name": "Gallus Kedi Maması", "quantity": 2, "unit_price": 150.0}],
            total_amount=300.0,
            channel="magaza",
            customer_name="Ahmet",
        )
        self.assertIn("300.00 ₺", title)
        self.assertIn("Gallus Kedi Maması x2", body)
        self.assertIn("Ahmet", body)
        self.assertIn("Mağaza POS", body)

    def test_web_routes_and_frontend_assets(self):
        from app.web.web_server import EXPECTED_GATE_TOKEN, app

        client = app.test_client()
        headers = {"X-Gate-Token": EXPECTED_GATE_TOKEN}

        with patch.object(push_service, "_get_cloud_db", return_value=None):
            r_key = client.get("/api/push/vapid-public-key", headers=headers)
            self.assertEqual(r_key.status_code, 200)
            self.assertTrue(r_key.get_json()["ok"])

        html = Path("app/web/static/index.html").read_text(encoding="utf-8")
        self.assertIn("https://gallusmama.com", html)
        self.assertIn("5 * 60 * 1000", html)
        self.assertIn("handlePushNotifBadgeClick", html)

        sw = Path("app/web/static/sw.js").read_text(encoding="utf-8")
        self.assertIn("addEventListener('push'", sw)
        self.assertIn("addEventListener('notificationclick'", sw)


if __name__ == "__main__":
    unittest.main()
