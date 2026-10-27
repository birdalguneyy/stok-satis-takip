import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.ui.components.farm_password_dialog import (
    AccessPasswordDialog,
    FarmPasswordDialog,
    MASTER_PASSWORDS,
    NORMAL_PASSWORDS,
)
from app.web.web_server import app


class TestScreenAccessSecurity(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_passwords_constants(self):
        """Doğru şifre sabitleri kontrolü: 28052024 ve 2805 normal, 0528 ve 05282024 master şifre olmalıdır."""
        self.assertIn("28052024", NORMAL_PASSWORDS)
        self.assertIn("2805", NORMAL_PASSWORDS)
        self.assertIn("0528", MASTER_PASSWORDS)
        self.assertIn("05282024", MASTER_PASSWORDS)

    def test_web_farm_auth_check_passwords(self):
        """Web API /api/farm/auth-check doğrulama testi.
        2805 personel şifresidir; is_master=False döner.
        0528 yönetici şifresidir; is_master=True döner.
        """
        # 1. 2805 personel şifresi (is_master=False dönmeli)
        res = self.client.post("/api/farm/auth-check", json={"password": "2805"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json().get("ok"))
        self.assertFalse(res.get_json().get("is_master"))

        # 2. 28052024 personel şifresi (is_master=False dönmeli)
        res = self.client.post("/api/farm/auth-check", json={"password": "28052024"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json().get("ok"))
        self.assertFalse(res.get_json().get("is_master"))

        # 3. 0528 yönetici master şifresi (200 OK)
        res = self.client.post("/api/farm/auth-check", json={"password": "0528"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json().get("ok"))
        self.assertTrue(res.get_json().get("is_master"))

        # 4. 05282024 yönetici master şifresi (200 OK)
        res = self.client.post("/api/farm/auth-check", json={"password": "05282024"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json().get("ok"))
        self.assertTrue(res.get_json().get("is_master"))

        # 5. Hatalı şifre (401 Unauthorized)
        res = self.client.post("/api/farm/auth-check", json={"password": "wrong"})
        self.assertEqual(res.status_code, 401)
        self.assertFalse(res.get_json().get("ok"))

    def test_web_gate_verify_passwords(self):
        """Web API /api/gate/verify doğrulama testi."""
        # 1. 2805 (personel girişi: POS açık, diğerleri kısıtlı)
        res = self.client.post("/api/gate/verify", json={"pin": "2805"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json().get("ok"))
        self.assertFalse(res.get_json().get("is_master"))

        # 2. 0528 (yönetici master girişi: tüm bölümler açık)
        res = self.client.post("/api/gate/verify", json={"pin": "0528"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json().get("ok"))
        self.assertTrue(res.get_json().get("is_master"))

        # 3. Hatalı şifre (401 dönmeli)
        res = self.client.post("/api/gate/verify", json={"pin": "0000"})
        self.assertEqual(res.status_code, 401)
        self.assertFalse(res.get_json().get("ok"))

    def test_web_caching_headers(self):
        """Web sunucusunun index.html, sw.js ve manifest dosyalarında no-cache başlıkları gönderdiğini test eder."""
        res_root = self.client.get("/")
        self.assertEqual(res_root.status_code, 200)
        self.assertIn("no-store", res_root.headers.get("Cache-Control", ""))

        res_sw = self.client.get("/sw.js")
        self.assertEqual(res_sw.status_code, 200)
        self.assertIn("no-store", res_sw.headers.get("Cache-Control", ""))

    def test_index_html_role_security(self):
        """index.html içeriğinde personel modu kısıtlamalarının ve CSS izolasyonunun varlığını test eder."""
        index_path = Path(__file__).resolve().parent.parent / "app" / "web" / "static" / "index.html"
        self.assertTrue(index_path.exists())
        content = index_path.read_text(encoding="utf-8")

        # 1. CSS kuralı: role-admin olmayan gövdede admin-only ve alt sekmeler gizlenmelidir
        self.assertIn("body:not(.role-admin) .admin-only", content)
        self.assertIn("body.role-personnel .admin-only", content)
        self.assertIn("display: none !important;", content)

        # 2. Alt sekmeler admin-only olarak işaretlenmiş olmalı
        self.assertIn('id="nav-btn-stock"', content)
        self.assertIn('id="nav-btn-reports"', content)
        self.assertIn('id="nav-btn-ai"', content)
        self.assertIn('id="farm-badge"', content)

        # 3. 2805 personel şifresinin farm pin modalında hata verip executePendingAuthAction çağırmadığı
        self.assertIn("Personel şifresi girildi. Bu alana erişim için Yönetici şifresi gereklidir.", content)

        # 4. Kullanıcıya açık arayüzde şifrelerin (2805, 0528) ipucu olarak yazılmadığı
        self.assertNotIn("Personel: 2805", content)
        self.assertNotIn("Yönetici: 0528", content)

    def test_sidebar_role_visibility(self):
        """Sidebar nesnesinde personel rolünde sadece sales butonunun kalması testi."""
        from app.ui.components.sidebar import Sidebar
        with patch.object(Sidebar, "__init__", lambda self, *args, **kwargs: None):
            sb = Sidebar(None, None, None)
            btn_dash = MagicMock()
            btn_products = MagicMock()
            btn_sales = MagicMock()
            btn_reports = MagicMock()
            sb._buttons = {
                "dashboard": btn_dash,
                "products": btn_products,
                "sales": btn_sales,
                "reports": btn_reports,
            }
            sb.role_badge = MagicMock()

            # Personel moduna geç
            sb.set_role("personnel")
            # sales hariç diğerleri pack_forget olmalı
            btn_dash.pack_forget.assert_called()
            btn_products.pack_forget.assert_called()
            btn_reports.pack_forget.assert_called()
            btn_sales.pack.assert_called()

            # Yönetici moduna geç
            btn_dash.reset_mock()
            btn_products.reset_mock()
            btn_sales.reset_mock()
            btn_reports.reset_mock()
            sb.set_role("admin")
            # Hepsi tekrar eklenmeli
            btn_dash.pack.assert_called()
            btn_products.pack.assert_called()
            btn_sales.pack.assert_called()
            btn_reports.pack.assert_called()

    def test_desktop_role_and_navigation_logic(self):
        """Masaüstü App nesnesinde 2805 personel ve 0528 yönetici rol yetkilendirme testi."""
        from app.ui.app import App

        with patch.object(App, "__init__", lambda self: None):
            app_instance = App()
            app_instance.user_role = "personnel"
            app_instance.is_unlocked = False
            app_instance.active_view_key = "sales"
            app_instance.views = {"sales": MagicMock(), "dashboard": MagicMock(), "products": MagicMock()}
            app_instance.sidebar = MagicMock()
            app_instance.show_view = MagicMock()
            app_instance.show_toast = MagicMock()
            app_instance.lock_btn = MagicMock()
            app_instance.farm_btn = MagicMock()
            app_instance.sync_btn = MagicMock()

            # 1. Personel modunda Satış (POS) erişimi serbesttir
            app_instance.active_view_key = "products"
            app_instance.request_navigation("sales")
            app_instance.show_view.assert_called_with("sales")

            # 2. Personel modunda korumalı ekrana (dashboard) geçiş engellenir, şifre sorulur
            app_instance.show_view.reset_mock()
            with patch("app.ui.app.AccessPasswordDialog") as mock_dialog:
                app_instance.request_navigation("dashboard")
                app_instance.show_view.assert_not_called()
                mock_dialog.assert_called_once()

                # Personel şifresi (2805) girilirse yetki verilmez
                callback = mock_dialog.call_args[1]["on_success"]
                callback(is_master=False)
                app_instance.show_view.assert_not_called()
                self.assertEqual(app_instance.user_role, "personnel")

                # Yönetici şifresi (0528) girilirse yönetici rolüne geçer ve ekran açılır
                callback(is_master=True)
                self.assertEqual(app_instance.user_role, "admin")
                self.assertTrue(app_instance.is_unlocked)
                app_instance.show_view.assert_called_with("dashboard")

            # 3. Yönetici modundayken her ekrana doğrudan geçilebilir
            app_instance.show_view.reset_mock()
            app_instance.active_view_key = "dashboard"
            with patch("app.ui.app.AccessPasswordDialog") as mock_dialog:
                app_instance.request_navigation("products")
                mock_dialog.assert_not_called()
                app_instance.show_view.assert_called_with("products")

            # 4. toggle_lock_mode ile personel moduna dönülebilmeli ve menüler gizlenmeli
            app_instance.toggle_lock_mode()
            self.assertEqual(app_instance.user_role, "personnel")
            self.assertFalse(app_instance.is_unlocked)
            app_instance.sidebar.set_role.assert_called_with("personnel")


if __name__ == "__main__":
    unittest.main()
