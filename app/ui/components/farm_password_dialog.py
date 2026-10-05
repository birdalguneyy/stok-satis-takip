from typing import Callable, Optional
import customtkinter as ctk

from app.ui.theme import ACCENT, ACCENT_HOVER, ERROR, FONT_BODY, FONT_HEADING, FONT_SMALL


class FarmPasswordDialog(ctk.CTkToplevel):
    """Çiftlik Modülü için 4 haneli PIN / Şifre sorgulama modal penceresi (Şifre: 2805)."""

    CORRECT_PASSWORD = "2805"

    def __init__(
        self,
        parent,
        on_success: Callable[[], None],
        on_cancel: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(parent)
        self.on_success = on_success
        self.on_cancel = on_cancel

        self.title("🔐 Çiftlik Girişi")
        self.geometry("360x280")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        # Ekranın ortasında konumlandır
        self.update_idletasks()
        try:
            x = parent.winfo_x() + (parent.winfo_width() // 2) - 180
            y = parent.winfo_y() + (parent.winfo_height() // 2) - 140
            self.geometry(f"+max(0, {x})+max(0, {y})")
        except Exception:
            pass

        self._build_ui()

    def _build_ui(self) -> None:
        card = ctk.CTkFrame(self, corner_radius=16)
        card.pack(fill="both", expand=True, padx=16, pady=16)

        ctk.CTkLabel(
            card,
            text="🚜 Çiftlik Modülü Girişi",
            font=FONT_HEADING,
            text_color=ACCENT,
        ).pack(pady=(16, 4))

        ctk.CTkLabel(
            card,
            text="Lütfen 4 haneli erişim şifresini giriniz:",
            font=FONT_SMALL,
            text_color=("gray40", "gray60"),
        ).pack(pady=(0, 14))

        self.pass_entry = ctk.CTkEntry(
            card,
            placeholder_text="Şifre",
            show="*",
            font=("Segoe UI", 18, "bold"),
            justify="center",
            width=180,
            height=44,
        )
        self.pass_entry.pack(pady=8)
        self.pass_entry.focus()
        self.pass_entry.bind("<Return>", lambda e: self._verify())

        self.error_label = ctk.CTkLabel(
            card,
            text="",
            font=FONT_SMALL,
            text_color=ERROR,
        )
        self.error_label.pack(pady=4)

        btn_row = ctk.CTkFrame(card, fg_color="transparent")
        btn_row.pack(fill="x", padx=20, pady=(10, 16))

        cancel_btn = ctk.CTkButton(
            btn_row,
            text="İptal",
            font=FONT_BODY,
            fg_color=("gray75", "gray30"),
            hover_color=ERROR,
            width=100,
            height=36,
            command=self._cancel,
        )
        cancel_btn.pack(side="left", padx=4)

        submit_btn = ctk.CTkButton(
            btn_row,
            text="Giriş Yap",
            font=FONT_BODY,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            width=130,
            height=36,
            command=self._verify,
        )
        submit_btn.pack(side="right", padx=4)

        self.bind("<Escape>", lambda e: self._cancel())

    def _verify(self) -> None:
        entered = self.pass_entry.get().strip()
        if entered == self.CORRECT_PASSWORD:
            self.destroy()
            if self.on_success:
                self.on_success()
        else:
            self.error_label.configure(text="❌ Hatalı şifre! Tekrar deneyiniz.")
            self.pass_entry.delete(0, "end")
            self.pass_entry.focus()

    def _cancel(self) -> None:
        self.destroy()
        if self.on_cancel:
            self.on_cancel()
