from typing import Callable, Optional
import customtkinter as ctk

from app.ui.theme import ACCENT, ACCENT_HOVER, ERROR, FONT_BODY, FONT_HEADING, FONT_SMALL

MASTER_PASSWORDS = {"0528", "05282024"}
NORMAL_PASSWORDS = {"28052024", "2805"}


class AccessPasswordDialog(ctk.CTkToplevel):
    """Güvenli bölümlere erişim için PIN / Şifre sorgulama modal penceresi.
    - Şifre: 28052024 veya 2805 (bölüme geçişe izin verir, sonraki geçişlerde şifre sormaya devam eder)
    - Master Şifre: 0528 veya 05282024 (tüm bölümlere şifresiz serbest geçiş modunu açar)
    """

    CORRECT_PASSWORD = "28052024"

    def __init__(
        self,
        parent,
        on_success: Callable[..., None],
        on_cancel: Optional[Callable[[], None]] = None,
        title: str = "🔐 Güvenlik Girişi",
        heading: str = "🔐 Güvenlik Girişi",
        prompt: str = "Lütfen erişim şifresini giriniz:",
    ) -> None:
        super().__init__(parent)
        self.on_success = on_success
        self.on_cancel = on_cancel
        self._title_text = title
        self._heading_text = heading
        self._prompt_text = prompt

        self.title(self._title_text)
        self.geometry("380x300")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        # Ekranın ortasında konumlandır
        self.update_idletasks()
        try:
            x = parent.winfo_x() + (parent.winfo_width() // 2) - 190
            y = parent.winfo_y() + (parent.winfo_height() // 2) - 150
            self.geometry(f"+{max(0, x)}+{max(0, y)}")
        except Exception:
            pass

        self._build_ui()

    def _build_ui(self) -> None:
        card = ctk.CTkFrame(self, corner_radius=16)
        card.pack(fill="both", expand=True, padx=16, pady=16)

        ctk.CTkLabel(
            card,
            text=self._heading_text,
            font=FONT_HEADING,
            text_color=ACCENT,
        ).pack(pady=(16, 4))

        ctk.CTkLabel(
            card,
            text=self._prompt_text,
            font=FONT_SMALL,
            text_color=("gray40", "gray60"),
        ).pack(pady=(0, 14))

        self.pass_entry = ctk.CTkEntry(
            card,
            placeholder_text="Şifre",
            show="*",
            font=("Segoe UI", 18, "bold"),
            justify="center",
            width=200,
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
        if entered in MASTER_PASSWORDS:
            self.destroy()
            if self.on_success:
                try:
                    self.on_success(True)
                except TypeError:
                    self.on_success()
        elif entered in NORMAL_PASSWORDS:
            self.destroy()
            if self.on_success:
                try:
                    self.on_success(False)
                except TypeError:
                    self.on_success()
        else:
            self.error_label.configure(text="❌ Hatalı şifre! Tekrar deneyiniz.")
            self.pass_entry.delete(0, "end")
            self.pass_entry.focus()

    def _cancel(self) -> None:
        self.destroy()
        if self.on_cancel:
            self.on_cancel()


# Geriye dönük uyumluluk için alias
FarmPasswordDialog = AccessPasswordDialog
