import customtkinter as ctk

from app.config import APP_NAME, WINDOW_MIN_HEIGHT, WINDOW_MIN_WIDTH
from app.database.migrations import run_migrations
from app.services.dashboard_service import DashboardService
from app.services.product_service import ProductService
from app.services.sale_service import SaleService
from app.ui.components.farm_password_dialog import FarmPasswordDialog
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

        self.sidebar = Sidebar(
            self,
            on_navigate=self.show_view,
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

        self.show_view("dashboard")
        self.bind("<F2>", self._on_f2_pressed)

        # Sağ Üst Köşe Çubuğu: Çiftlik Giriş Butonu ve Firebase Durum Rozeti
        self.top_actions_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.top_actions_frame.place(relx=0.985, rely=0.015, anchor="ne")

        # 🚜 Sağ Üst Köşe Çiftlik Butonu (Şifre: 2805)
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
        self.farm_btn.pack(side="left", padx=(0, 8))

        try:
            from app.database.cloud_db import CloudDatabase
            cloud_db = CloudDatabase()
            has_fb = bool(cloud_db.firestore_db)
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

    def _open_farm_module(self) -> None:
        """Sağ üstteki Çiftlik butonuna tıklandığında 2805 şifresini sorgular."""
        FarmPasswordDialog(self, on_success=self._on_farm_auth_success)

    def _on_farm_auth_success(self) -> None:
        self.show_toast("Çiftlik Modülüne başarıyla giriş yapıldı.", "success")
        self.show_view("farm")

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
