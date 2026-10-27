from typing import Callable, Optional

import customtkinter as ctk

from app.ui.theme import ACCENT, ACCENT_HOVER, FONT_BODY, FONT_HEADING, FONT_SMALL

NAV_ITEMS = [
    ("dashboard", "Gösterge Paneli"),
    ("products", "Stok Yönetimi"),
    ("sales", "Satış (POS)"),
    ("reports", "Raporlar & AI"),
]


class Sidebar(ctk.CTkFrame):
    def __init__(
        self,
        master,
        on_navigate: Callable[[str], None],
        on_toggle_theme: Callable[[], None],
        **kwargs,
    ) -> None:
        super().__init__(master, width=220, corner_radius=0, **kwargs)
        self.pack_propagate(False)
        self.on_navigate = on_navigate
        self._buttons: dict[str, ctk.CTkButton] = {}
        self._active = "sales"

        title = ctk.CTkLabel(
            self,
            text="Stok Takip",
            font=FONT_HEADING,
            text_color=ACCENT,
        )
        title.pack(padx=20, pady=(20, 4), anchor="w")

        self.role_badge = ctk.CTkLabel(
            self,
            text="👤 Personel Modu",
            font=(FONT_SMALL[0], 10, "bold"),
            text_color="#F59E0B",
            anchor="w",
        )
        self.role_badge.pack(padx=20, pady=(0, 16), anchor="w")

        self.nav_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.nav_frame.pack(fill="x", padx=12)

        for key, label in NAV_ITEMS:
            btn = ctk.CTkButton(
                self.nav_frame,
                text=label,
                font=FONT_BODY,
                anchor="w",
                height=40,
                fg_color="transparent",
                text_color=("gray20", "gray90"),
                hover_color=("gray85", "gray30"),
                command=lambda k=key: self._select(k),
            )
            btn.pack(fill="x", pady=2)
            self._buttons[key] = btn

        self.theme_btn = ctk.CTkButton(
            self,
            text="Tema Değiştir",
            font=FONT_SMALL,
            height=36,
            fg_color=("gray80", "gray30"),
            hover_color=("gray70", "gray35"),
            command=on_toggle_theme,
        )
        self.theme_btn.pack(side="bottom", fill="x", padx=16, pady=20)

        # Mobile Web info badge
        try:
            from app.web.web_server import get_local_ip

            mobile_url = f"http://{get_local_ip()}:5000"
            m_badge = ctk.CTkFrame(self, fg_color=("gray90", "gray20"), corner_radius=8)
            m_badge.pack(side="bottom", fill="x", padx=12, pady=(0, 4))

            ctk.CTkLabel(
                m_badge,
                text="🌐 7/24 Mobil & AI Adresi:",
                font=(FONT_SMALL[0], 10, "bold"),
                text_color=ACCENT,
            ).pack(anchor="w", padx=8, pady=(6, 2))

            ctk.CTkLabel(
                m_badge,
                text=mobile_url,
                font=(FONT_SMALL[0], 9),
                text_color=("gray30", "gray70"),
            ).pack(anchor="w", padx=8, pady=(0, 6))
        except Exception:
            pass

        self.set_active("sales")

    def _select(self, key: str) -> None:
        self.on_navigate(key)

    def set_active(self, key: str) -> None:
        self._active = key
        for nav_key, btn in self._buttons.items():
            if nav_key == key:
                btn.configure(fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color="white")
            else:
                btn.configure(
                    fg_color="transparent",
                    hover_color=("gray85", "gray30"),
                    text_color=("gray20", "gray90"),
                )

    def set_role(self, role: str) -> None:
        """Kullanıcı rolüne göre menü öğelerini gösterir veya gizler.
        'personnel' (2805): Sadece 'sales' (POS) görünür, diğer tüm yerler gizlenir.
        'admin' (0528): Tüm menü öğeleri görünür.
        """
        for btn in self._buttons.values():
            btn.pack_forget()

        for key, _label in NAV_ITEMS:
            if role == "personnel":
                if key == "sales":
                    self._buttons[key].pack(fill="x", pady=2)
            else:
                self._buttons[key].pack(fill="x", pady=2)

        if hasattr(self, "role_badge"):
            if role == "personnel":
                self.role_badge.configure(text="👤 Personel Modu (Kısıtlı)", text_color="#F59E0B")
            else:
                self.role_badge.configure(text="👑 Yönetici Modu (Tam Yetki)", text_color="#10B981")
