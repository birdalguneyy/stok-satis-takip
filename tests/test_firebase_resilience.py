import os
import unittest
from unittest.mock import MagicMock, patch

from app.database.cloud_db import CloudDatabase
from app.web.web_server import app, cloud_db


class _FakeDocRef:
    def __init__(self, fail_times=0, error_exc=None, calls_log=None):
        self.fail_times = fail_times
        self.error_exc = error_exc or RuntimeError("503 ServiceUnavailable: GOAWAY received")
        self.calls = 0
        self.calls_log = calls_log if calls_log is not None else []

    def set(self, data, **kwargs):
        self.calls += 1
        self.calls_log.append(data)
        if self.calls <= self.fail_times:
            raise self.error_exc
        return True


class _FakeFirestore:
    def __init__(self, doc_ref):
        self._doc_ref = doc_ref
        self.closed = False

    def collection(self, name):
        col = MagicMock()
        col.document.return_value = self._doc_ref
        return col

    def close(self):
        self.closed = True


class TestFirebaseResilience(unittest.TestCase):
    def setUp(self):
        self.orig_firestore = cloud_db.firestore_db
        self.orig_last_err = cloud_db.firebase_last_error
        self.orig_consec = cloud_db._firebase_consecutive_errors
        self.orig_quota_until = cloud_db._firebase_quota_blocked_until
        cloud_db._firebase_consecutive_errors = 0
        cloud_db._firebase_quota_blocked_until = 0.0
        cloud_db.firebase_last_error = None

    def tearDown(self):
        cloud_db.firestore_db = self.orig_firestore
        cloud_db.firebase_last_error = self.orig_last_err
        cloud_db._firebase_consecutive_errors = self.orig_consec
        cloud_db._firebase_quota_blocked_until = self.orig_quota_until

    def test_grpc_keepalive_env_defaults_configured(self):
        """gRPC keepalive ayarları bağlantı kopmalarını önlemek için set edilmiş olmalı."""
        self.assertEqual(os.environ.get("GRPC_KEEPALIVE_TIME_MS"), "30000")
        self.assertEqual(os.environ.get("GRPC_KEEPALIVE_TIMEOUT_MS"), "10000")
        self.assertEqual(os.environ.get("GRPC_KEEPALIVE_PERMIT_WITHOUT_CALLS"), "1")

    def test_transient_error_triggers_reconnect_and_succeeds(self):
        """Geçici 503/GOAWAY hatasında istemci otomatik sıfırlanıp tekrar deneyerek başarılı olmalı."""
        fake_doc = _FakeDocRef(fail_times=2)
        cloud_db.firestore_db = _FakeFirestore(fake_doc)

        reset_calls = []

        def fake_reset(reason=""):
            reset_calls.append(reason)
            return True

        with patch.object(cloud_db, "_reset_firebase_client", side_effect=fake_reset), \
             patch("app.database.cloud_db.time.sleep", return_value=None):
            result = cloud_db._firestore_call_with_retry(
                lambda db: db.collection("sales").document("u1_s1").set({"ok": True}),
                op_name="test_transient",
                max_retries=3,
            )

        self.assertTrue(result)
        self.assertEqual(fake_doc.calls, 3)
        self.assertEqual(len(reset_calls), 2)
        self.assertEqual(cloud_db._firebase_consecutive_errors, 0)
        self.assertIsNone(cloud_db.firebase_last_error)

    def test_quota_error_does_not_reconnect_loop_and_sets_cooldown(self):
        """Kota hatasında (ResourceExhausted) yeniden bağlanma döngüsüne girmemeli, bekleme süresine almalı."""
        fake_doc = _FakeDocRef(fail_times=5, error_exc=RuntimeError("429 ResourceExhausted: Quota exceeded"))
        cloud_db.firestore_db = _FakeFirestore(fake_doc)

        with patch.object(cloud_db, "_reset_firebase_client") as mock_reset:
            with self.assertRaises(RuntimeError):
                cloud_db._firestore_call_with_retry(
                    lambda db: db.collection("sales").document("u1_s1").set({"ok": True}),
                    op_name="test_quota",
                    max_retries=3,
                )

        mock_reset.assert_not_called()
        self.assertGreater(cloud_db._firebase_quota_blocked_until, 0.0)
        self.assertIn("kota", (cloud_db.firebase_last_error or "").lower())

    def test_configure_firebase_alias_and_sync_status_health_fields(self):
        """configure_firebase metodu mevcut olmalı ve /api/sync/status sağlık alanlarını dönmeli."""
        self.assertTrue(callable(getattr(cloud_db, "configure_firebase", None)))
        client = app.test_client()
        resp = client.get("/api/sync/status", headers={"X-Gate-Pin": "2805"})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        self.assertIn("healthy", body)
        self.assertIn("consecutive_errors", body)
        self.assertIn("quota_blocked", body)

    def test_error_classifiers(self):
        """Geçici ağ hataları ve kota hataları doğru sınıflandırılmalı."""
        self.assertTrue(CloudDatabase._is_transient_firebase_error(RuntimeError("503 Unavailable: failed to connect")))
        self.assertTrue(CloudDatabase._is_transient_firebase_error(ConnectionResetError("Connection reset by peer")))
        self.assertTrue(CloudDatabase._is_transient_firebase_error(TimeoutError("DeadlineExceeded 504")))
        self.assertFalse(CloudDatabase._is_transient_firebase_error(RuntimeError("ResourceExhausted: Quota exceeded")))
        self.assertTrue(CloudDatabase._is_quota_error(RuntimeError("ResourceExhausted: Quota exceeded")))


if __name__ == "__main__":
    unittest.main()
