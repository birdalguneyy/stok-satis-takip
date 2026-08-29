from typing import Callable, Dict, Optional

import customtkinter as ctk

from app.services.product_service import ProductService
from app.ui.components.data_table import DataTable
from app.ui.theme import (
    ACCENT,
    ERROR,
    FONT_BODY,
    FONT_HEADING,
    FONT_SMALL,
    STOCK_CRITICAL,
    STOCK_NORMAL,
    STOCK_OUT,
    WARNING,
)
from app.utils.dialogs import confirm
from app.utils.formatters import format_currency
from app.utils.validators import validate_product_fields

COLUMN_MAP = {
    "Ürün Adı": "name",
    "Kategori": "category",
    "Barkod": "barcode",
    "Alış": "purchase_price",
    "Satış": "sale_price",
    "Stok": "stock_quantity",
}


class ProductsView(ctk.CTkFrame):
    def __init__(
        self,
        master,
        product_service: ProductService,
        on_toast: Callable[[str, str], None],
        **kwargs,
    ) -> None:
        super().__init__(master, fg_color="transparent", **kwargs)
        self.product_service = product_service
        self.on_toast = on_toast
        self._order_by = "name"
        self._ascending = True
        self._selected_id: Optional[int] = None
        self.entries: Dict[str, ctk.CTkEntry | ctk.CTkComboBox] = {}

        header_row = ctk.CTkFrame(self, fg_color="transparent")
        header_row.pack(fill="x", padx=8, pady=(0, 12))

        ctk.CTkLabel(header_row, text="Stok Yönetimi", font=FONT_HEADING).pack(side="left")

        self.search_var = ctk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.refresh())
        
        ctk.CTkEntry(
            header_row,
            placeholder_text="Ürün, barkod veya kategori ara...",
            textvariable=self.search_var,
            width=240,
        ).pack(side="right")

        ctk.CTkButton(
            header_row,
            text="✨ Demo Veri Yükle",
            font=FONT_SMALL,
            fg_color=("gray80", "gray30"),
            hover_color=("gray70", "gray35"),
            text_color=("gray10", "gray90"),
            command=self._load_demo_data,
        ).pack(side="right", padx=(0, 8))

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=8)
        body.grid_columnconfigure(0, weight=3)
        body.grid_columnconfigure(1, weight=2)
        body.grid_rowconfigure(0, weight=1)

        self.table = DataTable(
            body,
            columns=list(COLUMN_MAP.keys()),
            column_weights=[3, 2, 2, 2, 2, 2],
            on_sort=self._on_sort,
            empty_message="Henüz ürün eklenmemiş.",
            empty_hint="Sağdaki formdan yeni ürün ekleyebilir veya demo verileri yükleyebilirsiniz.",
            empty_icon="📦",
            empty_action_text="Örnek Verileri Yükle",
            on_empty_action=self._load_demo_data,
        )
        self.table.grid(row=0, column=0, sticky="nsew", padx=(0, 8))

        form = ctk.CTkFrame(body, corner_radius=12)
        form.grid(row=0, column=1, sticky="nsew")

        self.form_title = ctk.CTkLabel(form, text="Yeni Ürün", font=FONT_HEADING)
        self.form_title.pack(anchor="w", padx=16, pady=(16, 4))

        self.form_error_label = ctk.CTkLabel(
            form, text="", font=FONT_SMALL, text_color=ERROR, anchor="w"
        )
        self.form_error_label.pack(anchor="w", padx=16, pady=(0, 8))

        self.name_var = ctk.StringVar()
        self.category_var = ctk.StringVar()
        self.barcode_var = ctk.StringVar()
        self.purchase_var = ctk.StringVar(value="0")
        self.sale_var = ctk.StringVar(value="0")
        self.stock_var = ctk.StringVar(value="0")
        self.critical_var = ctk.StringVar(value="5")

        self._add_field(form, "name", "Ürün Adı *", self.name_var)
        
        ctk.CTkLabel(form, text="Kategori *", font=FONT_SMALL, text_color="gray").pack(anchor="w", padx=16)
        self.category_combo = ctk.CTkComboBox(
            form,
            values=self._category_names(),
            variable=self.category_var,
        )
        self.category_combo.pack(fill="x", padx=16, pady=(2, 6))

        # Barkod alanı
        self._add_field(form, "barcode", "Barkod (Telefon / Okuyucu ile Okutun) *", self.barcode_var)

        self._add_field(form, "purchase_price", "Alış Fiyatı (₺)", self.purchase_var)
        self._add_field(form, "sale_price", "Satış Fiyatı (₺)", self.sale_var)
        self._add_field(form, "stock_quantity", "Stok Miktarı", self.stock_var)
        self._add_field(form, "critical_stock_level", "Kritik Stok Eşiği", self.critical_var)

        btn_row = ctk.CTkFrame(form, fg_color="transparent")
        btn_row.pack(fill="x", padx=16, pady=(8, 16))

        ctk.CTkButton(
            btn_row, text="Kaydet", fg_color=ACCENT, command=self._save
        ).pack(side="left", expand=True, fill="x", padx=(0, 4))
        ctk.CTkButton(
            btn_row, text="Temizle", fg_color=("gray75", "gray35"), command=self._clear_form
        ).pack(side="left", expand=True, fill="x", padx=4)
        ctk.CTkButton(
            btn_row,
            text="Sil",
            fg_color="#C62828",
            hover_color="#B71C1C",
            command=self._delete,
        ).pack(side="left", expand=True, fill="x", padx=(4, 0))

    def _add_field(self, parent, key: str, label: str, var: ctk.StringVar) -> None:
        ctk.CTkLabel(parent, text=label, font=FONT_SMALL, text_color="gray").pack(anchor="w", padx=16)
        entry = ctk.CTkEntry(parent, textvariable=var)
        entry.pack(fill="x", padx=16, pady=(2, 6))
        self.entries[key] = entry
        var.trace_add("write", lambda *_: self._reset_field_style(key))

    def _reset_field_style(self, key: str) -> None:
        if key in self.entries:
            widget = self.entries[key]
            if isinstance(widget, ctk.CTkEntry):
                widget.configure(border_color=("gray70", "gray30"))
            elif isinstance(widget, ctk.CTkComboBox):
                widget.configure(border_color=("gray70", "gray30"))

    def _category_names(self) -> list[str]:
        categories = self.product_service.get_categories()
        names = [c.name for c in categories]
        return names or ["Genel"]

    def _on_sort(self, column: str) -> None:
        self._order_by = COLUMN_MAP.get(column.split(" ")[0], "name")
        if self.table.sort_column == column.split(" ")[0]:
            self._ascending = self.table.sort_ascending
        self.refresh()

    def _on_row_click(self, product_id: int) -> None:
        self._load_product(product_id)
        self.table.set_selected_row(product_id)

    def _load_product(self, product_id: int) -> None:
        products = self.product_service.list_products()
        item = next((p for p in products if p.id == product_id), None)
        if not item:
            return
        self._selected_id = item.id
        self.form_title.configure(text="Ürün Düzenle")
        self.form_error_label.configure(text="")
        self.name_var.set(item.name)
        self.category_var.set(item.category_name or "")
        self.barcode_var.set(item.barcode)
        self.purchase_var.set(str(item.purchase_price))
        self.sale_var.set(str(item.sale_price))
        self.stock_var.set(str(item.stock_quantity))
        self.critical_var.set(str(item.critical_stock_level))

    def _parse_float(self, value: str) -> float:
        return float(value.replace(",", ".").strip())

    def _parse_int(self, value: str) -> int:
        return int(value.strip())

    def _save(self) -> None:
        # Reset previous field styles
        for key in self.entries:
            self._reset_field_style(key)
        self.form_error_label.configure(text="")

        try:
            p_price = self._parse_float(self.purchase_var.get())
        except ValueError:
            self._highlight_invalid("purchase_price", "Geçersiz alış fiyatı")
            return

        try:
            s_price = self._parse_float(self.sale_var.get())
        except ValueError:
            self._highlight_invalid("sale_price", "Geçersiz satış fiyatı")
            return

        try:
            stock_qty = self._parse_int(self.stock_var.get())
        except ValueError:
            self._highlight_invalid("stock_quantity", "Geçersiz stok miktarı")
            return

        try:
            crit_qty = self._parse_int(self.critical_var.get())
        except ValueError:
            self._highlight_invalid("critical_stock_level", "Geçersiz kritik stok eşiği")
            return

        val_res = validate_product_fields(
            name=self.name_var.get(),
            category_name=self.category_var.get(),
            purchase_price=p_price,
            sale_price=s_price,
            stock_quantity=stock_qty,
            barcode=self.barcode_var.get(),
        )

        if not val_res.is_valid:
            for field_key in val_res.invalid_fields:
                self._highlight_invalid(field_key, val_res.message or "Hatalı alan")
            return

        # Warning if sale price is less than purchase price
        if s_price < p_price:
            if not confirm(
                "Fiyat Uyarısı",
                f"Satış fiyatı ({format_currency(s_price)}) alış fiyatından ({format_currency(p_price)}) düşük. Devam etmek istiyor musunuz?",
            ):
                return

        ok, message, _ = self.product_service.save_product(
            name=self.name_var.get(),
            category_name=self.category_var.get(),
            purchase_price=p_price,
            sale_price=s_price,
            stock_quantity=stock_qty,
            barcode=self.barcode_var.get(),
            critical_stock_level=crit_qty,
            product_id=self._selected_id,
        )

        if not ok:
            self.form_error_label.configure(text=f"⚠️ {message}")
            if "barkod" in message.lower():
                self._highlight_invalid("barcode", message)
            self.on_toast(message, "error")
            return

        self.on_toast(message, "success")
        self._clear_form()
        self._refresh_categories()

    def _highlight_invalid(self, key: str, message: str) -> None:
        self.form_error_label.configure(text=f"⚠️ {message}")
        if key in self.entries:
            widget = self.entries[key]
            if isinstance(widget, (ctk.CTkEntry, ctk.CTkComboBox)):
                widget.configure(border_color=ERROR)

    def _load_demo_data(self) -> None:
        ok, message = self.product_service.seed_demo_data(force=True)
        self.on_toast(message, "success" if ok else "info")
        self._refresh_categories()
        self.refresh()

    def _delete(self) -> None:
        if not self._selected_id:
            self.on_toast("Silinecek ürün seçin", "warning")
            return
        if not confirm("Ürün Sil", "Seçili ürünü silmek istediğinize emin misiniz?"):
            return
        ok, message = self.product_service.delete_product(self._selected_id)
        self.on_toast(message, "success" if ok else "error")
        if ok:
            self._clear_form()
            self.refresh()

    def _clear_form(self) -> None:
        self._selected_id = None
        self.form_title.configure(text="Yeni Ürün")
        self.form_error_label.configure(text="")
        for key in self.entries:
            self._reset_field_style(key)
        self.name_var.set("")
        self.category_var.set("")
        self.barcode_var.set("")
        self.purchase_var.set("0")
        self.sale_var.set("0")
        self.stock_var.set("0")
        self.critical_var.set("5")
        self.table.set_selected_row(None)

    def _refresh_categories(self) -> None:
        self.category_combo.configure(values=self._category_names())

    def _stock_info(self, product) -> tuple[str, str]:
        if product.stock_quantity == 0:
            return "❌ Tükendi", STOCK_OUT
        if product.is_critical:
            return f"⚠️ {product.stock_quantity} (Kritik)", STOCK_CRITICAL
        return f"{product.stock_quantity} adet", STOCK_NORMAL

    def refresh(self) -> None:
        products = self.product_service.list_products(
            search=self.search_var.get(),
            order_by=self._order_by,
            ascending=self._ascending,
        )
        self.table.clear_rows()

        if not products:
            if self.search_var.get().strip():
                self.table.show_empty(
                    message="Arama sonucu bulunamadı.",
                    hint="Farklı bir arama terimi deneyin.",
                    icon="🔍",
                )
            else:
                self.table.show_empty(
                    message="Henüz ürün eklenmemiş.",
                    hint="Sağdaki formdan ürün ekleyebilir veya demo verileri yükleyebilirsiniz.",
                    icon="📦",
                    action_text="Örnek Verileri Yükle",
                    on_action=self._load_demo_data,
                )
            return

        for product in products:
            stock_text, stock_color = self._stock_info(product)
            row = self.table.add_row(
                [
                    product.name,
                    product.category_name or "",
                    product.barcode,
                    format_currency(product.purchase_price),
                    format_currency(product.sale_price),
                    stock_text,
                ],
                row_id=product.id,
                text_colors=[None, None, None, None, None, stock_color],
            )
            row.bind("<Button-1>", lambda e, pid=product.id: self._on_row_click(pid))
            for child in row.winfo_children():
                child.bind("<Button-1>", lambda e, pid=product.id: self._on_row_click(pid))

        if self._selected_id:
            self.table.set_selected_row(self._selected_id)

