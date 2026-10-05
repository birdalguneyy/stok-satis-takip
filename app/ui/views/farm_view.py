from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

import customtkinter as ctk

from app.services.farm_service import FarmService
from app.ui.components.data_table import DataTable
from app.ui.components.stat_card import StatCard
from app.ui.theme import (
    ACCENT,
    ACCENT_HOVER,
    ERROR,
    FONT_BODY,
    FONT_HEADING,
    FONT_SMALL,
    FONT_TITLE,
    FONT_TOTAL,
    SUCCESS,
    WARNING,
)
from app.utils.formatters import format_currency


class FarmView(ctk.CTkFrame):
    """Çiftlik Yönetim Modülü:
    1. 🥚 Yumurta Satışı (Koli sayısı, birim fiyat, müşteri hafızası)
    2. 🌾 Yem Alımı (Tek sayfa: üstte veri girişi, altta tarihli tonaj dökümü - 1 çuval = 50 kg)
    3. 📊 Gelişmiş Filtreleme & Müşteri Analizi (Müşteri bazlı filtre, Pazartesi-Pazartesi haftalık, aylık, 3 aylık)
    """

    def __init__(
        self,
        master,
        on_toast: Callable[[str, str], None],
        on_back_to_shop: Optional[Callable[[], None]] = None,
        **kwargs,
    ) -> None:
        super().__init__(master, fg_color="transparent", **kwargs)
        self.on_toast = on_toast
        self.on_back_to_shop = on_back_to_shop
        self.farm_service = FarmService()

        self._current_tab = "egg_sales"
        self._tab_buttons: Dict[str, ctk.CTkButton] = {}
        self._tab_frames: Dict[str, ctk.CTkFrame] = {}

        # Filtreleme durumu
        self._filter_period = "weekly"  # Varsayılan: Pazartesiden Pazartesiye
        self._filter_customer = ""
        self._period_buttons: Dict[str, ctk.CTkButton] = {}

        self._build_header()
        self._build_tabs()

    def _build_header(self) -> None:
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=8, pady=(0, 10))

        # Sol taraf: Başlık
        title_box = ctk.CTkFrame(header, fg_color="transparent")
        title_box.pack(side="left")

        ctk.CTkLabel(
            title_box,
            text="🚜 Çiftlik Yönetim Modülü",
            font=FONT_TITLE,
            text_color=ACCENT,
        ).pack(side="left", padx=(0, 12))

        ctk.CTkLabel(
            title_box,
            text="(Yumurta & Yem Takip)",
            font=FONT_SMALL,
            text_color=("gray40", "gray60"),
        ).pack(side="left")

        # Sağ taraf: Dükkana Dön butonu
        if self.on_back_to_shop:
            back_btn = ctk.CTkButton(
                header,
                text="🏪 ← Dükkana Dön (POS)",
                font=FONT_BODY,
                fg_color=("gray75", "gray30"),
                hover_color=ACCENT,
                height=34,
                command=self.on_back_to_shop,
            )
            back_btn.pack(side="right", padx=(10, 0))

        # Sekme Düğmeleri
        subnav_frame = ctk.CTkFrame(header, fg_color=("gray90", "gray20"), corner_radius=10)
        subnav_frame.pack(side="right")

        tabs = [
            ("egg_sales", "🥚 Yumurta Satışı"),
            ("feed_purchases", "🌾 Yem Alımı"),
            ("analytics", "📊 Filtreleme & Müşteri Analizi"),
        ]

        for key, label in tabs:
            btn = ctk.CTkButton(
                subnav_frame,
                text=label,
                font=FONT_BODY,
                height=34,
                fg_color=ACCENT if key == self._current_tab else "transparent",
                hover_color=ACCENT_HOVER if key == self._current_tab else ("gray80", "gray30"),
                text_color="white" if key == self._current_tab else ("gray20", "gray80"),
                corner_radius=8,
                command=lambda k=key: self._switch_tab(k),
            )
            btn.pack(side="left", padx=3, pady=3)
            self._tab_buttons[key] = btn

    def _build_tabs(self) -> None:
        self.container = ctk.CTkFrame(self, fg_color="transparent")
        self.container.pack(fill="both", expand=True)

        # 1. Yumurta Satışı Sekmesi
        self._build_egg_sales_tab()

        # 2. Yem Alımı Sekmesi
        self._build_feed_tab()

        # 3. Filtreleme & Analiz Sekmesi
        self._build_analytics_tab()

        self._switch_tab("egg_sales")

    def _switch_tab(self, tab_key: str) -> None:
        self._current_tab = tab_key
        for key, btn in self._tab_buttons.items():
            if key == tab_key:
                btn.configure(fg_color=ACCENT, text_color="white")
            else:
                btn.configure(fg_color="transparent", text_color=("gray20", "gray80"))

        for key, frame in self._tab_frames.items():
            if key == tab_key:
                frame.pack(fill="both", expand=True)
            else:
                frame.pack_forget()

        if tab_key == "egg_sales":
            self._refresh_egg_sales_tab()
        elif tab_key == "feed_purchases":
            self._refresh_feed_tab()
        elif tab_key == "analytics":
            self._refresh_analytics_tab()

    def refresh(self) -> None:
        self._switch_tab(self._current_tab)

    # ════════════════════════════════════════════════════════════════════
    # SEKME 1: YUMURTA SATIŞI
    # ════════════════════════════════════════════════════════════════════

    def _build_egg_sales_tab(self) -> None:
        tab = ctk.CTkFrame(self.container, fg_color="transparent")
        self._tab_frames["egg_sales"] = tab

        # Sol/Üst: Yeni Satış Formu
        form_card = ctk.CTkFrame(tab, corner_radius=12)
        form_card.pack(fill="x", padx=8, pady=(0, 10))

        f_header = ctk.CTkFrame(form_card, fg_color="transparent")
        f_header.pack(fill="x", padx=16, pady=(12, 6))

        ctk.CTkLabel(
            f_header,
            text="🥚 Yeni Yumurta Satışı Kaydı",
            font=FONT_HEADING,
        ).pack(side="left")

        # Form Alanları Izgarası
        grid_frame = ctk.CTkFrame(form_card, fg_color="transparent")
        grid_frame.pack(fill="x", padx=16, pady=(0, 12))

        # 1. Satır: Müşteri Adı (Otomatik Hatırlayan ComboBox)
        ctk.CTkLabel(grid_frame, text="👤 Müşteri Adı:", font=FONT_BODY).grid(row=0, column=0, sticky="w", padx=6, pady=6)
        self.egg_cust_combo = ctk.CTkComboBox(
            grid_frame,
            width=240,
            height=36,
            font=FONT_BODY,
            values=["Ahmet Yılmaz"],
        )
        self.egg_cust_combo.grid(row=0, column=1, sticky="w", padx=6, pady=6)

        # 2. Satır: Kaç Koli Satıldığı
        ctk.CTkLabel(grid_frame, text="📦 Satılan Koli Adedi:", font=FONT_BODY).grid(row=0, column=2, sticky="w", padx=16, pady=6)
        self.egg_box_entry = ctk.CTkEntry(
            grid_frame,
            placeholder_text="Örn: 2",
            width=120,
            height=36,
            font=FONT_BODY,
        )
        self.egg_box_entry.grid(row=0, column=3, sticky="w", padx=6, pady=6)
        self.egg_box_entry.bind("<KeyRelease>", lambda e: self._update_egg_total_preview())

        # 3. Satır: Kaç Paradan (Birim Koli Fiyatı TL)
        ctk.CTkLabel(grid_frame, text="💰 Koli Birim Fiyatı (TL):", font=FONT_BODY).grid(row=0, column=4, sticky="w", padx=16, pady=6)
        self.egg_price_entry = ctk.CTkEntry(
            grid_frame,
            placeholder_text="Örn: 180.00",
            width=120,
            height=36,
            font=FONT_BODY,
        )
        self.egg_price_entry.grid(row=0, column=5, sticky="w", padx=6, pady=6)
        self.egg_price_entry.bind("<KeyRelease>", lambda e: self._update_egg_total_preview())

        # 4. Satır: Toplam Tutar Göstergesi & Tarih
        row2 = ctk.CTkFrame(form_card, fg_color="transparent")
        row2.pack(fill="x", padx=16, pady=(0, 12))

        ctk.CTkLabel(row2, text="📅 Tarih:", font=FONT_BODY).pack(side="left", padx=(6, 4))
        self.egg_date_entry = ctk.CTkEntry(
            row2,
            width=120,
            height=36,
            font=FONT_BODY,
        )
        self.egg_date_entry.insert(0, datetime.now().strftime("%Y-%m-%d"))
        self.egg_date_entry.pack(side="left", padx=(0, 16))

        ctk.CTkLabel(row2, text="📝 Not / Açıklama:", font=FONT_BODY).pack(side="left", padx=(0, 4))
        self.egg_note_entry = ctk.CTkEntry(
            row2,
            placeholder_text="Opsiyonel açıklama...",
            width=220,
            height=36,
            font=FONT_BODY,
        )
        self.egg_note_entry.pack(side="left", padx=(0, 20))

        # Toplam Tutar Canlı Önizleme
        self.egg_total_preview_lbl = ctk.CTkLabel(
            row2,
            text="Toplam: 0,00 ₺",
            font=FONT_HEADING,
            text_color=ACCENT,
        )
        self.egg_total_preview_lbl.pack(side="left", padx=10)

        # Kaydet Butonu
        save_btn = ctk.CTkButton(
            row2,
            text="💾 Satışı Kaydet (Firebase)",
            font=FONT_HEADING,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            height=40,
            width=200,
            command=self._handle_save_egg_sale,
        )
        save_btn.pack(side="right", padx=6)

        # Alt: Son Kaydedilen Satışlar Tablosu
        table_card = ctk.CTkFrame(tab, corner_radius=12)
        table_card.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        t_header = ctk.CTkFrame(table_card, fg_color="transparent")
        t_header.pack(fill="x", padx=16, pady=(12, 6))

        ctk.CTkLabel(t_header, text="📋 Son Yumurta Satış Kayıtları", font=FONT_HEADING).pack(side="left")

        self.egg_sales_table = DataTable(
            table_card,
            columns=["Tarih", "Müşteri Adı", "Koli Adedi", "Birim Fiyat", "Toplam Tutar", "Kaynak", "Not", "İşlem"],
            column_weights=[2, 3, 2, 2, 2, 2, 3, 1],
            empty_message="Henüz kayıtlı yumurta satışı yok.",
            empty_hint="Yukarıdaki formdan ilk yumurta satışınızı kaydedebilirsiniz.",
            empty_icon="🥚",
        )
        self.egg_sales_table.pack(fill="both", expand=True, padx=12, pady=(0, 8))

    def _update_egg_total_preview(self) -> None:
        try:
            b_str = self.egg_box_entry.get().strip().replace(",", ".")
            p_str = self.egg_price_entry.get().strip().replace(",", ".")
            if b_str and p_str:
                boxes = float(b_str)
                price = float(p_str)
                total = boxes * price
                self.egg_total_preview_lbl.configure(text=f"Toplam: {format_currency(total)}")
            else:
                self.egg_total_preview_lbl.configure(text="Toplam: 0,00 ₺")
        except Exception:
            self.egg_total_preview_lbl.configure(text="Toplam: 0,00 ₺")

    def _handle_save_egg_sale(self) -> None:
        cust = self.egg_cust_combo.get().strip()
        b_str = self.egg_box_entry.get().strip().replace(",", ".")
        p_str = self.egg_price_entry.get().strip().replace(",", ".")
        s_date = self.egg_date_entry.get().strip()
        note = self.egg_note_entry.get().strip()

        if not cust:
            self.on_toast("Lütfen müşteri adı giriniz!", "warning")
            return
        if not b_str:
            self.on_toast("Lütfen kaç koli satıldığını giriniz!", "warning")
            return
        if not p_str:
            self.on_toast("Lütfen koli birim fiyatını giriniz!", "warning")
            return

        try:
            boxes = float(b_str)
            price = float(p_str)
            if boxes <= 0 or price < 0:
                self.on_toast("Koli adedi 0'dan büyük olmalıdır!", "error")
                return
        except ValueError:
            self.on_toast("Geçersiz sayısal format!", "error")
            return

        ok, msg, _ = self.farm_service.create_egg_sale(
            customer_name=cust,
            box_count=boxes,
            unit_price=price,
            sale_date=s_date,
            source="Ciftlik",
            note=note,
        )

        if ok:
            self.on_toast(msg, "success")
            self.egg_box_entry.delete(0, "end")
            self.egg_price_entry.delete(0, "end")
            self.egg_note_entry.delete(0, "end")
            self.egg_total_preview_lbl.configure(text="Toplam: 0,00 ₺")
            self._refresh_egg_sales_tab()
        else:
            self.on_toast(msg, "error")

    def _refresh_egg_sales_tab(self) -> None:
        # Müşteri isimlerini güncelle (Autocomplete / Hatırlama)
        customers = self.farm_service.get_customers()
        if customers:
            self.egg_cust_combo.configure(values=customers)

        # Tabloyu güncelle
        sales_data = self.farm_service.get_egg_sales_analysis(period="all")
        sales = sales_data.get("sales", [])
        self.egg_sales_table.clear_rows()

        if not sales:
            self.egg_sales_table.show_empty(
                message="Henüz kayıtlı yumurta satışı yok.",
                hint="Yukarıdaki formdan yeni satış kaydedebilirsiniz.",
                icon="🥚",
            )
            return

        for s in sales[:50]:
            sid = s.get("id")
            date_str = str(s.get("sale_date", ""))[:16]
            cust_name = s.get("customer_name", "")
            boxes = float(s.get("box_count", 0))
            price = float(s.get("unit_price", 0))
            total = float(s.get("total_amount", 0))
            src = s.get("source", "Ciftlik")
            note = s.get("note", "")

            src_color = ACCENT if src == "Ciftlik" else "#0284C7"

            del_btn = ctk.CTkButton(
                self.egg_sales_table.body,
                text="🗑️",
                width=32,
                height=26,
                font=FONT_SMALL,
                fg_color="transparent",
                hover_color=("gray80", "gray35"),
                text_color=ERROR,
                command=lambda id_=sid: self._delete_egg_sale(id_),
            )

            self.egg_sales_table.add_row(
                values=[
                    date_str,
                    cust_name,
                    f"{boxes:g} Koli",
                    format_currency(price),
                    format_currency(total),
                    f"🏷️ {src}",
                    note or "-",
                    "",
                ],
                row_id=sid,
                text_colors=[None, None, ACCENT, None, None, src_color, None, None],
                custom_widgets={7: del_btn},
            )

    def _delete_egg_sale(self, sale_id: int) -> None:
        ok, msg = self.farm_service.delete_egg_sale(sale_id)
        if ok:
            self.on_toast(msg, "info")
            self._refresh_egg_sales_tab()
        else:
            self.on_toast(msg, "error")

    # ════════════════════════════════════════════════════════════════════
    # SEKME 2: YEM ALIMI (TEK SAYFA - TONAJ HESABI: 1 ÇUVAL = 50 KG)
    # ════════════════════════════════════════════════════════════════════

    def _build_feed_tab(self) -> None:
        tab = ctk.CTkFrame(self.container, fg_color="transparent")
        self._tab_frames["feed_purchases"] = tab

        # ÜST BÖLÜM: Veri Girişi Kartı
        form_card = ctk.CTkFrame(tab, corner_radius=12)
        form_card.pack(fill="x", padx=8, pady=(0, 10))

        f_header = ctk.CTkFrame(form_card, fg_color="transparent")
        f_header.pack(fill="x", padx=16, pady=(12, 6))

        ctk.CTkLabel(f_header, text="🌾 Yem Alımı Veri Girişi (1 Çuval = 50 kg)", font=FONT_HEADING).pack(side="left")

        # Input Row 1
        row1 = ctk.CTkFrame(form_card, fg_color="transparent")
        row1.pack(fill="x", padx=16, pady=(4, 6))

        ctk.CTkLabel(row1, text="📦 Kaç Çuval Alındı:", font=FONT_BODY).pack(side="left", padx=(0, 4))
        self.feed_bag_entry = ctk.CTkEntry(
            row1,
            placeholder_text="Örn: 40",
            width=110,
            height=36,
            font=FONT_BODY,
        )
        self.feed_bag_entry.pack(side="left", padx=(0, 16))
        self.feed_bag_entry.bind("<KeyRelease>", lambda e: self._update_feed_ton_preview())

        # Canlı Tonaj Bilgisi Rozeti
        self.feed_ton_preview_lbl = ctk.CTkLabel(
            row1,
            text="⚖️ 0 Çuval = 0 kg = 0.00 Ton",
            font=(FONT_BODY[0], 12, "bold"),
            text_color=ACCENT,
            fg_color=("gray85", "gray25"),
            corner_radius=8,
            padx=12,
            pady=6,
        )
        self.feed_ton_preview_lbl.pack(side="left", padx=(0, 16))

        ctk.CTkLabel(row1, text="💰 Çuval Fiyatı (TL):", font=FONT_BODY).pack(side="left", padx=(0, 4))
        self.feed_unit_price_entry = ctk.CTkEntry(
            row1,
            placeholder_text="Örn: 450.00",
            width=110,
            height=36,
            font=FONT_BODY,
        )
        self.feed_unit_price_entry.pack(side="left", padx=(0, 16))
        self.feed_unit_price_entry.bind("<KeyRelease>", lambda e: self._update_feed_ton_preview())

        ctk.CTkLabel(row1, text="Toplam Tutar (TL):", font=FONT_BODY).pack(side="left", padx=(0, 4))
        self.feed_total_price_entry = ctk.CTkEntry(
            row1,
            placeholder_text="Opsiyonel...",
            width=120,
            height=36,
            font=FONT_BODY,
        )
        self.feed_total_price_entry.pack(side="left", padx=(0, 10))

        # Input Row 2
        row2 = ctk.CTkFrame(form_card, fg_color="transparent")
        row2.pack(fill="x", padx=16, pady=(4, 14))

        ctk.CTkLabel(row2, text="📅 Alım Tarihi:", font=FONT_BODY).pack(side="left", padx=(0, 4))
        self.feed_date_entry = ctk.CTkEntry(row2, width=120, height=36, font=FONT_BODY)
        self.feed_date_entry.insert(0, datetime.now().strftime("%Y-%m-%d"))
        self.feed_date_entry.pack(side="left", padx=(0, 16))

        ctk.CTkLabel(row2, text="🏭 Tedarikçi / Fabrika / Açıklama:", font=FONT_BODY).pack(side="left", padx=(0, 4))
        self.feed_supplier_entry = ctk.CTkEntry(
            row2,
            placeholder_text="Örn: ABC Yem Fabrikası - Pelet Yem",
            width=280,
            height=36,
            font=FONT_BODY,
        )
        self.feed_supplier_entry.pack(side="left", padx=(0, 20))

        save_feed_btn = ctk.CTkButton(
            row2,
            text="💾 Yem Alımını Kaydet",
            font=FONT_HEADING,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            height=38,
            width=180,
            command=self._handle_save_feed,
        )
        save_feed_btn.pack(side="right", padx=6)

        # ORTA BÖLÜM: İstatistik KPI Kartları
        stats_frame = ctk.CTkFrame(tab, fg_color="transparent")
        stats_frame.pack(fill="x", padx=8, pady=(0, 10))
        stats_frame.grid_columnconfigure((0, 1, 2), weight=1)

        self.feed_stat_bags = StatCard(stats_frame, title="TOPLAM ALINAN ÇUVAL", value="0 Adet", accent=ACCENT)
        self.feed_stat_bags.grid(row=0, column=0, sticky="nsew", padx=4)

        self.feed_stat_tons = StatCard(stats_frame, title="TOPLAM GELEN YEM (TON)", value="0.00 Ton", accent="#0284C7")
        self.feed_stat_tons.grid(row=0, column=1, sticky="nsew", padx=4)

        self.feed_stat_cost = StatCard(stats_frame, title="TOPLAM YEM HARCAMASI", value="0,00 ₺", accent=WARNING)
        self.feed_stat_cost.grid(row=0, column=2, sticky="nsew", padx=4)

        # ALT BÖLÜM: Tarihleri ile Birlikte Yem Gelişi ve Detayları Tablosu
        table_card = ctk.CTkFrame(tab, corner_radius=12)
        table_card.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        t_header = ctk.CTkFrame(table_card, fg_color="transparent")
        t_header.pack(fill="x", padx=16, pady=(12, 6))

        ctk.CTkLabel(t_header, text="📜 Yem Gelişi ve Detayları Geçmişi", font=FONT_HEADING).pack(side="left")

        self.feed_table = DataTable(
            table_card,
            columns=["Tarih", "Çuval Adedi", "Gelen Yem (Ton)", "Toplam Kg", "Çuval Fiyatı", "Toplam Tutar", "Tedarikçi / Not", "İşlem"],
            column_weights=[2, 2, 2, 2, 2, 2, 3, 1],
            empty_message="Henüz yem alım kaydı bulunmamaktadır.",
            empty_hint="Yukarıdaki veri giriş alanından ilk yem alımınızı ekleyebilirsiniz.",
            empty_icon="🌾",
        )
        self.feed_table.pack(fill="both", expand=True, padx=12, pady=(0, 8))

    def _update_feed_ton_preview(self) -> None:
        try:
            b_str = self.feed_bag_entry.get().strip().replace(",", ".")
            if b_str:
                bags = float(b_str)
                kg = bags * 50.0
                tons = kg / 1000.0
                self.feed_ton_preview_lbl.configure(text=f"⚖️ {bags:g} Çuval = {kg:,.0f} kg = {tons:.2f} Ton")

                # Otomatik toplam fiyat hesabı
                p_str = self.feed_unit_price_entry.get().strip().replace(",", ".")
                if p_str:
                    u_price = float(p_str)
                    calc_total = bags * u_price
                    self.feed_total_price_entry.delete(0, "end")
                    self.feed_total_price_entry.insert(0, f"{calc_total:.2f}")
            else:
                self.feed_ton_preview_lbl.configure(text="⚖️ 0 Çuval = 0 kg = 0.00 Ton")
        except Exception:
            pass

    def _handle_save_feed(self) -> None:
        b_str = self.feed_bag_entry.get().strip().replace(",", ".")
        p_str = self.feed_unit_price_entry.get().strip().replace(",", ".")
        t_str = self.feed_total_price_entry.get().strip().replace(",", ".")
        p_date = self.feed_date_entry.get().strip()
        supplier = self.feed_supplier_entry.get().strip()

        if not b_str:
            self.on_toast("Lütfen kaç çuval alındığını giriniz!", "warning")
            return

        try:
            bags = float(b_str)
            if bags <= 0:
                self.on_toast("Çuval adedi 0'dan büyük olmalıdır!", "error")
                return
            u_price = float(p_str) if p_str else 0.0
            t_price = float(t_str) if t_str else 0.0
        except ValueError:
            self.on_toast("Geçersiz sayısal format!", "error")
            return

        ok, msg, _ = self.farm_service.create_feed_purchase(
            bag_count=bags,
            unit_price=u_price,
            total_amount=t_price,
            purchase_date=p_date,
            supplier=supplier,
        )

        if ok:
            self.on_toast(msg, "success")
            self.feed_bag_entry.delete(0, "end")
            self.feed_unit_price_entry.delete(0, "end")
            self.feed_total_price_entry.delete(0, "end")
            self.feed_supplier_entry.delete(0, "end")
            self.feed_ton_preview_lbl.configure(text="⚖️ 0 Çuval = 0 kg = 0.00 Ton")
            self._refresh_feed_tab()
        else:
            self.on_toast(msg, "error")

    def _refresh_feed_tab(self) -> None:
        feed_data = self.farm_service.get_feed_analysis()
        purchases = feed_data.get("purchases", [])

        # KPI'ları güncelle
        self.feed_stat_bags.set_value(f"{feed_data.get('total_bags', 0):g} Çuval")
        self.feed_stat_tons.set_value(f"{feed_data.get('total_tons', 0):.2f} Ton")
        self.feed_stat_cost.set_value(format_currency(feed_data.get("total_cost", 0)))

        self.feed_table.clear_rows()
        if not purchases:
            self.feed_table.show_empty(
                message="Henüz yem alım kaydı bulunmamaktadır.",
                hint="Yukarıdaki formdan yeni yem alımı ekleyebilirsiniz.",
                icon="🌾",
            )
            return

        for p in purchases:
            pid = p.get("id")
            p_date = str(p.get("purchase_date", ""))[:10]
            bags = float(p.get("bag_count", 0))
            ton = float(p.get("total_weight_ton", 0))
            kg = float(p.get("total_weight_kg", 0))
            u_price = float(p.get("unit_price", 0))
            total = float(p.get("total_amount", 0))
            supp = p.get("supplier", "") or p.get("note", "")

            del_btn = ctk.CTkButton(
                self.feed_table.body,
                text="🗑️",
                width=32,
                height=26,
                font=FONT_SMALL,
                fg_color="transparent",
                hover_color=("gray80", "gray35"),
                text_color=ERROR,
                command=lambda id_=pid: self._delete_feed_purchase(id_),
            )

            self.feed_table.add_row(
                values=[
                    p_date,
                    f"{bags:g} Adet",
                    f"{ton:.2f} Ton",
                    f"{kg:,.0f} kg",
                    format_currency(u_price) if u_price > 0 else "-",
                    format_currency(total) if total > 0 else "-",
                    supp or "-",
                    "",
                ],
                row_id=pid,
                text_colors=[None, None, "#0284C7", None, None, ACCENT, None, None],
                custom_widgets={7: del_btn},
            )

    def _delete_feed_purchase(self, purchase_id: int) -> None:
        ok, msg = self.farm_service.delete_feed_purchase(purchase_id)
        if ok:
            self.on_toast(msg, "info")
            self._refresh_feed_tab()
        else:
            self.on_toast(msg, "error")

    # ════════════════════════════════════════════════════════════════════
    # SEKME 3: GELİŞMİŞ FİLTRELEME & MÜŞTERİ ANALİZİ
    # ════════════════════════════════════════════════════════════════════

    def _build_analytics_tab(self) -> None:
        tab = ctk.CTkFrame(self.container, fg_color="transparent")
        self._tab_frames["analytics"] = tab

        # FİLTRE ÇUBUĞU
        filter_card = ctk.CTkFrame(tab, corner_radius=12)
        filter_card.pack(fill="x", padx=8, pady=(0, 10))

        f_row = ctk.CTkFrame(filter_card, fg_color="transparent")
        f_row.pack(fill="x", padx=16, pady=12)

        # Müşteri Filtresi
        ctk.CTkLabel(f_row, text="👤 Müşteri Seçimi:", font=FONT_BODY).pack(side="left", padx=(0, 6))
        self.analysis_cust_combo = ctk.CTkComboBox(
            f_row,
            width=220,
            height=34,
            font=FONT_BODY,
            values=["Tüm Müşteriler (Genel)"],
            command=lambda val: self._on_customer_filter_changed(val),
        )
        self.analysis_cust_combo.set("Tüm Müşteriler (Genel)")
        self.analysis_cust_combo.pack(side="left", padx=(0, 16))

        # Periyot Seçici Düğmeler (Pazartesi-Pazartesi, Aylık, 3 Aylık, Tümü)
        periods = [
            ("weekly", "📅 Haftalık (Pzt - Pzt)"),
            ("monthly", "📅 Aylık"),
            ("3_months", "📅 3 Aylık"),
            ("all", "📅 Tüm Zamanlar"),
        ]

        p_frame = ctk.CTkFrame(f_row, fg_color=("gray85", "gray25"), corner_radius=8)
        p_frame.pack(side="left", padx=(0, 16))

        for p_key, p_label in periods:
            btn = ctk.CTkButton(
                p_frame,
                text=p_label,
                font=FONT_SMALL,
                height=30,
                fg_color=ACCENT if p_key == self._filter_period else "transparent",
                hover_color=ACCENT_HOVER if p_key == self._filter_period else ("gray75", "gray35"),
                text_color="white" if p_key == self._filter_period else ("gray20", "gray80"),
                corner_radius=6,
                command=lambda k=p_key: self._on_period_changed(k),
            )
            btn.pack(side="left", padx=2, pady=2)
            self._period_buttons[p_key] = btn

        # Aktif Aralık Açıklaması
        self.active_period_lbl = ctk.CTkLabel(
            f_row,
            text="",
            font=FONT_SMALL,
            text_color=("gray40", "gray60"),
        )
        self.active_period_lbl.pack(side="left", padx=8)

        # Yenile Butonu
        refresh_btn = ctk.CTkButton(
            f_row,
            text="🔄 Yenile",
            font=FONT_SMALL,
            width=80,
            height=32,
            fg_color=("gray75", "gray30"),
            hover_color=ACCENT,
            command=self._refresh_analytics_tab,
        )
        refresh_btn.pack(side="right")

        # İSTATİSTİK KPI KARTLARI
        stats_frame = ctk.CTkFrame(tab, fg_color="transparent")
        stats_frame.pack(fill="x", padx=8, pady=(0, 10))
        stats_frame.grid_columnconfigure((0, 1, 2, 3), weight=1)

        self.stat_boxes = StatCard(stats_frame, title="TOPLAM SATILAN KOLİ", value="0 Koli", accent=ACCENT)
        self.stat_boxes.grid(row=0, column=0, sticky="nsew", padx=4)

        self.stat_revenue = StatCard(stats_frame, title="TOPLAM CİRO", value="0,00 ₺", accent="#0284C7")
        self.stat_revenue.grid(row=0, column=1, sticky="nsew", padx=4)

        self.stat_avg_price = StatCard(stats_frame, title="ORTALAMA KOLİ FİYATI", value="0,00 ₺", accent=WARNING)
        self.stat_avg_price.grid(row=0, column=2, sticky="nsew", padx=4)

        self.stat_customers = StatCard(stats_frame, title="MÜŞTERİ SAYISI", value="0 Kişi", accent="#8B5CF6")
        self.stat_customers.grid(row=0, column=3, sticky="nsew", padx=4)

        # TABLOLAR ALANI (Müşteri Özeti & Detaylı Hareketler)
        self.analytics_content = ctk.CTkFrame(tab, fg_color="transparent")
        self.analytics_content.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.analytics_content.grid_columnconfigure(0, weight=1)
        self.analytics_content.grid_rowconfigure(0, weight=1)

        # 1. Müşteri Bazında Döküm Tablosu (Tüm müşterilere kaç koli satıldığının özeti)
        self.cust_summary_card = ctk.CTkFrame(self.analytics_content, corner_radius=12)
        self.cust_summary_card.pack(fill="both", expand=True, pady=(0, 8))

        cs_header = ctk.CTkFrame(self.cust_summary_card, fg_color="transparent")
        cs_header.pack(fill="x", padx=16, pady=(10, 4))
        self.cs_header_label = ctk.CTkLabel(
            cs_header,
            text="👥 Müşteri Bazında Toplam Yumurta Satış Dökümü",
            font=FONT_HEADING,
        )
        self.cs_header_label.pack(side="left")

        self.cust_summary_table = DataTable(
            self.cust_summary_card,
            columns=["Müşteri Adı", "Alınan Toplam Koli", "Toplam Ciro (TL)", "Ortalama Koli Fiyatı", "Satış Adedi", "Son Alım Tarihi"],
            column_weights=[4, 3, 3, 3, 2, 3],
            empty_message="Bu dönemde müşteri satışı bulunamadı.",
            empty_hint="Filtre tarih aralığını genişletmeyi deneyebilirsiniz.",
            empty_icon="📊",
        )
        self.cust_summary_table.pack(fill="both", expand=True, padx=12, pady=(0, 8))

        # 2. Detaylı Satış Hareketleri Dökümü
        self.details_card = ctk.CTkFrame(self.analytics_content, corner_radius=12)
        self.details_card.pack(fill="both", expand=True)

        d_header = ctk.CTkFrame(self.details_card, fg_color="transparent")
        d_header.pack(fill="x", padx=16, pady=(10, 4))
        ctk.CTkLabel(d_header, text="📜 Detaylı Satış Hareketleri", font=FONT_HEADING).pack(side="left")

        self.details_table = DataTable(
            self.details_card,
            columns=["Tarih", "Müşteri Adı", "Koli Adedi", "Birim Fiyat", "Toplam Tutar", "Kaynak", "Açıklama", "İşlem"],
            column_weights=[2, 3, 2, 2, 2, 2, 3, 1],
            empty_message="Kayıtlı satış hareketi bulunamadı.",
            empty_hint="",
            empty_icon="🔍",
        )
        self.details_table.pack(fill="both", expand=True, padx=12, pady=(0, 8))

    def _on_customer_filter_changed(self, value: str) -> None:
        clean = value.strip()
        if "Tüm Müşteriler" in clean:
            self._filter_customer = ""
        else:
            self._filter_customer = clean
        self._refresh_analytics_tab()

    def _on_period_changed(self, period_key: str) -> None:
        self._filter_period = period_key
        for key, btn in self._period_buttons.items():
            if key == period_key:
                btn.configure(fg_color=ACCENT, text_color="white")
            else:
                btn.configure(fg_color="transparent", text_color=("gray20", "gray80"))
        self._refresh_analytics_tab()

    def _refresh_analytics_tab(self) -> None:
        # Müşteri listesini güncelle
        customers = self.farm_service.get_customers()
        combo_vals = ["Tüm Müşteriler (Genel)"] + customers
        self.analysis_cust_combo.configure(values=combo_vals)

        cust_filter = self._filter_customer if self._filter_customer else None

        data = self.farm_service.get_egg_sales_analysis(
            customer_name=cust_filter,
            period=self._filter_period,
        )

        s_date = data.get("start_date")
        e_date = data.get("end_date")
        if s_date and e_date:
            period_name = {
                "weekly": "Haftalık (Pzt - Pzt)",
                "monthly": "Aylık",
                "3_months": "3 Aylık",
                "all": "Tüm Zamanlar",
            }.get(self._filter_period, "Özel")
            self.active_period_lbl.configure(text=f"📅 {period_name}: {s_date} ➔ {e_date}")
        else:
            self.active_period_lbl.configure(text="📅 Tüm Zamanlar (Filtresiz)")

        # KPI Güncellemeleri
        total_boxes = data.get("total_boxes", 0)
        total_rev = data.get("total_revenue", 0)
        avg_price = data.get("avg_box_price", 0)
        cust_count = data.get("customer_count", 0)

        self.stat_boxes.set_value(f"{total_boxes:g} Koli")
        self.stat_revenue.set_value(format_currency(total_rev))
        self.stat_avg_price.set_value(format_currency(avg_price))
        self.stat_customers.set_value(f"{cust_count} Müşteri")

        # 1. Müşteri Özeti Tablosu
        c_summary = data.get("customer_summary", [])
        self.cust_summary_table.clear_rows()

        if cust_filter:
            self.cs_header_label.configure(text=f"👤 '{cust_filter}' Müşteri Satış Özeti")
        else:
            self.cs_header_label.configure(text="👥 Tüm Müşterilerin Toplam Satış Dökümü")

        if not c_summary:
            self.cust_summary_table.show_empty(
                message="Seçilen kriterlere uygun satış bulunamadı.",
                hint="Filtreleri değiştirmeyi deneyebilirsiniz.",
                icon="📊",
            )
        else:
            for cs in c_summary:
                self.cust_summary_table.add_row(
                    values=[
                        cs.get("customer_name", ""),
                        f"{cs.get('total_boxes', 0):g} Koli",
                        format_currency(cs.get("total_revenue", 0)),
                        format_currency(cs.get("avg_price", 0)),
                        f"{cs.get('sale_count', 0)} adet",
                        cs.get("last_sale_date", "-"),
                    ],
                    text_colors=[None, ACCENT, None, None, None, None],
                )

        # 2. Detaylı Hareketler Tablosu
        sales = data.get("sales", [])
        self.details_table.clear_rows()

        if not sales:
            self.details_table.show_empty(
                message="Satış hareketi bulunamadı.",
                hint="",
                icon="🔍",
            )
        else:
            for s in sales:
                sid = s.get("id")
                date_str = str(s.get("sale_date", ""))[:16]
                c_name = s.get("customer_name", "")
                boxes = float(s.get("box_count", 0))
                price = float(s.get("unit_price", 0))
                total = float(s.get("total_amount", 0))
                src = s.get("source", "Ciftlik")
                note = s.get("note", "")

                src_color = ACCENT if src == "Ciftlik" else "#0284C7"

                del_btn = ctk.CTkButton(
                    self.details_table.body,
                    text="🗑️",
                    width=32,
                    height=26,
                    font=FONT_SMALL,
                    fg_color="transparent",
                    hover_color=("gray80", "gray35"),
                    text_color=ERROR,
                    command=lambda id_=sid: self._delete_egg_sale_from_analytics(id_),
                )

                self.details_table.add_row(
                    values=[
                        date_str,
                        c_name,
                        f"{boxes:g} Koli",
                        format_currency(price),
                        format_currency(total),
                        f"🏷️ {src}",
                        note or "-",
                        "",
                    ],
                    row_id=sid,
                    text_colors=[None, None, ACCENT, None, None, src_color, None, None],
                    custom_widgets={7: del_btn},
                )

    def _delete_egg_sale_from_analytics(self, sale_id: int) -> None:
        ok, msg = self.farm_service.delete_egg_sale(sale_id)
        if ok:
            self.on_toast(msg, "info")
            self._refresh_analytics_tab()
        else:
            self.on_toast(msg, "error")
