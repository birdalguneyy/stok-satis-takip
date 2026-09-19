from typing import Callable, List, Optional

import customtkinter as ctk

from app.models.product import Product
from app.services.sale_service import SaleService
from app.ui.components.barcode_entry import BarcodeEntry
from app.ui.components.data_table import DataTable
from app.ui.components.product_select_dialog import ProductSelectDialog
from app.ui.components.usb_scanner_dialog import USBScannerDialog
from app.ui.theme import (
    ACCENT,
    ERROR,
    FONT_BODY,
    FONT_HEADING,
    FONT_SMALL,
    FONT_TOTAL,
    STOCK_CRITICAL,
    STOCK_OUT,
    SUCCESS,
)
from app.utils.formatters import format_currency
from app.utils.sound import play_error_beep, play_success_beep


class SalesView(ctk.CTkFrame):
    def __init__(
        self,
        master,
        sale_service: SaleService,
        on_toast: Callable[[str, str], None],
        on_sale_complete: Callable[[], None],
        **kwargs,
    ) -> None:
        super().__init__(master, fg_color="transparent", **kwargs)
        self.sale_service = sale_service
        self.on_toast = on_toast
        self.on_sale_complete = on_sale_complete

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=8, pady=(0, 12))

        ctk.CTkLabel(header, text="Satış Hızlı Ekranı (POS)", font=FONT_HEADING).pack(side="left")

        badges_frame = ctk.CTkFrame(header, fg_color="transparent")
        badges_frame.pack(side="right")

        ctk.CTkButton(
            badges_frame,
            text="📱 / 🔌 Okuyucu Testi",
            font=FONT_SMALL,
            fg_color=("gray85", "gray25"),
            hover_color=ACCENT,
            text_color=("gray10", "gray90"),
            height=28,
            command=self._open_scanner_test,
        ).pack(side="left", padx=(0, 8))

        for key_text, desc in [("F2", "Arama Odaklan"), ("Enter", "Sepete Ekle"), ("Del", "Ürün Sil")]:
            badge = ctk.CTkFrame(badges_frame, fg_color=("gray85", "gray25"), corner_radius=6)
            badge.pack(side="left", padx=4)
            ctk.CTkLabel(
                badge, text=key_text, font=(FONT_SMALL[0], 10, "bold"), text_color=ACCENT
            ).pack(side="left", padx=(6, 2), pady=2)
            ctk.CTkLabel(
                badge, text=desc, font=FONT_SMALL, text_color=("gray30", "gray70")
            ).pack(side="left", padx=(2, 6), pady=2)

        self.barcode_entry = BarcodeEntry(
            self,
            placeholder="Barkod okutun veya ürün adı arayın... (Kısayol: F2)",
            on_submit=self._handle_scan,
        )
        self.barcode_entry.pack(fill="x", padx=8, pady=(0, 12))

        content = ctk.CTkFrame(self, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=8)
        content.grid_columnconfigure(0, weight=1)
        content.grid_rowconfigure(0, weight=1)

        cart_box = ctk.CTkFrame(content, corner_radius=12)
        cart_box.grid(row=0, column=0, sticky="nsew")

        cart_header = ctk.CTkFrame(cart_box, fg_color="transparent")
        cart_header.pack(fill="x", padx=16, pady=(16, 8))

        ctk.CTkLabel(cart_header, text="Sepetteki Ürünler", font=FONT_HEADING).pack(side="left")

        self.clear_cart_btn = ctk.CTkButton(
            cart_header,
            text="Sepeti Temizle",
            font=FONT_SMALL,
            fg_color=("gray75", "gray35"),
            hover_color=ERROR,
            width=100,
            command=self._clear_cart,
        )
        self.clear_cart_btn.pack(side="right")

        self.cart_table = DataTable(
            cart_box,
            columns=["Ürün Adı", "Birim Fiyat", "Miktar", "Ara Toplam", "İşlem"],
            column_weights=[4, 2, 2, 2, 1],
            empty_message="Sepetiniz henüz boş.",
            empty_hint="Ürün eklemek için yukarıdaki alana barkod okutun veya isim yazın.",
            empty_icon="🛒",
        )
        self.cart_table.pack(fill="both", expand=True, padx=12, pady=(0, 8))

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(fill="x", padx=8, pady=(12, 8))

        self.total_label = ctk.CTkLabel(
            footer,
            text=f"Genel Toplam: {format_currency(0)}",
            font=FONT_TOTAL,
        )
        self.total_label.pack(side="left")

        self.edit_total_btn = ctk.CTkButton(
            footer,
            text="✏️ Toplamı Düzenle",
            font=FONT_SMALL,
            width=125,
            height=32,
            fg_color=("gray75", "gray30"),
            hover_color=ACCENT,
            command=self._edit_total_amount,
        )
        self.edit_total_btn.pack(side="left", padx=(12, 4))

        self.reset_total_btn = ctk.CTkButton(
            footer,
            text="↺ Sıfırla",
            font=FONT_SMALL,
            width=70,
            height=32,
            fg_color=("gray75", "gray30"),
            hover_color=ERROR,
            command=self._reset_total_amount,
        )

        self.channel_selector = ctk.CTkSegmentedButton(
            footer,
            values=["🏬 Mağaza Satışı", "🌐 İnternet Satışı"],
            height=36,
            selected_color="#0284C7",
            selected_hover_color="#0369A1",
        )
        self.channel_selector.set("🏬 Mağaza Satışı")
        self.channel_selector.pack(side="left", padx=16)

        ctk.CTkButton(
            footer,
            text="Satışı Tamamla (Enter)",
            font=FONT_HEADING,
            height=48,
            width=220,
            fg_color=ACCENT,
            command=self._complete_sale,
        ).pack(side="right")

        self.after(100, self._bind_keys)

    def _bind_keys(self) -> None:
        try:
            top = self.winfo_toplevel()
            top.bind("<Delete>", self._on_delete_key)
            top.bind("<BackSpace>", self._on_delete_key)
            top.bind("<Key>", self._on_global_key)
        except Exception:
            pass

    def _open_scanner_test(self) -> None:
        USBScannerDialog(self)

    def on_show(self) -> None:
        self._bind_keys()
        self.barcode_entry.clear_and_focus()
        self.refresh()

    def _handle_scan(self, term: str) -> None:
        term_clean = term.strip()
        if not term_clean:
            return

        candidates = self.sale_service.search_candidates(term_clean)
        if not candidates:
            play_error_beep()
            self.on_toast(f"'{term_clean}' kodlu veya isimli ürün bulunamadı", "warning")
            return

        # Exact barcode match first
        exact_barcode = next((p for p in candidates if p.barcode == term_clean), None)
        if exact_barcode:
            self._add_product_to_cart(exact_barcode)
            return

        # Single candidate match
        if len(candidates) == 1:
            self._add_product_to_cart(candidates[0])
            return

        # Multiple candidate products match -> open selection dialog
        ProductSelectDialog(
            self,
            candidates=candidates,
            on_select=self._add_product_to_cart,
        )

    def _add_product_to_cart(self, product: Product) -> None:
        ok, message = self.sale_service.add_product(product)
        if ok:
            play_success_beep()
        else:
            play_error_beep()
        level = "success" if ok else "error"
        self.on_toast(message, level)
        self.refresh()
        self.barcode_entry.clear_and_focus()

    def _on_global_key(self, event) -> None:
        if not self.winfo_viewable():
            return
        focus_widget = self.focus_get()
        if focus_widget is not None:
            try:
                w_class = focus_widget.winfo_class()
                if w_class in ("Entry", "TEntry", "Text", "Spinbox", "Combobox", "Listbox"):
                    return
            except Exception:
                pass
            if isinstance(focus_widget, (ctk.CTkEntry, ctk.CTkInputDialog, ctk.CTkComboBox)):
                return
        if event.keysym in (
            "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12",
            "Delete", "BackSpace", "Return", "Tab", "Escape",
            "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R", "Caps_Lock"
        ):
            return
        if event.char and event.char.isprintable():
            self.barcode_entry.focus()
            self.barcode_entry.entry.insert("end", event.char)

    def _on_delete_key(self, event=None) -> None:
        if not self.winfo_viewable():
            return
        selected_id = self.cart_table.selected_row_id
        if selected_id:
            self._remove_item(selected_id)

    def _remove_item(self, product_id: int) -> None:
        ok, message = self.sale_service.remove_from_cart(product_id)
        self.on_toast(message, "info" if ok else "error")
        self.refresh()

    def _clear_cart(self) -> None:
        if not self.sale_service.cart_items:
            return
        self.sale_service.clear_cart()
        self.on_toast("Sepet temizlendi", "info")
        self.refresh()

    def refresh(self) -> None:
        self.cart_table.clear_rows()

        if not self.sale_service.cart_items:
            self.cart_table.show_empty(
                message="Sepetiniz henüz boş.",
                hint="Ürün eklemek için yukarıdaki kutuya barkod okutun veya arama yapın.",
                icon="🛒",
            )
            self.clear_cart_btn.configure(state="disabled")
            self.edit_total_btn.configure(state="disabled")
            self.reset_total_btn.pack_forget()
            self.total_label.configure(
                text=f"Genel Toplam: {format_currency(0)}"
            )
        else:
            self.clear_cart_btn.configure(state="normal")
            self.edit_total_btn.configure(state="normal")

            for item in self.sale_service.cart_items:
                delete_btn = ctk.CTkButton(
                    self.cart_table.body,
                    text="🗑️",
                    width=32,
                    height=28,
                    font=FONT_SMALL,
                    fg_color="transparent",
                    hover_color=("gray80", "gray35"),
                    text_color=ERROR,
                    command=lambda pid=item.product_id: self._remove_item(pid),
                )

                remaining = item.stock_quantity - item.quantity
                stock_color = None
                if remaining == 0:
                    stock_color = STOCK_OUT
                elif remaining <= 3:
                    stock_color = STOCK_CRITICAL

                price_text = format_currency(item.unit_price)
                if item.is_price_overridden:
                    price_text += " (Özel) ✏️"
                else:
                    price_text += " (çift tıkla)"

                row = self.cart_table.add_row(
                    values=[
                        item.product_name,
                        price_text,
                        f"{item.quantity} adet (çift tıkla)",
                        format_currency(item.subtotal),
                        "",
                    ],
                    row_id=item.product_id,
                    text_colors=[None, ACCENT if item.is_price_overridden else None, stock_color, None, None],
                    custom_widgets={4: delete_btn},
                )
                self._bind_row_events(row, item.product_id)

            if self.sale_service.is_custom_total:
                self.total_label.configure(
                    text=f"Genel Toplam: {format_currency(self.sale_service.total_amount)} (Özel Tutar - Normal: {format_currency(self.sale_service.calculated_total)})"
                )
                self.reset_total_btn.pack(side="left", padx=4)
            else:
                self.total_label.configure(
                    text=f"Genel Toplam: {format_currency(self.sale_service.total_amount)}"
                )
                self.reset_total_btn.pack_forget()

    def _bind_row_events(self, row, product_id: int) -> None:
        row.bind("<Button-1>", lambda e, pid=product_id: self.cart_table.set_selected_row(pid))
        for child in row.winfo_children():
            if not isinstance(child, ctk.CTkButton):
                child.bind("<Button-1>", lambda e, pid=product_id: self.cart_table.set_selected_row(pid))

        children = row.winfo_children()
        # Double click on price column (column index 1) to edit unit price
        if len(children) > 1:
            price_widget = children[1]
            price_widget.bind(
                "<Double-Button-1>",
                lambda e, pid=product_id: self._edit_unit_price(pid),
            )

        # Double click on quantity column (column index 2) to edit quantity
        if len(children) > 2:
            qty_widget = children[2]
            qty_widget.bind(
                "<Double-Button-1>",
                lambda e, pid=product_id: self._edit_quantity(pid),
            )

    def _edit_unit_price(self, product_id: int) -> None:
        item = next((i for i in self.sale_service.cart_items if i.product_id == product_id), None)
        if not item:
            return

        orig_str = f"{item.original_unit_price:.2f}" if item.original_unit_price is not None else f"{item.unit_price:.2f}"
        dialog = ctk.CTkInputDialog(
            text=f"'{item.product_name}' için birim satış fiyatını girin (TL):\n(Normal Liste Fiyatı: {orig_str} TL)",
            title="Birim Fiyatı Düzenle",
        )
        value = dialog.get_input()
        if value is None:
            return
        val_str = value.strip().replace(",", ".")
        if not val_str:
            return
        try:
            new_price = float(val_str)
            if new_price < 0:
                self.on_toast("Birim fiyat negatif olamaz", "error")
                return
        except ValueError:
            self.on_toast("Geçersiz fiyat formatı", "error")
            return

        ok, message = self.sale_service.update_item_price(product_id, new_price)
        self.on_toast(message, "success" if ok else "error")
        self.refresh()

    def _edit_quantity(self, product_id: int) -> None:
        dialog = ctk.CTkInputDialog(
            text="Yeni miktar girin:",
            title="Miktar Güncelle",
        )
        value = dialog.get_input()
        if value is None:
            return
        try:
            quantity = int(value.strip())
        except ValueError:
            self.on_toast("Geçersiz miktar", "error")
            return

        ok, message = self.sale_service.update_quantity(product_id, quantity)
        self.on_toast(message, "success" if ok else "error")
        self.refresh()

    def _edit_total_amount(self) -> None:
        if not self.sale_service.cart_items:
            self.on_toast("Sepet henüz boş", "warning")
            return

        calc_str = f"{self.sale_service.calculated_total:.2f}"
        dialog = ctk.CTkInputDialog(
            text=f"Bu satış fişi için Genel Toplam Tutarı girin (TL):\n(Hesaplanan Normal Tutar: {calc_str} TL)",
            title="Toplam Tutarı Düzenle",
        )
        value = dialog.get_input()
        if value is None:
            return
        val_str = value.strip().replace(",", ".")
        if not val_str:
            return
        try:
            new_total = float(val_str)
            if new_total < 0:
                self.on_toast("Toplam tutar negatif olamaz", "error")
                return
        except ValueError:
            self.on_toast("Geçersiz tutar formatı", "error")
            return

        self.sale_service.set_custom_total(new_total)
        self.on_toast(f"Genel toplam {new_total:.2f} TL olarak belirlendi", "info")
        self.refresh()

    def _reset_total_amount(self) -> None:
        self.sale_service.set_custom_total(None)
        self.on_toast("Genel toplam hesaplanan değere döndürüldü", "info")
        self.refresh()

    def _complete_sale(self) -> None:
        raw_val = self.channel_selector.get() if hasattr(self, "channel_selector") else ""
        channel = "internet" if "İnternet" in str(raw_val) else "magaza"
        ok, message = self.sale_service.complete_sale(channel=channel)
        level = "success" if ok else "error"
        self.on_toast(message, level)
        if ok:
            self.on_sale_complete()
        self.refresh()
        self.barcode_entry.clear_and_focus()


