from typing import Callable

import customtkinter as ctk

from app.services.sale_service import SaleService
from app.ui.components.barcode_entry import BarcodeEntry
from app.ui.components.data_table import DataTable
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
        ok, message = self.sale_service.add_by_scan(term)
        if ok:
            play_success_beep()
        else:
            play_error_beep()
        level = "success" if ok else ("warning" if "bulunamadı" in message.lower() else "error")
        self.on_toast(message, level)
        self.refresh()

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
        # Check if sales view is visible
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
                hint="Ürün eklemek için yukardaki kutuya barkod okutun veya arama yapın.",
                icon="🛒",
            )
            self.clear_cart_btn.configure(state="disabled")
        else:
            self.clear_cart_btn.configure(state="normal")

            for item in self.sale_service.cart_items:
                # Create delete action button widget for column index 4
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

                row = self.cart_table.add_row(
                    values=[
                        item.product_name,
                        format_currency(item.unit_price),
                        f"{item.quantity} adet (çift tıkla)",
                        format_currency(item.subtotal),
                        "",
                    ],
                    row_id=item.product_id,
                    text_colors=[None, None, stock_color, None, None],
                    custom_widgets={4: delete_btn},
                )
                self._bind_row_events(row, item.product_id)

        self.total_label.configure(
            text=f"Genel Toplam: {format_currency(self.sale_service.total_amount)}"
        )

    def _bind_row_events(self, row, product_id: int) -> None:
        row.bind("<Button-1>", lambda e, pid=product_id: self.cart_table.set_selected_row(pid))
        for child in row.winfo_children():
            # Don't override button commands
            if not isinstance(child, ctk.CTkButton):
                child.bind("<Button-1>", lambda e, pid=product_id: self.cart_table.set_selected_row(pid))

        # Double click on quantity column (column index 2) to edit
        if len(row.winfo_children()) > 2:
            qty_widget = row.winfo_children()[2]
            qty_widget.bind(
                "<Double-Button-1>",
                lambda e, pid=product_id: self._edit_quantity(pid),
            )

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

    def _complete_sale(self) -> None:
        ok, message = self.sale_service.complete_sale()
        level = "success" if ok else "error"
        self.on_toast(message, level)
        if ok:
            self.on_sale_complete()
        self.refresh()
        self.barcode_entry.clear_and_focus()

