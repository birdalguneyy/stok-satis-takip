from typing import Any, Callable, Dict, List, Optional
import customtkinter as ctk

from app.database.cloud_db import CloudDatabase
from app.ui.theme import (
    ACCENT,
    ACCENT_HOVER,
    FONT_BODY,
    FONT_HEADING,
    FONT_SMALL,
    FONT_TITLE,
)


class WebOrdersView(ctk.CTkFrame):
    """Gallus Mama Web Siparişleri, Puan Onayı (%1 × 100) ve Bülten Mailleri Yönetim Ekranı."""

    def __init__(
        self,
        master,
        on_toast: Callable[[str, str], None],
        **kwargs,
    ) -> None:
        super().__init__(master, fg_color="transparent", **kwargs)
        self.on_toast = on_toast
        self.cloud_db = CloudDatabase()
        self._subscribers: List[str] = []
        self._orders: List[Dict[str, Any]] = []

        self._build_ui()

    def _build_ui(self) -> None:
        # Header
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=12, pady=(4, 10))

        title_box = ctk.CTkFrame(header, fg_color="transparent")
        title_box.pack(side="left")

        ctk.CTkLabel(
            title_box,
            text="🌐 Web Siparişleri & Bülten Mailleri",
            font=FONT_TITLE,
            text_color=ACCENT,
        ).pack(side="left", padx=(0, 10))

        self.status_lbl = ctk.CTkLabel(
            title_box,
            text="Gallus Mama Canlı Senkronizasyon",
            font=FONT_SMALL,
            text_color="gray50",
        )
        self.status_lbl.pack(side="left")

        ctk.CTkButton(
            header,
            text="🔄 Yenile",
            font=("Segoe UI", 12, "bold"),
            fg_color="#059669",
            hover_color="#047857",
            height=32,
            width=90,
            corner_radius=10,
            command=self.refresh,
        ).pack(side="right")

        # 1. Card: Newsletter / Member Emails Pool
        mail_card = ctk.CTkFrame(self, corner_radius=14, border_width=2, border_color="#D97706")
        mail_card.pack(fill="x", padx=12, pady=(0, 10))

        mail_top = ctk.CTkFrame(mail_card, fg_color="transparent")
        mail_top.pack(fill="x", padx=12, pady=(10, 4))

        self.mail_title_lbl = ctk.CTkLabel(
            mail_top,
            text="📧 Bültene Abone Olan & Üye E-Postaları (0 Adet)",
            font=("Segoe UI", 13, "bold"),
            text_color="#D97706",
        )
        self.mail_title_lbl.pack(side="left")

        ctk.CTkButton(
            mail_top,
            text="📋 Tüm Mailleri Toplu Kopyala",
            font=("Segoe UI", 11, "bold"),
            fg_color="#D97706",
            hover_color="#B45309",
            height=28,
            command=lambda: self._copy_emails(", "),
        ).pack(side="right", padx=(6, 0))

        ctk.CTkButton(
            mail_top,
            text="📄 Alt Alta Kopyala",
            font=("Segoe UI", 11),
            fg_color="#475569",
            hover_color="#334155",
            height=28,
            command=lambda: self._copy_emails("\n"),
        ).pack(side="right")

        self.mail_textbox = ctk.CTkTextbox(mail_card, height=55, font=("Consolas", 11))
        self.mail_textbox.pack(fill="x", padx=12, pady=(2, 10))

        # 2. Scrollable Frame: Orders List
        self.orders_scroll = ctk.CTkScrollableFrame(
            self,
            label_text="📦 Gelen Web Siparişleri & Müşteri Puanı Onayı (%1 × 100)",
            label_font=("Segoe UI", 13, "bold"),
        )
        self.orders_scroll.pack(fill="both", expand=True, padx=12, pady=(0, 8))

    def _copy_emails(self, sep: str = ", ") -> None:
        if not self._subscribers:
            self.on_toast("Henüz kayıtlı e-posta bulunmuyor.", "warning")
            return
        joined = sep.join(self._subscribers)
        try:
            self.clipboard_clear()
            self.clipboard_append(joined)
            self.update()
            self.on_toast(f"Toplam {len(self._subscribers)} adet e-posta kopyalandı!", "success")
        except Exception:
            self.on_toast("Kopyalama başarısız.", "error")

    def _approve_order(self, order_id: str) -> None:
        ok, msg = self.cloud_db.approve_web_order_points(order_id)
        if ok:
            self.on_toast(msg, "success")
        else:
            self.on_toast(msg, "error")
        self.refresh()

    def refresh(self) -> None:
        try:
            data = self.cloud_db.sync_and_get_web_orders_and_subscribers()
            self._subscribers = data.get("subscribers", [])
            self._orders = data.get("orders", [])

            # Update email textbox
            self.mail_title_lbl.configure(text=f"📧 Bültene Abone Olan & Üye E-Postaları ({len(self._subscribers)} Adet)")
            self.mail_textbox.configure(state="normal")
            self.mail_textbox.delete("1.0", "end")
            self.mail_textbox.insert("1.0", ", ".join(self._subscribers) if self._subscribers else "Henüz kayıtlı e-posta bulunmuyor.")
            self.mail_textbox.configure(state="disabled")

            # Render orders
            for w in self.orders_scroll.winfo_children():
                w.destroy()

            if not self._orders:
                ctk.CTkLabel(
                    self.orders_scroll,
                    text="Henüz web sitesinden gelen bir sipariş bulunmuyor.",
                    font=("Segoe UI", 12),
                    text_color="gray50",
                ).pack(pady=32)
                return

            for o in self._orders:
                c = o.get("customer") or {}
                oid = str(o.get("id") or "")
                is_app = bool(o.get("pointsApproved") or o.get("status") in ["Onaylandı", "Tamamlandı"])
                pts = int(o.get("earnedPoints") or 0)
                items_str = " | ".join(f"{i.get('qty', 1)}x {i.get('name', '')}" for i in (o.get("items") or []))
                full_addr = f"{c.get('address', '')} ({c.get('city', '')})"

                card = ctk.CTkFrame(
                    self.orders_scroll,
                    corner_radius=12,
                    border_width=2,
                    border_color="#10B981" if is_app else "#F59E0B",
                )
                card.pack(fill="x", padx=4, pady=5)

                top_bar = ctk.CTkFrame(card, fg_color="transparent")
                top_bar.pack(fill="x", padx=12, pady=(8, 4))

                info_str = f"#{oid} • 👤 {c.get('fullname', '-')} • 📞 {c.get('phone', '-')} • ✉️ {c.get('email') or o.get('email', '-')}"
                ctk.CTkLabel(
                    top_bar,
                    text=info_str,
                    font=("Segoe UI", 12, "bold"),
                ).pack(side="left")

                pay_badge = ctk.CTkLabel(
                    top_bar,
                    text=f"💳 {o.get('paymentMethod', 'Havale / EFT')}",
                    font=("Segoe UI", 10, "bold"),
                    text_color="#0284C7",
                    fg_color=("#E0F2FE", "#0C4A6E"),
                    corner_radius=6,
                    padx=6,
                    pady=2,
                )
                pay_badge.pack(side="left", padx=8)

                if is_app:
                    ctk.CTkLabel(
                        top_bar,
                        text=f"✅ Onaylandı (+{pts} Puan) • {o.get('total', '')}",
                        font=("Segoe UI", 12, "bold"),
                        text_color="#10B981",
                    ).pack(side="right")
                else:
                    ctk.CTkButton(
                        top_bar,
                        text=f"✅ Siparişi & +{pts} Puanı Onayla ({o.get('total', '')})",
                        font=("Segoe UI", 11, "bold"),
                        fg_color="#10B981",
                        hover_color="#059669",
                        height=28,
                        command=lambda _id=oid: self._approve_order(_id),
                    ).pack(side="right")

                details_text = f"📍 Adres: {full_addr}"
                if c.get("note"):
                    details_text += f" • Not: {c.get('note')}"
                details_text += f"\n🛒 Ürünler: {items_str}"

                ctk.CTkLabel(
                    card,
                    text=details_text,
                    font=("Segoe UI", 11),
                    justify="left",
                    anchor="w",
                ).pack(fill="x", padx=12, pady=(0, 8))

        except Exception as e:
            self.on_toast(f"Yükleme hatası: {e}", "error")

    def on_show(self) -> None:
        self.refresh()
