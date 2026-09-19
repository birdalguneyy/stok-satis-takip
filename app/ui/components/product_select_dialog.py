from typing import Callable, List, Optional

import customtkinter as ctk

from app.models.product import Product
from app.ui.theme import ACCENT, FONT_BODY, FONT_HEADING, FONT_SMALL
from app.utils.formatters import format_currency


class ProductSelectDialog(ctk.CTkToplevel):
    """Satış ekranında arama sonucu birden fazla ürün eşleştiğinde kullanıcının doğru ürünü seçmesini sağlayan pencere."""

    def __init__(
        self,
        parent,
        candidates: List[Product],
        on_select: Callable[[Product], None],
    ) -> None:
        super().__init__(parent)
        self.candidates = candidates
        self.on_select = on_select

        self.title("🔍 Ürün Seçimi")
        self.geometry("640x500")
        self.resizable(False, False)

        self.transient(parent)
        self.grab_set()

        self._build_ui()
        self.bind("<Escape>", lambda e: self.destroy())

    def _build_ui(self) -> None:
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(16, 8))

        ctk.CTkLabel(
            header,
            text=f"Eşleşen Ürünler ({len(self.candidates)} Ürün)",
            font=FONT_HEADING,
        ).pack(anchor="w")

        ctk.CTkLabel(
            header,
            text="Birden fazla ürün bulundu. Lütfen sepete eklemek istediğiniz ürünü seçin:",
            font=FONT_SMALL,
            text_color=("gray40", "gray70"),
        ).pack(anchor="w", pady=(2, 0))

        # Filter entry inside dialog
        filter_frame = ctk.CTkFrame(self, fg_color="transparent")
        filter_frame.pack(fill="x", padx=20, pady=(6, 8))

        self.filter_entry = ctk.CTkEntry(
            filter_frame,
            placeholder_text="Sonuçlar içinde filtrele...",
            font=FONT_BODY,
            height=36,
        )
        self.filter_entry.pack(fill="x")
        self.filter_entry.bind("<KeyRelease>", self._on_filter_changed)

        # Scrollable list of candidate products
        self.scroll_frame = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.scroll_frame.pack(fill="both", expand=True, padx=20, pady=(0, 12))

        self._render_items(self.candidates)

        # Bottom close button
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(fill="x", padx=20, pady=(0, 16))

        ctk.CTkButton(
            footer,
            text="İptal (Esc)",
            font=FONT_BODY,
            fg_color=("gray75", "gray30"),
            hover_color=("gray65", "gray40"),
            text_color=("black", "white"),
            width=100,
            command=self.destroy,
        ).pack(side="right")

    def _on_filter_changed(self, event=None) -> None:
        query = self.filter_entry.get().strip().lower()
        if not query:
            filtered = self.candidates
        else:
            filtered = [
                p for p in self.candidates
                if query in p.name.lower() or (p.barcode and query in p.barcode.lower())
            ]
        self._render_items(filtered)

    def _render_items(self, products: List[Product]) -> None:
        for widget in self.scroll_frame.winfo_children():
            widget.destroy()

        if not products:
            ctk.CTkLabel(
                self.scroll_frame,
                text="Filtreye uygun ürün bulunamadı.",
                font=FONT_BODY,
                text_color=("gray40", "gray70"),
            ).pack(pady=30)
            return

        for prod in products:
            card = ctk.CTkFrame(self.scroll_frame, corner_radius=8)
            card.pack(fill="x", pady=4, padx=2)

            info_box = ctk.CTkFrame(card, fg_color="transparent")
            info_box.pack(side="left", fill="both", expand=True, padx=12, pady=10)

            title_label = ctk.CTkLabel(
                info_box,
                text=prod.name,
                font=(FONT_BODY[0], 13, "bold"),
                anchor="w",
            )
            title_label.pack(anchor="w")

            detail_text = f"Barkod: {prod.barcode or 'Barkodsuz'} | Stok: {prod.stock_quantity} Adet | Kategori: {prod.category_name or 'Genel'}"
            sub_label = ctk.CTkLabel(
                info_box,
                text=detail_text,
                font=FONT_SMALL,
                text_color=("gray40", "gray70"),
                anchor="w",
            )
            sub_label.pack(anchor="w", pady=(2, 0))

            # Right actions
            action_box = ctk.CTkFrame(card, fg_color="transparent")
            action_box.pack(side="right", padx=12, pady=10)

            price_label = ctk.CTkLabel(
                action_box,
                text=format_currency(prod.sale_price),
                font=(FONT_BODY[0], 14, "bold"),
                text_color=ACCENT,
            )
            price_label.pack(side="left", padx=(0, 12))

            select_btn = ctk.CTkButton(
                action_box,
                text="➕ Seç",
                font=FONT_BODY,
                fg_color=ACCENT,
                width=80,
                height=32,
                command=lambda p=prod: self._select_product(p),
            )
            select_btn.pack(side="left")

            # Click on the card itself to select
            card.bind("<Button-1>", lambda e, p=prod: self._select_product(p))
            title_label.bind("<Button-1>", lambda e, p=prod: self._select_product(p))
            sub_label.bind("<Button-1>", lambda e, p=prod: self._select_product(p))

    def _select_product(self, product: Product) -> None:
        self.destroy()
        self.on_select(product)
