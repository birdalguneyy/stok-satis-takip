import customtkinter as ctk

from app.services.dashboard_service import DashboardService
from app.ui.components.stat_card import StatCard
from app.ui.theme import ACCENT, ERROR, FONT_BODY, FONT_HEADING, FONT_SMALL, SUCCESS, WARNING
from app.utils.formatters import format_currency


class DashboardView(ctk.CTkFrame):
    def __init__(self, master, dashboard_service: DashboardService, **kwargs) -> None:
        super().__init__(master, fg_color="transparent", **kwargs)
        self.dashboard_service = dashboard_service

        header = ctk.CTkLabel(self, text="Gösterge Paneli", font=FONT_HEADING, anchor="w")
        header.pack(fill="x", padx=8, pady=(0, 16))

        cards_frame = ctk.CTkFrame(self, fg_color="transparent")
        cards_frame.pack(fill="x", padx=8)
        cards_frame.grid_columnconfigure((0, 1, 2), weight=1)

        self.card_total = StatCard(cards_frame, "Toplam Ürün", accent=ACCENT)
        self.card_total.grid(row=0, column=0, sticky="ew", padx=(0, 8))

        self.card_critical = StatCard(cards_frame, "Kritik Stok", accent=WARNING)
        self.card_critical.grid(row=0, column=1, sticky="ew", padx=4)

        self.card_sales = StatCard(cards_frame, "Bugünkü Satış", accent=SUCCESS)
        self.card_sales.grid(row=0, column=2, sticky="ew", padx=(8, 0))

        lists_frame = ctk.CTkFrame(self, fg_color="transparent")
        lists_frame.pack(fill="both", expand=True, padx=8, pady=(24, 8))
        lists_frame.grid_columnconfigure((0, 1), weight=1)
        lists_frame.grid_rowconfigure(0, weight=1)

        top_frame = ctk.CTkFrame(lists_frame, corner_radius=12)
        top_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 8))

        ctk.CTkLabel(
            top_frame, text="En Çok Satılan Ürünler", font=FONT_HEADING, anchor="w"
        ).pack(fill="x", padx=16, pady=(16, 8))

        self.top_list = ctk.CTkFrame(top_frame, fg_color="transparent")
        self.top_list.pack(fill="both", expand=True, padx=16, pady=(0, 16))

        critical_frame = ctk.CTkFrame(lists_frame, corner_radius=12)
        critical_frame.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

        ctk.CTkLabel(
            critical_frame, text="Kritik Stok Uyarıları", font=FONT_HEADING, anchor="w"
        ).pack(fill="x", padx=16, pady=(16, 8))

        self.critical_list = ctk.CTkFrame(critical_frame, fg_color="transparent")
        self.critical_list.pack(fill="both", expand=True, padx=16, pady=(0, 16))

    def refresh(self) -> None:
        stats = self.dashboard_service.get_stats()
        self.card_total.set_value(str(stats.total_products))
        self.card_critical.set_value(str(stats.critical_stock_count))
        self.card_sales.set_value(str(stats.today_sales_count))

        for widget in self.top_list.winfo_children():
            widget.destroy()

        if not stats.top_products:
            ctk.CTkLabel(
                self.top_list,
                text="Henüz satış verisi yok.",
                font=FONT_BODY,
                text_color="gray",
            ).pack(anchor="w", pady=4)
        else:
            for i, (name, qty) in enumerate(stats.top_products, start=1):
                row = ctk.CTkFrame(self.top_list, fg_color=("gray92", "gray18"), corner_radius=8)
                row.pack(fill="x", pady=3)
                ctk.CTkLabel(row, text=f"{i}.", font=FONT_BODY, width=30).pack(
                    side="left", padx=(12, 4), pady=10
                )
                ctk.CTkLabel(row, text=name, font=FONT_BODY, anchor="w").pack(
                    side="left", fill="x", expand=True
                )
                ctk.CTkLabel(row, text=f"{qty} adet", font=FONT_BODY, text_color="gray").pack(
                    side="right", padx=12
                )

        for widget in self.critical_list.winfo_children():
            widget.destroy()

        if not stats.critical_products:
            ctk.CTkLabel(
                self.critical_list,
                text="Kritik stokta ürün yok.",
                font=FONT_BODY,
                text_color="gray",
            ).pack(anchor="w", pady=4)
        else:
            for product in stats.critical_products:
                row = ctk.CTkFrame(self.critical_list, fg_color=("gray92", "gray18"), corner_radius=8)
                row.pack(fill="x", pady=3)

                stock_color = ERROR if product.stock_quantity == 0 else WARNING
                stock_text = "Tükendi" if product.stock_quantity == 0 else f"{product.stock_quantity} adet"

                ctk.CTkLabel(row, text=product.name, font=FONT_BODY, anchor="w").pack(
                    side="left", fill="x", expand=True, padx=12, pady=10
                )
                ctk.CTkLabel(
                    row,
                    text=stock_text,
                    font=FONT_SMALL,
                    text_color=stock_color,
                ).pack(side="right", padx=12)
