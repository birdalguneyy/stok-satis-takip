from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Set

import customtkinter as ctk

from app.services.sale_service import SaleService
from app.ui.theme import (
    ACCENT,
    ACCENT_HOVER,
    ERROR,
    FONT_BODY,
    FONT_HEADING,
    FONT_SMALL,
    FONT_TITLE,
)
from app.utils.dialogs import confirm
from app.utils.formatters import format_currency


class HistoryView(ctk.CTkFrame):
    def __init__(
        self,
        master,
        sale_service: SaleService,
        on_toast: Callable[[str, str], None],
        on_stock_changed: Optional[Callable[[], None]] = None,
        **kwargs,
    ) -> None:
        super().__init__(master, fg_color="transparent", **kwargs)
        self.sale_service = sale_service
        self.on_toast = on_toast
        self.on_stock_changed = on_stock_changed

        self._all_sales: List[Dict[str, Any]] = []
        self._filtered_sales: List[Dict[str, Any]] = []
        self._selected_ids: Set[int] = set()
        self._start_date: Optional[str] = None
        self._end_date: Optional[str] = None
        self._date_preset: str = "all"
        self._date_buttons: Dict[str, ctk.CTkButton] = {}

        # Build UI
        self._build_header()
        self._build_date_filter_bar()
        self._build_action_bar()
        self._build_sales_container()

    def _build_header(self) -> None:
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=8, pady=(0, 8))

        ctk.CTkLabel(
            header,
            text="📜 Satış Geçmişi & Yönetimi",
            font=FONT_HEADING,
        ).pack(side="left")

        # Refresh button
        ctk.CTkButton(
            header,
            text="🔄 Yenile",
            font=FONT_SMALL,
            width=80,
            height=32,
            fg_color=("gray80", "gray30"),
            hover_color=("gray70", "gray35"),
            text_color=("gray10", "gray90"),
            command=self.refresh,
        ).pack(side="right", padx=(8, 0))

        # Search box
        self.search_var = ctk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._apply_filter())
        search_entry = ctk.CTkEntry(
            header,
            placeholder_text="🔍 Müşteri, not veya ürün ara...",
            textvariable=self.search_var,
            width=220,
            height=32,
            font=FONT_SMALL,
        )
        search_entry.pack(side="right", padx=(8, 0))

        # Grouping combobox
        ctk.CTkLabel(
            header,
            text="Grupla:",
            font=FONT_SMALL,
            text_color=("gray40", "gray60"),
        ).pack(side="right", padx=(12, 4))

        self.group_var = ctk.StringVar(value="Gruplama Yok")
        self.group_menu = ctk.CTkOptionMenu(
            header,
            values=["Gruplama Yok", "Müşteriye Göre", "Tarihe Göre", "Kanala Göre"],
            variable=self.group_var,
            command=lambda _: self._render_sales(),
            width=160,
            height=32,
            font=FONT_SMALL,
        )
        self.group_menu.pack(side="right")

    def _build_date_filter_bar(self) -> None:
        filter_card = ctk.CTkFrame(self, fg_color=("gray90", "gray18"), corner_radius=8)
        filter_card.pack(fill="x", padx=8, pady=(0, 8))

        # Top row: Presets
        presets_row = ctk.CTkFrame(filter_card, fg_color="transparent")
        presets_row.pack(fill="x", padx=8, pady=(8, 4))

        ctk.CTkLabel(
            presets_row,
            text="📅 Hızlı Tarih:",
            font=FONT_SMALL,
            text_color=("gray30", "gray70"),
        ).pack(side="left", padx=(0, 6))

        preset_defs = [
            ("all", "🌐 Tümü"),
            ("today", "⚡ Bugün"),
            ("yesterday", "⏮️ Dün"),
            ("this_week", "📅 Bu Hafta"),
            ("last_week", "⏪ Geçen Hafta"),
            ("this_month", "🗓️ Bu Ay"),
            ("last_month", "⏮️ Geçen Ay"),
            ("last_30", "📊 Son 30 Gün"),
        ]

        for p_key, p_label in preset_defs:
            is_active = (p_key == self._date_preset)
            btn = ctk.CTkButton(
                presets_row,
                text=p_label,
                font=FONT_SMALL,
                height=26,
                width=75 if len(p_label) < 9 else 95,
                fg_color=ACCENT if is_active else ("gray85", "gray28"),
                hover_color=ACCENT_HOVER if is_active else ("gray75", "gray35"),
                text_color="white" if is_active else ("gray10", "gray90"),
                corner_radius=6,
                command=lambda k=p_key: self._set_date_preset(k),
            )
            btn.pack(side="left", padx=2)
            self._date_buttons[p_key] = btn

        # Active date range badge
        self._date_badge = ctk.CTkLabel(
            presets_row,
            text="Tüm Zamanlar",
            font=FONT_SMALL,
            text_color=ACCENT,
        )
        self._date_badge.pack(side="right", padx=(4, 0))

        # Bottom row: Custom date range inputs
        custom_row = ctk.CTkFrame(filter_card, fg_color="transparent")
        custom_row.pack(fill="x", padx=8, pady=(0, 6))

        ctk.CTkLabel(
            custom_row,
            text="Özel Aralık:",
            font=FONT_SMALL,
            text_color=("gray40", "gray60"),
        ).pack(side="left", padx=(0, 4))

        self._start_entry = ctk.CTkEntry(
            custom_row,
            placeholder_text="YYYY-AA-GG",
            width=100,
            height=26,
            font=FONT_SMALL,
        )
        self._start_entry.pack(side="left", padx=2)

        ctk.CTkLabel(custom_row, text="-", font=FONT_SMALL).pack(side="left", padx=2)

        self._end_entry = ctk.CTkEntry(
            custom_row,
            placeholder_text="YYYY-AA-GG",
            width=100,
            height=26,
            font=FONT_SMALL,
        )
        self._end_entry.pack(side="left", padx=2)

        ctk.CTkButton(
            custom_row,
            text="Filtrele",
            font=FONT_SMALL,
            width=65,
            height=26,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            command=self._apply_custom_date,
        ).pack(side="left", padx=(6, 0))

    def _set_date_preset(self, preset: str) -> None:
        self._date_preset = preset
        today = datetime.now().date()

        if preset == "today":
            self._start_date = today.strftime("%Y-%m-%d")
            self._end_date = today.strftime("%Y-%m-%d")
            badge_text = f"Bugün ({today.strftime('%d.%m.%Y')})"
        elif preset == "yesterday":
            yest = today - timedelta(days=1)
            self._start_date = yest.strftime("%Y-%m-%d")
            self._end_date = yest.strftime("%Y-%m-%d")
            badge_text = f"Dün ({yest.strftime('%d.%m.%Y')})"
        elif preset == "this_week":
            start = today - timedelta(days=today.weekday())
            end = start + timedelta(days=6)
            self._start_date = start.strftime("%Y-%m-%d")
            self._end_date = end.strftime("%Y-%m-%d")
            badge_text = f"Bu Hafta ({start.strftime('%d.%m')} - {end.strftime('%d.%m.%Y')})"
        elif preset == "last_week":
            start = today - timedelta(days=today.weekday() + 7)
            end = start + timedelta(days=6)
            self._start_date = start.strftime("%Y-%m-%d")
            self._end_date = end.strftime("%Y-%m-%d")
            badge_text = f"Geçen Hafta ({start.strftime('%d.%m')} - {end.strftime('%d.%m.%Y')})"
        elif preset == "this_month":
            start = today.replace(day=1)
            if today.month == 12:
                next_month = today.replace(year=today.year + 1, month=1, day=1)
            else:
                next_month = today.replace(month=today.month + 1, day=1)
            end = next_month - timedelta(days=1)
            self._start_date = start.strftime("%Y-%m-%d")
            self._end_date = end.strftime("%Y-%m-%d")
            badge_text = f"Bu Ay ({start.strftime('%d.%m')} - {end.strftime('%d.%m.%Y')})"
        elif preset == "last_month":
            first_this_month = today.replace(day=1)
            end = first_this_month - timedelta(days=1)
            start = end.replace(day=1)
            self._start_date = start.strftime("%Y-%m-%d")
            self._end_date = end.strftime("%Y-%m-%d")
            badge_text = f"Geçen Ay ({start.strftime('%d.%m')} - {end.strftime('%d.%m.%Y')})"
        elif preset == "last_30":
            start = today - timedelta(days=29)
            self._start_date = start.strftime("%Y-%m-%d")
            self._end_date = today.strftime("%Y-%m-%d")
            badge_text = f"Son 30 Gün ({start.strftime('%d.%m')} - {today.strftime('%d.%m.%Y')})"
        else:  # "all"
            self._start_date = None
            self._end_date = None
            badge_text = "Tüm Zamanlar"

        for p, btn in self._date_buttons.items():
            if p == preset:
                btn.configure(fg_color=ACCENT, text_color="white")
            else:
                btn.configure(fg_color=("gray85", "gray28"), text_color=("gray10", "gray90"))

        if hasattr(self, "_date_badge"):
            self._date_badge.configure(text=badge_text)

        if hasattr(self, "_start_entry"):
            self._start_entry.delete(0, "end")
            if self._start_date:
                self._start_entry.insert(0, self._start_date)
        if hasattr(self, "_end_entry"):
            self._end_entry.delete(0, "end")
            if self._end_date:
                self._end_entry.insert(0, self._end_date)

        self.refresh()

    def _apply_custom_date(self) -> None:
        start_val = self._start_entry.get().strip()
        end_val = self._end_entry.get().strip()
        if not start_val or not end_val:
            self.on_toast("Lütfen başlangıç ve bitiş tarihlerini girin (YYYY-AA-GG)", "warning")
            return
        self._start_date = start_val
        self._end_date = end_val
        self._date_preset = "custom"
        for btn in self._date_buttons.values():
            btn.configure(fg_color=("gray85", "gray28"), text_color=("gray10", "gray90"))
        self._date_badge.configure(text=f"Özel: {start_val} - {end_val}")
        self.refresh()

    def _build_action_bar(self) -> None:
        bar = ctk.CTkFrame(self, fg_color=("gray85", "gray22"), corner_radius=8, height=44)
        bar.pack(fill="x", padx=8, pady=(0, 10))

        # Select All Checkbox
        self.select_all_var = ctk.BooleanVar(value=False)
        self.select_all_cb = ctk.CTkCheckBox(
            bar,
            text="Tümünü Seç",
            variable=self.select_all_var,
            command=self._on_toggle_select_all,
            font=FONT_SMALL,
            width=100,
            height=24,
        )
        self.select_all_cb.pack(side="left", padx=(12, 8), pady=8)

        # Selected counter
        self.selected_label = ctk.CTkLabel(
            bar,
            text="0 satış seçildi",
            font=FONT_SMALL,
            text_color=("gray40", "gray60"),
        )
        self.selected_label.pack(side="left", padx=4, pady=8)

        # Bulk Delete Button
        self.delete_btn = ctk.CTkButton(
            bar,
            text="🗑️ Seçilenleri Sil (0)",
            font=FONT_SMALL,
            fg_color=ERROR,
            hover_color="#991B1B",
            height=30,
            state="disabled",
            command=self._delete_selected,
        )
        self.delete_btn.pack(side="right", padx=12, pady=7)

    def _build_sales_container(self) -> None:
        self.scroll_frame = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.scroll_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    def on_show(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        try:
            self._all_sales = self.sale_service.get_sales_history(
                start_date=self._start_date, end_date=self._end_date
            )
        except Exception as e:
            self.on_toast(f"Satış geçmişi yüklenemedi: {e}", "error")
            self._all_sales = []

        # Keep selected IDs that still exist
        existing_ids = {s["id"] for s in self._all_sales}
        self._selected_ids = self._selected_ids.intersection(existing_ids)

        self._apply_filter()

    def _apply_filter(self) -> None:
        q = self.search_var.get().strip().lower()
        if not q:
            self._filtered_sales = list(self._all_sales)
        else:
            filtered = []
            for s in self._all_sales:
                cust = str(s.get("customer_name") or "").lower()
                note = str(s.get("note") or "").lower()
                sid = str(s.get("id") or "")
                channel = str(s.get("channel") or "").lower()
                # Check items names
                items_match = any(
                    q in str(it.get("product_name", "")).lower() or q in str(it.get("barcode", "")).lower()
                    for it in s.get("items", [])
                )
                if q in cust or q in note or q in sid or q in channel or items_match:
                    filtered.append(s)
            self._filtered_sales = filtered

        self._render_sales()
        self._update_action_bar()

    def _update_action_bar(self) -> None:
        count = len(self._selected_ids)
        total_visible = len(self._filtered_sales)

        self.selected_label.configure(text=f"{count} / {total_visible} satış seçildi")

        if count > 0:
            self.delete_btn.configure(
                state="normal",
                text=f"🗑️ Seçilenleri Sil ({count})",
            )
        else:
            self.delete_btn.configure(
                state="disabled",
                text="🗑️ Seçilenleri Sil (0)",
            )

        # Update select all checkbox state without triggering event
        all_selected = total_visible > 0 and all(s["id"] in self._selected_ids for s in self._filtered_sales)
        self.select_all_var.set(all_selected)

    def _on_toggle_select_all(self) -> None:
        if self.select_all_var.get():
            for s in self._filtered_sales:
                self._selected_ids.add(s["id"])
        else:
            for s in self._filtered_sales:
                self._selected_ids.discard(s["id"])

        self._render_sales()
        self._update_action_bar()

    def _render_sales(self) -> None:
        # Clear existing widgets
        for child in self.scroll_frame.winfo_children():
            child.destroy()

        if not self._filtered_sales:
            empty_box = ctk.CTkFrame(self.scroll_frame, fg_color=("gray90", "gray18"), corner_radius=10)
            empty_box.pack(fill="both", expand=True, padx=20, pady=40)
            ctk.CTkLabel(
                empty_box,
                text="🛒 Satış Kaydı Bulunamadı",
                font=FONT_HEADING,
                text_color=("gray40", "gray60"),
            ).pack(pady=(30, 8))
            ctk.CTkLabel(
                empty_box,
                text="Arama kriterine uygun veya yapılmış bir satış kaydı yok.",
                font=FONT_BODY,
                text_color=("gray50", "gray50"),
            ).pack(pady=(0, 30))
            return

        mode = self.group_var.get()
        if mode == "Müşteriye Göre":
            self._render_grouped_by_customer()
        elif mode == "Tarihe Göre":
            self._render_grouped_by_date()
        elif mode == "Kanala Göre":
            self._render_grouped_by_channel()
        else:
            self._render_flat_list()

    def _render_flat_list(self) -> None:
        for sale in self._filtered_sales:
            self._create_sale_card(self.scroll_frame, sale)

    def _render_grouped_by_customer(self) -> None:
        groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for s in self._filtered_sales:
            cust = s.get("customer_name")
            key = cust.strip() if cust and str(cust).strip() else "İsimsiz / Genel Müşteriler"
            groups[key].append(s)

        # Sort groups by count descending
        sorted_keys = sorted(groups.keys(), key=lambda k: len(groups[k]), reverse=True)
        for group_name in sorted_keys:
            sales = groups[group_name]
            self._create_group_section(f"👤 {group_name}", sales)

    def _render_grouped_by_date(self) -> None:
        groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for s in self._filtered_sales:
            sold_at = str(s.get("sold_at") or "")
            date_part = sold_at.split(" ")[0] if " " in sold_at else (sold_at[:10] if sold_at else "Bilinmeyen Tarih")
            groups[date_part].append(s)

        sorted_keys = sorted(groups.keys(), reverse=True)
        for date_str in sorted_keys:
            sales = groups[date_str]
            self._create_group_section(f"📅 {date_str}", sales)

    def _render_grouped_by_channel(self) -> None:
        groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for s in self._filtered_sales:
            ch = str(s.get("channel") or "magaza").lower()
            key = "🌐 İnternet Satışları" if ch == "internet" else "🏬 Mağaza Satışları"
            groups[key].append(s)

        for channel_name in sorted(groups.keys()):
            sales = groups[channel_name]
            self._create_group_section(channel_name, sales)

    def _create_group_section(self, group_title: str, sales: List[Dict[str, Any]]) -> None:
        total_rev = sum(float(s.get("total_amount") or 0) for s in sales)
        sale_ids = [s["id"] for s in sales]
        all_in_group_selected = all(sid in self._selected_ids for sid in sale_ids)

        group_frame = ctk.CTkFrame(self.scroll_frame, fg_color=("gray85", "gray20"), corner_radius=10)
        group_frame.pack(fill="x", pady=6, padx=4)

        header_frame = ctk.CTkFrame(group_frame, fg_color="transparent")
        header_frame.pack(fill="x", padx=12, pady=(10, 6))

        title_text = f"{group_title}  •  {len(sales)} Satış  •  Toplam: {format_currency(total_rev)}"
        ctk.CTkLabel(
            header_frame,
            text=title_text,
            font=(FONT_BODY[0], 13, "bold"),
            anchor="w",
        ).pack(side="left")

        # Group Select Button
        btn_text = "Seçimi Kaldır" if all_in_group_selected else "Grubu Seç"
        ctk.CTkButton(
            header_frame,
            text=btn_text,
            font=FONT_SMALL,
            width=90,
            height=26,
            fg_color=("gray75", "gray32") if all_in_group_selected else ACCENT,
            hover_color=("gray65", "gray40") if all_in_group_selected else ACCENT_HOVER,
            command=lambda sids=sale_ids, deselect=all_in_group_selected: self._toggle_group_selection(sids, deselect),
        ).pack(side="right")

        content_frame = ctk.CTkFrame(group_frame, fg_color="transparent")
        content_frame.pack(fill="x", padx=6, pady=(0, 6))

        for s in sales:
            self._create_sale_card(content_frame, s)

    def _toggle_group_selection(self, sale_ids: List[int], deselect: bool) -> None:
        if deselect:
            for sid in sale_ids:
                self._selected_ids.discard(sid)
        else:
            for sid in sale_ids:
                self._selected_ids.add(sid)
        self._render_sales()
        self._update_action_bar()

    def _create_sale_card(self, parent, sale: Dict[str, Any]) -> None:
        sid = sale["id"]
        is_selected = sid in self._selected_ids

        card = ctk.CTkFrame(
            parent,
            fg_color=("#E0F2FE", "#1E293B") if is_selected else ("gray95", "gray16"),
            border_width=1 if is_selected else 0,
            border_color=ACCENT if is_selected else "transparent",
            corner_radius=8,
        )
        card.pack(fill="x", pady=3, padx=2)

        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=8)

        # Checkbox
        cb_var = ctk.BooleanVar(value=is_selected)

        def on_toggle():
            if cb_var.get():
                self._selected_ids.add(sid)
            else:
                self._selected_ids.discard(sid)
            self._update_action_bar()
            card.configure(
                fg_color=("#E0F2FE", "#1E293B") if cb_var.get() else ("gray95", "gray16"),
                border_width=1 if cb_var.get() else 0,
                border_color=ACCENT if cb_var.get() else "transparent",
            )

        cb = ctk.CTkCheckBox(
            row,
            text="",
            variable=cb_var,
            command=on_toggle,
            width=24,
            height=24,
        )
        cb.pack(side="left", padx=(0, 8))

        # Sale ID badge
        ctk.CTkLabel(
            row,
            text=f"#{sid}",
            font=(FONT_SMALL[0], 11, "bold"),
            text_color=ACCENT,
            width=45,
            anchor="w",
        ).pack(side="left")

        # Date
        sold_at = str(sale.get("sold_at") or "")
        ctk.CTkLabel(
            row,
            text=sold_at[:16] if len(sold_at) >= 16 else sold_at,
            font=FONT_SMALL,
            text_color=("gray40", "gray60"),
            width=115,
            anchor="w",
        ).pack(side="left", padx=4)

        # Channel badge
        is_internet = str(sale.get("channel") or "").lower() == "internet"
        ch_text = "🌐 İnternet" if is_internet else "🏬 Mağaza"
        ch_color = "#059669" if is_internet else "#0284C7"
        ctk.CTkLabel(
            row,
            text=ch_text,
            font=(FONT_SMALL[0], 10, "bold"),
            text_color=ch_color,
            width=75,
            anchor="w",
        ).pack(side="left", padx=4)

        # Customer name
        cust_name = sale.get("customer_name")
        cust_display = f"👤 {cust_name}" if cust_name and str(cust_name).strip() else "👤 İsimsiz Müşteri"
        cust_color = ("gray15", "gray90") if cust_name and str(cust_name).strip() else ("gray50", "gray50")
        ctk.CTkLabel(
            row,
            text=cust_display,
            font=(FONT_BODY[0], 12, "bold" if cust_name else "normal"),
            text_color=cust_color,
            width=160,
            anchor="w",
        ).pack(side="left", padx=6)

        # Items preview
        items = sale.get("items", [])
        if items:
            items_summary = ", ".join(f"{it.get('product_name')} ({it.get('quantity')}x)" for it in items[:2])
            if len(items) > 2:
                items_summary += f" +{len(items) - 2} ürün"
        else:
            items_summary = f"{sale.get('item_count', 0)} adet ürün"

        ctk.CTkLabel(
            row,
            text=items_summary,
            font=FONT_SMALL,
            text_color=("gray40", "gray60"),
            anchor="w",
        ).pack(side="left", fill="x", expand=True, padx=6)

        # Total Amount
        total_val = float(sale.get("total_amount") or 0)
        ctk.CTkLabel(
            row,
            text=format_currency(total_val),
            font=(FONT_BODY[0], 13, "bold"),
            text_color=ACCENT,
            width=110,
            anchor="e",
        ).pack(side="right", padx=(8, 12))

        # Single Delete Button
        ctk.CTkButton(
            row,
            text="🗑️",
            font=FONT_SMALL,
            width=32,
            height=28,
            fg_color="transparent",
            hover_color=("gray85", "gray25"),
            text_color=ERROR,
            command=lambda s_id=sid: self._delete_single(s_id),
        ).pack(side="right")

    def _delete_single(self, sale_id: int) -> None:
        if not confirm(
            "Satışı Sil",
            f"#{sale_id} numaralı satışı silmek istiyor musunuz?\n\nBu işlem satılan ürünlerin stoklarını depoya geri yükleyecektir.",
        ):
            return

        ok, msg = self.sale_service.delete_sale(sale_id, restore_stock=True)
        level = "success" if ok else "error"
        self.on_toast(msg, level)
        if ok:
            self._selected_ids.discard(sale_id)
            if self.on_stock_changed:
                self.on_stock_changed()
            self.refresh()

    def _delete_selected(self) -> None:
        count = len(self._selected_ids)
        if count == 0:
            return

        if not confirm(
            "Toplu Satış Silme",
            f"Seçili olan {count} adet satışı silmek istiyor musunuz?\n\nSatılan ürünlerin stokları depoya otomatik olarak geri yüklenecektir.",
        ):
            return

        sale_ids = list(self._selected_ids)
        ok, msg, deleted_count = self.sale_service.delete_sales_bulk(sale_ids, restore_stock=True)
        level = "success" if ok else "error"
        self.on_toast(msg, level)
        if ok:
            self._selected_ids.clear()
            if self.on_stock_changed:
                self.on_stock_changed()
            self.refresh()
