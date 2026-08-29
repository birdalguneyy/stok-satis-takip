import customtkinter as ctk

from app.config import APP_NAME, WINDOW_MIN_HEIGHT, WINDOW_MIN_WIDTH
from app.database.migrations import run_migrations
from app.services.dashboard_service import DashboardService
from app.services.product_service import ProductService
from app.services.sale_service import SaleService
from app.ui.components.sidebar import Sidebar
from app.ui.components.toast import Toast
from app.ui.views.dashboard_view import DashboardView
from app.ui.views.products_view import ProductsView
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

        for view in self.views.values():
            view.grid(row=0, column=0, sticky="nsew")

    def show_view(self, key: str) -> None:
        self.sidebar.set_active(key)
        for name, view in self.views.items():
            if name == key:
                view.tkraise()
                if hasattr(view, "refresh"):
                    view.refresh()
                if hasattr(view, "on_show"):
                    view.on_show()
            else:
                if key != "sales" and name == "sales":
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
