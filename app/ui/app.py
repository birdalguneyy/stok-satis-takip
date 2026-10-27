import customtkinter as ctk

from app.config import APP_NAME, WINDOW_MIN_HEIGHT, WINDOW_MIN_WIDTH
from app.database.migrations import run_migrations
from app.services.dashboard_service import DashboardService
from app.services.product_service import ProductService
from app.services.sale_service import SaleService
from app.ui.components.farm_password_dialog import AccessPasswordDialog, FarmPasswordDialog
from app.ui.components.sidebar import Sidebar
from app.ui.components.toast import Toast
from app.ui.views.dashboard_view import DashboardView
from app.ui.views.farm_view import FarmView
from app.ui.views.history_view import HistoryView
from app.ui.views.products_view import ProductsView
from app.ui.views.reports_view import ReportsView
from app.ui.views.sales_view import SalesView


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        run_migrations()

        self.title(APP_NAME)
        self.geometry("1200x760")
        self.minsize(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT)

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.dashboard_service = DashboardService()
        self.product_service = ProductService()
        self.sale_service = SaleService(self.product_service)

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.user_role: str = "personnel"
        self.is_unlocked: bool = False

        self.sidebar = Sidebar(
            self,
            on_navigate=self.request_navigation,
            on_toggle_theme=self.toggle_theme,
        )
        self.sidebar.grid(row=0, column=0, sticky="ns")

        self.content = ctk.CTkFrame(self, fg_color="transparent")
        self.content.grid(row=0, column=1, sticky="nsew", padx=16, pady=16)
        self.content.grid_rowconfigure(0, weight=1)
        self.content.grid_columnconfigure(0, weight=1)

        self.views: dict[str, ctk.CTkFrame] = {}
        self._build_views()
        self.toast = Toast(self)

        self.active_view_key = "sales"
        self.show_view("sales")
        self.bind("<F2>", self._on_f2_pressed)

        # Sağ Üst Köşe Çubuğu: Kilit Durumu, Çiftlik Giriş, Bulut Eşitle, Web Modu ve Firebase Rozeti
        self.top_actions_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.top_actions_frame.place(relx=0.985, rely=0.015, anchor="ne")

        # 🔒 / 🔓 Kilit ve Güvenlik Durumu Butonu
        self.lock_btn = ctk.CTkButton(
            self.top_actions_frame,
            text="🔒 Korumalı",
            font=("Segoe UI", 11, "bold"),
            fg_color=("#E2E8F0", "#334155"),
            text_color=("gray20", "gray85"),
            hover_color=("#CBD5E1", "#475569"),
            height=28,
            width=88,
            corner_radius=12,
            command=self.toggle_lock_mode,
        )
        self.lock_btn.pack(side="left", padx=(0, 6))

        # 🚜 Sağ Üst Köşe Çiftlik Butonu
        self.farm_btn = ctk.CTkButton(
            self.top_actions_frame,
            text="🚜 Çiftlik",
            font=("Segoe UI", 12, "bold"),
            fg_color="#059669",
            hover_color="#047857",
            height=28,
            width=88,
            corner_radius=12,
            command=self._open_farm_module,
        )
        self.farm_btn.pack(side="left", padx=(0, 6))

        # 🔄 Bulut Eşitle Butonu
        self.sync_btn = ctk.CTkButton(
            self.top_actions_frame,
            text="🔄 Bulut Eşitle",
            font=("Segoe UI", 11, "bold"),
            fg_color="#2563EB",
            hover_color="#1D4ED8",
            height=28,
            width=105,
            corner_radius=12,
            command=self._manual_firebase_sync,
        )
        self.sync_btn.pack(side="left", padx=(0, 6))

        # 🌐 Tam Özellikli Yerel Web Arayüzü Butonu
        self.web_btn = ctk.CTkButton(
            self.top_actions_frame,
            text="🌐 Web Modu",
            font=("Segoe UI", 11, "bold"),
            fg_color="#4F46E5",
            hover_color="#4338CA",
            height=28,
            width=96,
            corner_radius=12,
            command=self._open_local_web_app,
        )
        self.web_btn.pack(side="left", padx=(0, 8))

        self._is_syncing_cloud = False

        try:
            from app.database.cloud_db import CloudDatabase
            cloud_db = CloudDatabase()
            has_fb = bool(cloud_db.ensure_firebase())
            status_text = "🔥 🟢 Firebase Cloud Aktif" if has_fb else "🔥 🟡 Firebase Yerel (SQLite)"
            status_color = "#10B981" if has_fb else "#F59E0B"

            self.fb_badge = ctk.CTkLabel(
                self.top_actions_frame,
                text=status_text,
                font=("Segoe UI", 11, "bold"),
                text_color=status_color,
                fg_color="#1E293B",
                corner_radius=12,
                padx=10,
                pady=4,
            )
            self.fb_badge.pack(side="left")
        except Exception:
            pass

        # Rolü personel olarak başlatıp diğer bölümleri gizle
        self.set_user_role("personnel")
        # Açılışta şifre sor (2805: Personel - Yalnızca POS, 0528: Yönetici - Tam yetki)
        self.after(200, self._show_startup_login)
        # Başlangıçta ve periyodik olarak Firebase ile çift yönlü eşitleme başlat
        self.after(1200, lambda: self._run_background_firebase_sync(manual=False))

    def _open_local_web_app(self) -> None:
        """Render'a gerek kalmadan yerel sunucuyu (http://localhost:5000) uygulama penceresi olarak açar."""
        import subprocess
        import webbrowser
        url = "http://localhost:5000"
        for browser_cmd in [
            ["cmd", "/c", "start", "msedge", f"--app={url}"],
            ["cmd", "/c", "start", "chrome", f"--app={url}"],
        ]:
            try:
                res = subprocess.run(browser_cmd, capture_output=True)
                if res.returncode == 0:
                    return
            except Exception:
                pass
        webbrowser.open(url)

    def _manual_firebase_sync(self) -> None:
        self._run_background_firebase_sync(manual=True)

    def _run_background_firebase_sync(self, manual: bool = False) -> None:
        if getattr(self, "_is_syncing_cloud", False):
            if manual:
                self.show_toast("Bulut eşitleme zaten devam ediyor...", "info")
            return
        self._is_syncing_cloud = True
        if manual and hasattr(self, "sync_btn"):
            try:
                self.sync_btn.configure(state="disabled", text="⏳ Eşitleniyor...")
            except Exception:
                pass

        import threading
        def _worker():
            res = {"synced": False, "pushed": 0, "pulled": 0, "reason": ""}
            has_fb = False
            try:
                from app.database.cloud_db import CloudDatabase
                cdb = CloudDatabase()
                has_fb = bool(cdb.ensure_firebase())
                if has_fb:
                    if manual:
                        cdb._last_pull_timestamp = 0.0
                    res = cdb.sync_offline_data_with_firebase(allow_pull=True)
            except Exception as exc:
                res["reason"] = str(exc)

            def _on_done():
                self._is_syncing_cloud = False
                if hasattr(self, "sync_btn"):
                    try:
                        self.sync_btn.configure(state="normal", text="🔄 Bulut Eşitle")
                    except Exception:
                        pass
                if hasattr(self, "fb_badge"):
                    try:
                        self.fb_badge.configure(
                            text="🔥 🟢 Firebase Cloud Aktif" if has_fb else "🔥 🟡 Firebase Yerel (SQLite)",
                            text_color="#10B981" if has_fb else "#F59E0B",
                        )
                    except Exception:
                        pass
                if res.get("pulled", 0) > 0 or res.get("pushed", 0) > 0 or manual:
                    cur_view = self.views.get(getattr(self, "active_view_key", "dashboard"))
                    if cur_view:
                        if hasattr(cur_view, "refresh"):
                            cur_view.refresh()
                        if hasattr(cur_view, "on_show"):
                            cur_view.on_show()
                if manual:
                    if res.get("synced"):
                        self.show_toast(res.get("reason") or "Firebase bulut eşitlemesi tamamlandı.", "success")
                    else:
                        self.show_toast(res.get("reason") or "Firebase bağlantısı kurulamadı.", "warning")

            try:
                self.after(0, _on_done)
            except Exception:
                self._is_syncing_cloud = False

        threading.Thread(target=_worker, daemon=True).start()
        if not manual:
            try:
                self.after(60000, lambda: self._run_background_firebase_sync(manual=False))
            except Exception:
                pass

    def set_user_role(self, role: str) -> None:
        """Kullanıcı rolünü ayarlar: 'admin' (0528) veya 'personnel' (2805)."""
        self.user_role = role
        self.is_unlocked = (role == "admin")
        if hasattr(self, "sidebar"):
            self.sidebar.set_role(role)
        self._update_role_ui()
        if role == "personnel":
            self.show_view("sales")

    def _update_role_ui(self) -> None:
        if not hasattr(self, "lock_btn"):
            return
        if self.user_role == "admin":
            self.lock_btn.configure(
                text="🔒 Personel Moduna Geç",
                fg_color="#D97706",
                hover_color="#B45309",
                text_color="#FFFFFF",
                width=175,
            )
            # Çiftlik butonunu göster
            if hasattr(self, "farm_btn") and hasattr(self, "sync_btn"):
                self.farm_btn.pack(side="left", padx=(0, 6), before=self.sync_btn)
        else:
            self.lock_btn.configure(
                text="👑 Yönetici Girişi",
                fg_color="#2563EB",
                hover_color="#1D4ED8",
                text_color="#FFFFFF",
                width=140,
            )
            # Çiftlik butonunu personel modunda tamamen gizle
            if hasattr(self, "farm_btn"):
                self.farm_btn.pack_forget()

    def _show_startup_login(self) -> None:
        def on_login(is_master: bool = False):
            if is_master:
                self.set_user_role("admin")
                self.show_toast("👑 Yönetici Modu Aktif. Tüm bölümler açık.", "success")
            else:
                self.set_user_role("personnel")
                self.show_toast("👤 Personel Modu Aktif. Yalnızca Satış (POS) açık.", "info")

        def on_cancel():
            self.set_user_role("personnel")

        AccessPasswordDialog(
            self,
            title="🔐 Sistem Girişi",
            heading="🔐 Giriş Doğrulama",
            prompt="Lütfen erişim şifrenizi giriniz\n(2805: Personel | 0528: Yönetici):",
            on_success=on_login,
            on_cancel=on_cancel,
        )

    def toggle_lock_mode(self) -> None:
        """Yönetici ve Personel modu arasında geçiş yapar."""
        if self.user_role == "admin":
            self.set_user_role("personnel")
            self.show_toast("🔒 Personel moduna geçildi. Diğer bölümler gizlendi.", "info")
        else:
            def on_auth_success(is_master: bool = False):
                if is_master:
                    self.set_user_role("admin")
                    self.show_toast("👑 Yönetici Modu Aktif. Tüm bölümler açıldı.", "success")
                else:
                    self.show_toast("⚠️ 2805 Personel şifresidir. Tüm bölümler için 0528 yönetici şifresini giriniz.", "warning")

            AccessPasswordDialog(
                self,
                title="👑 Yönetici Girişi",
                heading="👑 Yönetici Yetkilendirmesi",
                prompt="Tüm bölümleri açmak için yönetici şifresini (0528) giriniz:",
                on_success=on_auth_success,
            )

    def request_navigation(self, key: str) -> None:
        target_key = "reports" if key == "history" else key

        if target_key == self.active_view_key:
            return

        # Satış ekranı (POS) her zaman açıktır
        if target_key == "sales":
            self.show_view("sales")
            return

        # Personel modundaysa diğer kısımlar tamamen kapalıdır ve gözükmez. Yönetici şifresi (0528) istenir.
        if self.user_role == "personnel":
            def on_auth_success(is_master: bool = False):
                if is_master:
                    self.set_user_role("admin")
                    self.show_toast("👑 Yönetici Modu Aktif.", "success")
                    self.show_view(target_key)
                else:
                    self.show_toast("⚠️ Bu bölüme sadece Yönetici (0528) erişebilir!", "warning")

            AccessPasswordDialog(
                self,
                title="👑 Yönetici Girişi",
                heading="👑 Yönetici Yetkisi Gerekli",
                prompt="Bu alana sadece yönetici (0528) erişebilir. Şifre giriniz:",
                on_success=on_auth_success,
            )
            return

        # Yönetici modunda ise serbestçe geçiş yapılır
        self.show_view(target_key)

    def _open_farm_module(self) -> None:
        """Sağ üstteki Çiftlik butonuna tıklandığında geçişi başlatır."""
        self.request_navigation("farm")

    def _on_f2_pressed(self, _event=None) -> None:
        self.show_view("sales")
        sales_view = self.views.get("sales")
        if sales_view and hasattr(sales_view, "barcode_entry"):
            sales_view.barcode_entry.clear_and_focus()

    def _build_views(self) -> None:

        self.views["dashboard"] = DashboardView(self.content, self.dashboard_service)
        self.views["products"] = ProductsView(
            self.content,
            self.product_service,
            on_toast=self.show_toast,
        )
        self.views["sales"] = SalesView(
            self.content,
            self.sale_service,
            on_toast=self.show_toast,
            on_sale_complete=self._on_sale_complete,
        )
        self.views["history"] = HistoryView(
            self.content,
            self.sale_service,
            on_toast=self.show_toast,
            on_stock_changed=self._on_stock_changed,
        )

        self.views["reports"] = ReportsView(
            self.content,
            self.sale_service,
            on_toast=self.show_toast,
            on_stock_changed=self._on_stock_changed,
        )

        self.views["farm"] = FarmView(
            self.content,
            on_toast=self.show_toast,
            on_back_to_shop=lambda: self.show_view("sales"),
        )

        for view in self.views.values():
            view.grid(row=0, column=0, sticky="nsew")

    def show_view(self, key: str) -> None:
        target_key = "reports" if key == "history" else key
        self.active_view_key = target_key
        if target_key in self.sidebar._buttons:
            self.sidebar.set_active(target_key)
        else:
            # Çiftlik ekranındayken kenar çubuğu butonlarının seçimini kaldır
            for b in self.sidebar._buttons.values():
                b.configure(fg_color="transparent", text_color=("gray20", "gray90"))

        for name, view in self.views.items():
            if name == target_key:
                view.tkraise()
                if hasattr(view, "refresh"):
                    view.refresh()
                if hasattr(view, "on_show"):
                    view.on_show()
            else:
                if target_key != "sales" and name == "sales":
                    pass

    def show_toast(self, message: str, level: str = "info") -> None:

        self.toast.show(message, level)

    def toggle_theme(self) -> None:
        mode = ctk.get_appearance_mode()
        ctk.set_appearance_mode("light" if mode == "Dark" else "dark")

    def _on_sale_complete(self) -> None:
        dashboard = self.views.get("dashboard")
        if dashboard and hasattr(dashboard, "refresh"):
            dashboard.refresh()
        reports = self.views.get("reports")
        if reports and hasattr(reports, "on_show"):
            reports.on_show()
        history = self.views.get("history")
        if history and hasattr(history, "refresh"):
            history.refresh()

    def _on_stock_changed(self) -> None:
        dashboard = self.views.get("dashboard")
        if dashboard and hasattr(dashboard, "refresh"):
            dashboard.refresh()
        products = self.views.get("products")
        if products and hasattr(products, "refresh"):
            products.refresh()
