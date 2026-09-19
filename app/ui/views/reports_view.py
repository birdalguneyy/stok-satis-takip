import concurrent.futures
import threading
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

import customtkinter as ctk

from app.database.cloud_db import CloudDatabase
from app.services.forecast_service import ForecastService
from app.services.sale_service import SaleService
from app.ui.components.stat_card import StatCard
from app.ui.theme import (
    ACCENT,
    ACCENT_HOVER,
    ERROR,
    FONT_BODY,
    FONT_HEADING,
    FONT_SMALL,
    FONT_TITLE,
    SUCCESS,
    WARNING,
)
from app.ui.views.history_view import HistoryView
from app.utils.formatters import format_currency


class ReportsView(ctk.CTkFrame):
    """Satış Raporları & AI Merkezi:
    4 Alt Menüden Oluşur:
    1. 📜 Satışlar & Filtreleme (Detaylı geçmiş, kolay tarih filtreleme, grup görünümü, toplu silme)
    2. 💰 Aylık & Haftalık Ciro (Dönemsel ciro KPI'ları, her ayın ve haftanın net ciroları, mağaza/internet)
    3. ⚡ Gelecek Tahminleri (Çok çekirdekli simülasyon, en yoğun günler, 7 günlük talep & stok projeksiyonu)
    4. 🤖 AI Danışmanı (Akıllı iş geliştirme önerileri, çalışma saatleri & karlı ürün analizleri)
    """

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
        self.cloud_db = CloudDatabase()
        self.forecast_service = ForecastService(self.cloud_db)

        self._current_subtab = "sales"
        self._subnav_buttons: Dict[str, ctk.CTkButton] = {}
        self._sub_frames: Dict[str, ctk.CTkFrame] = {}

        # Revenue Date Filter State
        self._rev_start_date: Optional[str] = None
        self._rev_end_date: Optional[str] = None
        self._rev_date_preset: str = "all"
        self._rev_date_buttons: Dict[str, ctk.CTkButton] = {}

        # Cached forecast data
        self._forecast_data: Optional[Dict[str, Any]] = None

        self._build_top_bar()
        self._build_subtabs()

    def _build_top_bar(self) -> None:
        top_bar = ctk.CTkFrame(self, fg_color="transparent")
        top_bar.pack(fill="x", padx=8, pady=(0, 10))

        # Title
        ctk.CTkLabel(
            top_bar,
            text="📊 Satış Raporları & AI Danışmanı",
            font=FONT_TITLE,
        ).pack(side="left", padx=(0, 20))

        # Sub-menu navigation buttons
        subnav_frame = ctk.CTkFrame(top_bar, fg_color=("gray90", "gray20"), corner_radius=10)
        subnav_frame.pack(side="left")

        tabs = [
            ("sales", "📜 Satışlar & Filtreleme"),
            ("revenue", "💰 Aylık & Haftalık Ciro"),
            ("forecasts", "⚡ Gelecek Tahminleri"),
            ("ai", "🤖 AI Danışmanı"),
        ]

        for key, title in tabs:
            btn = ctk.CTkButton(
                subnav_frame,
                text=title,
                font=FONT_BODY,
                height=34,
                fg_color=ACCENT if key == "sales" else "transparent",
                hover_color=ACCENT_HOVER if key == "sales" else ("gray80", "gray30"),
                text_color="white" if key == "sales" else ("gray20", "gray80"),
                corner_radius=8,
                command=lambda k=key: self._switch_subtab(k),
            )
            btn.pack(side="left", padx=3, pady=3)
            self._subnav_buttons[key] = btn

    def _build_subtabs(self) -> None:
        self.container = ctk.CTkFrame(self, fg_color="transparent")
        self.container.pack(fill="both", expand=True)

        # 1. Satışlar & Filtreleme (Embeds HistoryView)
        self.history_view = HistoryView(
            self.container,
            sale_service=self.sale_service,
            on_toast=self.on_toast,
            on_stock_changed=self.on_stock_changed,
        )
        self._sub_frames["sales"] = self.history_view
        self.history_view.pack(fill="both", expand=True)

        # 2. Aylık & Haftalık Ciro
        self._build_revenue_subtab()

        # 3. Gelecek Tahminleri
        self._build_forecasts_subtab()

        # 4. AI Danışmanı
        self._build_ai_subtab()

    def _switch_subtab(self, tab_key: str) -> None:
        self._current_subtab = tab_key
        for key, btn in self._subnav_buttons.items():
            if key == tab_key:
                btn.configure(fg_color=ACCENT, text_color="white")
            else:
                btn.configure(fg_color="transparent", text_color=("gray20", "gray80"))

        for key, frame in self._sub_frames.items():
            if key == tab_key:
                frame.pack(fill="both", expand=True)
            else:
                frame.pack_forget()

        if tab_key == "sales":
            self.history_view.refresh()
        elif tab_key == "revenue":
            self._refresh_revenue()
        elif tab_key == "forecasts":
            self._refresh_forecasts()
        elif tab_key == "ai":
            self._refresh_ai()

    def on_show(self) -> None:
        if self._current_subtab == "sales":
            self.history_view.refresh()
        elif self._current_subtab == "revenue":
            self._refresh_revenue()
        elif self._current_subtab == "forecasts":
            self._refresh_forecasts()
        elif self._current_subtab == "ai":
            self._refresh_ai()

    def refresh(self) -> None:
        self.on_show()

    # ════════════════════════════════════════════════════════════════
    # SUBTAB 2: AYLIK & HAFTALIK CİRO
    # ════════════════════════════════════════════════════════════════
    def _build_revenue_subtab(self) -> None:
        rev_frame = ctk.CTkScrollableFrame(self.container, fg_color="transparent")
        self._sub_frames["revenue"] = rev_frame

        # Easy Date Presets Bar
        date_bar = ctk.CTkFrame(rev_frame, fg_color=("gray90", "gray18"), corner_radius=8)
        date_bar.pack(fill="x", padx=4, pady=(0, 10))

        presets_row = ctk.CTkFrame(date_bar, fg_color="transparent")
        presets_row.pack(fill="x", padx=8, pady=8)

        ctk.CTkLabel(
            presets_row,
            text="📅 Kolay Tarih:",
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
            is_active = (p_key == self._rev_date_preset)
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
                command=lambda k=p_key: self._set_rev_date_preset(k),
            )
            btn.pack(side="left", padx=2)
            self._rev_date_buttons[p_key] = btn

        self._rev_date_badge = ctk.CTkLabel(
            presets_row,
            text="Tüm Zamanlar",
            font=FONT_SMALL,
            text_color=ACCENT,
        )
        self._rev_date_badge.pack(side="right", padx=(4, 0))

        # KPI Cards Row
        cards_row = ctk.CTkFrame(rev_frame, fg_color="transparent")
        cards_row.pack(fill="x", padx=4, pady=(0, 12))
        cards_row.grid_columnconfigure((0, 1, 2, 3), weight=1)

        self.kpi_revenue = StatCard(cards_row, "Toplam Ciro", accent=SUCCESS)
        self.kpi_revenue.grid(row=0, column=0, sticky="ew", padx=(0, 6))

        self.kpi_expense = StatCard(cards_row, "Toplam Gider", accent=ERROR)
        self.kpi_expense.grid(row=0, column=1, sticky="ew", padx=3)

        self.kpi_profit = StatCard(cards_row, "Net Kâr", accent="#10B981")
        self.kpi_profit.grid(row=0, column=2, sticky="ew", padx=3)

        self.kpi_items = StatCard(cards_row, "Satılan Ürün", accent=ACCENT)
        self.kpi_items.grid(row=0, column=3, sticky="ew", padx=(6, 0))

        # Channel Split Banner
        self.channel_box = ctk.CTkFrame(rev_frame, fg_color=("gray92", "gray18"), corner_radius=10)
        self.channel_box.pack(fill="x", padx=4, pady=(0, 14))

        ch_header = ctk.CTkFrame(self.channel_box, fg_color="transparent")
        ch_header.pack(fill="x", padx=12, pady=(10, 6))

        ctk.CTkLabel(
            ch_header,
            text="🛍️ Satış Kanalları Dağılımı",
            font=FONT_HEADING,
        ).pack(side="left")

        self.channel_info_lbl = ctk.CTkLabel(
            ch_header,
            text="Mağaza: 0,00 ₺ (0 adet) | İnternet: 0,00 ₺ (0 adet)",
            font=FONT_BODY,
            text_color=("gray40", "gray60"),
        )
        self.channel_info_lbl.pack(side="right")

        # Two Column Periodic Breakdown: Left: Monthly, Right: Weekly
        two_col_frame = ctk.CTkFrame(rev_frame, fg_color="transparent")
        two_col_frame.pack(fill="both", expand=True, padx=4, pady=(0, 10))
        two_col_frame.grid_columnconfigure((0, 1), weight=1)

        # Left: Monthly Revenue
        monthly_container = ctk.CTkFrame(two_col_frame, corner_radius=10)
        monthly_container.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        m_head = ctk.CTkFrame(monthly_container, fg_color="transparent")
        m_head.pack(fill="x", padx=14, pady=(12, 8))
        ctk.CTkLabel(m_head, text="📅 Her Ayın Toplam Cirosu", font=FONT_HEADING).pack(side="left")
        self.monthly_count_lbl = ctk.CTkLabel(m_head, text="Son 24 Ay", font=FONT_SMALL, text_color="gray")
        self.monthly_count_lbl.pack(side="right")

        self.monthly_list_frame = ctk.CTkFrame(monthly_container, fg_color="transparent")
        self.monthly_list_frame.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        # Right: Weekly Revenue
        weekly_container = ctk.CTkFrame(two_col_frame, corner_radius=10)
        weekly_container.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        w_head = ctk.CTkFrame(weekly_container, fg_color="transparent")
        w_head.pack(fill="x", padx=14, pady=(12, 8))
        ctk.CTkLabel(w_head, text="🗓️ Her Haftanın Toplam Cirosu", font=FONT_HEADING).pack(side="left")
        self.weekly_count_lbl = ctk.CTkLabel(w_head, text="Son 16 Hafta", font=FONT_SMALL, text_color="gray")
        self.weekly_count_lbl.pack(side="right")

        self.weekly_list_frame = ctk.CTkFrame(weekly_container, fg_color="transparent")
        self.weekly_list_frame.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    def _set_rev_date_preset(self, preset: str) -> None:
        self._rev_date_preset = preset
        today = datetime.now().date()

        if preset == "today":
            self._rev_start_date = today.strftime("%Y-%m-%d")
            self._rev_end_date = today.strftime("%Y-%m-%d")
            badge_text = f"Bugün ({today.strftime('%d.%m.%Y')})"
        elif preset == "yesterday":
            yest = today - timedelta(days=1)
            self._rev_start_date = yest.strftime("%Y-%m-%d")
            self._rev_end_date = yest.strftime("%Y-%m-%d")
            badge_text = f"Dün ({yest.strftime('%d.%m.%Y')})"
        elif preset == "this_week":
            start = today - timedelta(days=today.weekday())
            end = start + timedelta(days=6)
            self._rev_start_date = start.strftime("%Y-%m-%d")
            self._rev_end_date = end.strftime("%Y-%m-%d")
            badge_text = f"Bu Hafta ({start.strftime('%d.%m')} - {end.strftime('%d.%m.%Y')})"
        elif preset == "last_week":
            start = today - timedelta(days=today.weekday() + 7)
            end = start + timedelta(days=6)
            self._rev_start_date = start.strftime("%Y-%m-%d")
            self._rev_end_date = end.strftime("%Y-%m-%d")
            badge_text = f"Geçen Hafta ({start.strftime('%d.%m')} - {end.strftime('%d.%m.%Y')})"
        elif preset == "this_month":
            start = today.replace(day=1)
            if today.month == 12:
                next_month = today.replace(year=today.year + 1, month=1, day=1)
            else:
                next_month = today.replace(month=today.month + 1, day=1)
            end = next_month - timedelta(days=1)
            self._rev_start_date = start.strftime("%Y-%m-%d")
            self._rev_end_date = end.strftime("%Y-%m-%d")
            badge_text = f"Bu Ay ({start.strftime('%d.%m')} - {end.strftime('%d.%m.%Y')})"
        elif preset == "last_month":
            first_this_month = today.replace(day=1)
            end = first_this_month - timedelta(days=1)
            start = end.replace(day=1)
            self._rev_start_date = start.strftime("%Y-%m-%d")
            self._rev_end_date = end.strftime("%Y-%m-%d")
            badge_text = f"Geçen Ay ({start.strftime('%d.%m')} - {end.strftime('%d.%m.%Y')})"
        elif preset == "last_30":
            start = today - timedelta(days=29)
            self._rev_start_date = start.strftime("%Y-%m-%d")
            self._rev_end_date = today.strftime("%Y-%m-%d")
            badge_text = f"Son 30 Gün ({start.strftime('%d.%m')} - {today.strftime('%d.%m.%Y')})"
        else:
            self._rev_start_date = None
            self._rev_end_date = None
            badge_text = "Tüm Zamanlar"

        for p, btn in self._rev_date_buttons.items():
            if p == preset:
                btn.configure(fg_color=ACCENT, text_color="white")
            else:
                btn.configure(fg_color=("gray85", "gray28"), text_color=("gray10", "gray90"))

        self._rev_date_badge.configure(text=badge_text)
        self._refresh_revenue()

    def _refresh_revenue(self) -> None:
        try:
            analytics = self.cloud_db.get_sales_analytics(
                start_date=self._rev_start_date,
                end_date=self._rev_end_date,
            )
        except Exception as ex:
            self.on_toast(f"Ciro raporları alınamadı: {ex}", "error")
            return

        # Update KPIs
        self.kpi_revenue.set_value(format_currency(analytics.get("total_revenue", 0.0)))
        self.kpi_expense.set_value(format_currency(analytics.get("total_expenses", 0.0)))
        self.kpi_profit.set_value(format_currency(analytics.get("net_profit", 0.0)))
        self.kpi_items.set_value(f"{analytics.get('total_items_sold', 0)} adet")

        # Update Channels
        ch = analytics.get("channel_stats", {})
        mag = ch.get("magaza", {})
        net = ch.get("internet", {})
        mag_rev = format_currency(mag.get("revenue", 0.0))
        net_rev = format_currency(net.get("revenue", 0.0))
        self.channel_info_lbl.configure(
            text=f"🏪 Mağaza: {mag_rev} ({mag.get('items', 0)} adet)   |   🌐 İnternet: {net_rev} ({net.get('items', 0)} adet)"
        )

        # Render Monthly List
        for w in self.monthly_list_frame.winfo_children():
            w.destroy()

        monthly_stats = analytics.get("monthly_stats", [])
        if not monthly_stats:
            ctk.CTkLabel(
                self.monthly_list_frame,
                text="Bu dönemde aylık satış verisi bulunamadı.",
                font=FONT_BODY,
                text_color="gray",
            ).pack(anchor="w", pady=8)
        else:
            for item in monthly_stats:
                m_card = ctk.CTkFrame(self.monthly_list_frame, fg_color=("gray94", "gray22"), corner_radius=8)
                m_card.pack(fill="x", pady=4)

                top_r = ctk.CTkFrame(m_card, fg_color="transparent")
                top_r.pack(fill="x", padx=10, pady=(6, 2))
                ctk.CTkLabel(top_r, text=item.get("month_label", "-"), font=FONT_HEADING).pack(side="left")
                ctk.CTkLabel(
                    top_r,
                    text=format_currency(item.get("revenue", 0.0)),
                    font=FONT_HEADING,
                    text_color="#10B981",
                ).pack(side="right")

                sub_r = ctk.CTkFrame(m_card, fg_color="transparent")
                sub_r.pack(fill="x", padx=10, pady=(0, 6))
                sub_txt = f"{item.get('tx_count', 0)} İşlem • {item.get('items', 0)} Adet • Mağaza: {format_currency(item.get('store_revenue', 0.0))} • Net: {format_currency(item.get('net_revenue', 0.0))}"
                ctk.CTkLabel(sub_r, text=sub_txt, font=FONT_SMALL, text_color=("gray40", "gray60")).pack(side="left")

        # Render Weekly List
        for w in self.weekly_list_frame.winfo_children():
            w.destroy()

        weekly_stats = analytics.get("weekly_stats", [])
        if not weekly_stats:
            ctk.CTkLabel(
                self.weekly_list_frame,
                text="Bu dönemde haftalık satış verisi bulunamadı.",
                font=FONT_BODY,
                text_color="gray",
            ).pack(anchor="w", pady=8)
        else:
            for item in weekly_stats:
                w_card = ctk.CTkFrame(self.weekly_list_frame, fg_color=("gray94", "gray22"), corner_radius=8)
                w_card.pack(fill="x", pady=4)

                top_r = ctk.CTkFrame(w_card, fg_color="transparent")
                top_r.pack(fill="x", padx=10, pady=(6, 2))
                ctk.CTkLabel(top_r, text=item.get("week_label", "-"), font=FONT_HEADING).pack(side="left")
                ctk.CTkLabel(
                    top_r,
                    text=format_currency(item.get("revenue", 0.0)),
                    font=FONT_HEADING,
                    text_color=ACCENT,
                ).pack(side="right")

                sub_r = ctk.CTkFrame(w_card, fg_color="transparent")
                sub_r.pack(fill="x", padx=10, pady=(0, 6))
                sub_txt = f"{item.get('tx_count', 0)} İşlem • {item.get('items', 0)} Ürün • Ort: {format_currency(item.get('avg_cart', 0.0))}"
                ctk.CTkLabel(sub_r, text=sub_txt, font=FONT_SMALL, text_color=("gray40", "gray60")).pack(side="left")

    # ════════════════════════════════════════════════════════════════
    # SUBTAB 3: GELECEK TAHMİNLERİ (AI)
    # ════════════════════════════════════════════════════════════════
    def _build_forecasts_subtab(self) -> None:
        fc_frame = ctk.CTkScrollableFrame(self.container, fg_color="transparent")
        self._sub_frames["forecasts"] = fc_frame

        # Hardware simulation banner
        self.hw_banner = ctk.CTkFrame(fc_frame, fg_color=("gray88", "gray22"), corner_radius=10)
        self.hw_banner.pack(fill="x", padx=4, pady=(0, 10))

        hw_inner = ctk.CTkFrame(self.hw_banner, fg_color="transparent")
        hw_inner.pack(fill="x", padx=12, pady=10)

        ctk.CTkLabel(
            hw_inner,
            text="⚡ Cihaz Gücüyle Desteklenen Tahminleme Motoru",
            font=FONT_HEADING,
            text_color="#8B5CF6",
        ).pack(side="left")

        self.hw_stats_lbl = ctk.CTkLabel(
            hw_inner,
            text="Donanım: Hesaplanıyor...",
            font=FONT_SMALL,
            text_color=("gray30", "gray70"),
        )
        self.hw_stats_lbl.pack(side="right")

        # Peak day highlight box
        self.peak_box = ctk.CTkFrame(fc_frame, fg_color=("#EEF2FF", "#1E1B4B"), corner_radius=10)
        self.peak_box.pack(fill="x", padx=4, pady=(0, 12))

        peak_inner = ctk.CTkFrame(self.peak_box, fg_color="transparent")
        peak_inner.pack(fill="x", padx=14, pady=12)

        self.peak_day_title = ctk.CTkLabel(
            peak_inner,
            text="Haftanın En Yoğun Günü: Hesaplanıyor...",
            font=FONT_TITLE,
            text_color="#6366F1",
            anchor="w",
        )
        self.peak_day_title.pack(fill="x")

        self.peak_day_desc = ctk.CTkLabel(
            peak_inner,
            text="Müşteri ve ciro verileri analiz edilerek yoğun günler tespit ediliyor.",
            font=FONT_BODY,
            text_color=("gray20", "gray80"),
            anchor="w",
        )
        self.peak_day_desc.pack(fill="x", pady=(4, 0))

        # 7 Days distribution
        days_header = ctk.CTkLabel(
            fc_frame,
            text="📅 7 Günlük Müşteri & Satış Yoğunluk Dağılımı",
            font=FONT_HEADING,
            anchor="w",
        )
        days_header.pack(fill="x", padx=4, pady=(6, 8))

        self.days_cards_frame = ctk.CTkFrame(fc_frame, fg_color="transparent")
        self.days_cards_frame.pack(fill="x", padx=4, pady=(0, 14))
        self.days_cards_frame.grid_columnconfigure((0, 1, 2, 3, 4, 5, 6), weight=1)

        # Top demand products
        prod_head = ctk.CTkLabel(
            fc_frame,
            text="🔥 Önümüzdeki Dönemde Çok Satması Beklenen Ürünler & Stok Riski",
            font=FONT_HEADING,
            anchor="w",
        )
        prod_head.pack(fill="x", padx=4, pady=(6, 8))

        self.demand_list_frame = ctk.CTkFrame(fc_frame, fg_color="transparent")
        self.demand_list_frame.pack(fill="x", padx=4, pady=(0, 12))

    def _refresh_forecasts(self) -> None:
        def worker():
            try:
                fc = self.forecast_service.generate_comprehensive_forecast()
                self._forecast_data = fc
                self.after(0, self._render_forecast_ui)
            except Exception as e:
                self.after(0, lambda: self.on_toast(f"Tahminler hesaplanamadı: {e}", "error"))

        threading.Thread(target=worker, daemon=True).start()

    def _render_forecast_ui(self) -> None:
        if not self._forecast_data:
            return

        fc = self._forecast_data
        hw = fc.get("hardware_metrics", {})
        busy = fc.get("busy_days", {})
        day_forecasts = busy.get("day_forecasts", [])
        top_prods = fc.get("product_forecasts", {}).get("top_predicted_products", [])

        # Update Hardware stats
        cores = hw.get("cpu_cores", 4)
        ms = hw.get("elapsed_ms", 0)
        sims = hw.get("simulations_count", 0)
        self.hw_stats_lbl.configure(text=f"🚀 {cores} CPU Çekirdeği • {sims} Simülasyon • {ms:.1f} ms")

        # Update Peak Day Banner
        p_day = busy.get("peak_day", "Belirleniyor")
        p_pct = busy.get("peak_intensity", 100)
        self.peak_day_title.configure(text=f"🔥 Haftanın En Yoğun Günü: {p_day} (%{p_pct} Yoğunluk)")
        self.peak_day_desc.configure(
            text=f"{p_day} günleri mağazanız ve internet siparişlerinizde en yüksek talep beklenmektedir. Stok ve personel planlamanızı bu güne göre yapınız."
        )

        # Render 7 days cards
        for w in self.days_cards_frame.winfo_children():
            w.destroy()

        for idx, df in enumerate(day_forecasts):
            col_card = ctk.CTkFrame(self.days_cards_frame, corner_radius=8, fg_color=("gray92", "gray20"))
            col_card.grid(row=0, column=idx, sticky="nsew", padx=3, pady=2)

            d_name = df.get("day_name", "-")
            d_pct = df.get("intensity_pct", 0)
            d_lvl = df.get("level", "Normal")

            color = "#10B981" if d_pct < 50 else ("#F59E0B" if d_pct < 80 else "#EF4444")

            ctk.CTkLabel(col_card, text=d_name[:3], font=FONT_HEADING).pack(pady=(8, 2))
            ctk.CTkLabel(col_card, text=f"%{d_pct}", font=FONT_HEADING, text_color=color).pack()
            ctk.CTkLabel(col_card, text=d_lvl, font=FONT_SMALL, text_color=("gray40", "gray60")).pack(pady=(0, 8))

        # Render Top Demand Products
        for w in self.demand_list_frame.winfo_children():
            w.destroy()

        if not top_prods:
            ctk.CTkLabel(
                self.demand_list_frame,
                text="Yeterli satış geçmişi oluştuğunda ürün bazlı talep tahminleri burada listelenecektir.",
                font=FONT_BODY,
                text_color="gray",
            ).pack(anchor="w", pady=6)
        else:
            for p in top_prods[:8]:
                row = ctk.CTkFrame(self.demand_list_frame, fg_color=("gray92", "gray20"), corner_radius=8)
                row.pack(fill="x", pady=3)

                ctk.CTkLabel(row, text=p.get("name", "-"), font=FONT_BODY, anchor="w").pack(
                    side="left", fill="x", expand=True, padx=12, pady=8
                )

                risk = p.get("risk_level", "NORMAL")
                risk_color = "#EF4444" if risk == "YUKSEK" else ("#F59E0B" if risk == "ORTA" else "#10B981")

                pred_qty = p.get("predicted_next_7_days", 0)
                curr_stock = p.get("current_stock", 0)

                ctk.CTkLabel(
                    row,
                    text=f"Beklenen Talep: ~{pred_qty} adet",
                    font=FONT_BODY,
                    text_color=ACCENT,
                ).pack(side="left", padx=12)

                ctk.CTkLabel(
                    row,
                    text=f"Mevcut Stok: {curr_stock}",
                    font=FONT_BODY,
                    text_color="white" if curr_stock > 0 else "#EF4444",
                ).pack(side="left", padx=12)

                ctk.CTkLabel(
                    row,
                    text=f"Risk: {risk}",
                    font=FONT_SMALL,
                    text_color=risk_color,
                ).pack(side="right", padx=12)

    # ════════════════════════════════════════════════════════════════
    # SUBTAB 4: AI DANIŞMANI
    # ════════════════════════════════════════════════════════════════
    def _build_ai_subtab(self) -> None:
        ai_frame = ctk.CTkScrollableFrame(self.container, fg_color="transparent")
        self._sub_frames["ai"] = ai_frame

        # AI Advisor Card
        advice_card = ctk.CTkFrame(ai_frame, fg_color=("#F5F3FF", "#1E1A38"), corner_radius=12)
        advice_card.pack(fill="x", padx=4, pady=(0, 14))

        ad_inner = ctk.CTkFrame(advice_card, fg_color="transparent")
        ad_inner.pack(fill="x", padx=16, pady=16)

        ctk.CTkLabel(
            ad_inner,
            text="🤖 Antigravity Akıllı İş Geliştirme Danışmanı",
            font=FONT_TITLE,
            text_color="#8B5CF6",
        ).pack(anchor="w")

        self.advice_content_lbl = ctk.CTkLabel(
            ad_inner,
            text="AI motoru mağaza ve internet satış trendlerinizi inceliyor...",
            font=FONT_BODY,
            text_color=("gray10", "gray90"),
            wraplength=850,
            justify="left",
        )
        self.advice_content_lbl.pack(fill="x", pady=(10, 14))

        # Re-run button
        ctk.CTkButton(
            ad_inner,
            text="🔄 Tavsiyeleri & Analizi Yenile",
            font=FONT_SMALL,
            height=32,
            fg_color="#8B5CF6",
            hover_color="#7C3AED",
            command=self._refresh_ai,
        ).pack(anchor="w")

        # Working hours & tips card
        hours_card = ctk.CTkFrame(ai_frame, fg_color=("gray92", "gray20"), corner_radius=10)
        hours_card.pack(fill="x", padx=4, pady=(0, 12))

        h_inner = ctk.CTkFrame(hours_card, fg_color="transparent")
        h_inner.pack(fill="x", padx=16, pady=14)

        ctk.CTkLabel(
            h_inner,
            text="⏰ Çalışma Saatleri & Yoğun Zaman Analizi",
            font=FONT_HEADING,
        ).pack(anchor="w")

        self.hours_desc_lbl = ctk.CTkLabel(
            h_inner,
            text="Veriler analiz edildikten sonra gün içi en çok satış yapılan saat dilimleri belirlenecektir.",
            font=FONT_BODY,
            text_color=("gray30", "gray70"),
            wraplength=850,
            justify="left",
        )
        self.hours_desc_lbl.pack(fill="x", pady=(8, 0))

    def _refresh_ai(self) -> None:
        def worker():
            try:
                fc = self.forecast_service.generate_comprehensive_forecast()
                self._forecast_data = fc
                self.after(0, self._render_ai_ui)
            except Exception as e:
                self.after(0, lambda: self.on_toast(f"AI danışman verileri alınamadı: {e}", "error"))

        threading.Thread(target=worker, daemon=True).start()

    def _render_ai_ui(self) -> None:
        if not self._forecast_data:
            return

        fc = self._forecast_data
        advice = fc.get("ai_insights", {}).get("strategic_advice", "")
        if not advice:
            advice = (
                "Satış verileriniz düzenli olarak işlenmektedir. Haftanın yoğun günlerinde stok miktarlarınızı "
                "artırmanız ve internet satış kanalına ağırlık vermeniz cironuzu %25'e kadar yükseltebilir."
            )
        self.advice_content_lbl.configure(text=advice)

        busy = fc.get("busy_days", {})
        peak_hours = busy.get("hourly_peaks", {}).get("peak_period", "14:00 - 18:00")
        peak_day = busy.get("peak_day", "Cumartesi")

        self.hours_desc_lbl.configure(
            text=f"• En Yoğun Satış Saatleri: {peak_hours}\n"
            f"• En Yüksek Ciro Getiren Gün: {peak_day}\n"
            f"• Öneri: Yoğun saatlerde internet siparişlerini erken kargoya hazırlamak ve müşteri taleplerini hızlı karşılamak mağaza puanınızı yükseltecektir."
        )
