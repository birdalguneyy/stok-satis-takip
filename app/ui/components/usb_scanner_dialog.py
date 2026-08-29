import time
from typing import Optional

import customtkinter as ctk

from app.ui.theme import ACCENT, ERROR, FONT_BODY, FONT_HEADING, FONT_SMALL, SUCCESS
from app.utils.sound import play_error_beep, play_success_beep


class USBScannerDialog(ctk.CTkToplevel):
    """USB Barkod Okuyucu donanımını test etmek ve doğrulama yapmak için teşhis penceresi."""

    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.title("🔌 USB Barkod Okuyucu Testi")
        self.geometry("520x440")
        self.resizable(False, False)

        # Make modal & stay on top
        self.transient(parent)
        self.grab_set()

        self.start_time: Optional[float] = None
        self.char_times: list[float] = []

        self._build_ui()
        self.after(200, self.test_entry.focus_set)

    def _build_ui(self) -> None:
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(16, 8))

        ctk.CTkLabel(
            header,
            text="USB Barkod Okuyucu Test Paneli",
            font=FONT_HEADING,
        ).pack(anchor="w")

        ctk.CTkLabel(
            header,
            text="Barkod okuyucunuz USB klavye (HID) olarak çalışır. Aşağıdaki kutuya herhangi bir ürün barkodu okutun.",
            font=FONT_SMALL,
            text_color=("gray30", "gray70"),
            wraplength=460,
            justify="left",
        ).pack(anchor="w", pady=(4, 0))

        # Test Entry box
        self.test_entry = ctk.CTkEntry(
            self,
            placeholder_text="Buraya barkod okutun...",
            height=44,
            font=FONT_BODY,
            border_color=ACCENT,
            border_width=2,
        )
        self.test_entry.pack(fill="x", padx=20, pady=12)
        self.test_entry.bind("<Key>", self._on_key_press)
        self.test_entry.bind("<Return>", self._on_return_scanned)

        # Results Frame
        self.result_card = ctk.CTkFrame(self, corner_radius=10, fg_color=("gray90", "gray20"))
        self.result_card.pack(fill="both", expand=True, padx=20, pady=(0, 12))

        self.status_label = ctk.CTkLabel(
            self.result_card,
            text="⏳ Barkod okutulması bekleniyor...",
            font=FONT_BODY,
            text_color=("gray40", "gray60"),
        )
        self.status_label.pack(anchor="w", padx=16, pady=(12, 6))

        self.detail_barcode = ctk.CTkLabel(
            self.result_card,
            text="• Okunan Değer: -",
            font=FONT_SMALL,
            anchor="w",
        )
        self.detail_barcode.pack(anchor="w", padx=16, pady=2)

        self.detail_speed = ctk.CTkLabel(
            self.result_card,
            text="• Okuma Hızı: -",
            font=FONT_SMALL,
            anchor="w",
        )
        self.detail_speed.pack(anchor="w", padx=16, pady=2)

        self.detail_enter = ctk.CTkLabel(
            self.result_card,
            text="• Sonlandırıcı (Enter): -",
            font=FONT_SMALL,
            anchor="w",
        )
        self.detail_enter.pack(anchor="w", padx=16, pady=2)

        # Tips label
        self.tip_label = ctk.CTkLabel(
            self.result_card,
            text="💡 İpucu: Okuma sonrasında biip sesi duyuyorsanız cihazınız hazır demektir.",
            font=FONT_SMALL,
            text_color=("gray50", "gray50"),
            wraplength=440,
            justify="left",
        )
        self.tip_label.pack(anchor="w", padx=16, pady=(8, 12))

        # Bottom Action Bar
        action_bar = ctk.CTkFrame(self, fg_color="transparent")
        action_bar.pack(fill="x", padx=20, pady=(0, 16))

        ctk.CTkButton(
            action_bar,
            text="🔊 Bip Sesini Test Et",
            font=FONT_SMALL,
            fg_color=("gray75", "gray35"),
            hover_color=ACCENT,
            command=self._test_sound,
            width=150,
        ).pack(side="left")

        ctk.CTkButton(
            action_bar,
            text="Temizle ve Yeniden Dene",
            font=FONT_SMALL,
            command=self._reset_test,
            width=160,
        ).pack(side="right", padx=(8, 0))

    def _on_key_press(self, event) -> None:
        now = time.time()
        if not self.start_time:
            self.start_time = now
        self.char_times.append(now)

    def _on_return_scanned(self, _event=None) -> None:
        scanned_val = self.test_entry.get().strip()
        now = time.time()

        if not scanned_val:
            return

        play_success_beep()

        duration_ms = 0
        if self.start_time and len(self.char_times) > 1:
            duration_ms = int((now - self.start_time) * 1000)

        speed_text = (
            f"{duration_ms} ms (Yüksek Hızlı - Otomatik Okuyucu)"
            if duration_ms < 400
            else f"{duration_ms} ms (Manuel Yazım veya Yavaş Cihaz)"
        )

        self.status_label.configure(
            text="✅ USB Barkod Okuyucu Başarıyla Algılandı!",
            text_color=SUCCESS,
        )
        self.detail_barcode.configure(text=f"• Okunan Değer: '{scanned_val}' ({len(scanned_val)} karakter)")
        self.detail_speed.configure(text=f"• Okuma Hızı: {speed_text}")
        self.detail_enter.configure(text="• Sonlandırıcı (Enter): ✅ Algılandı (Return Key)")

        self.tip_label.configure(
            text="✅ Cihazınız hazır! Satış ekranında ürün barkodunu okutarak anında sepete ekleyebilirsiniz."
        )

    def _test_sound(self) -> None:
        play_success_beep()
        self.after(200, play_error_beep)

    def _reset_test(self) -> None:
        self.test_entry.delete(0, "end")
        self.start_time = None
        self.char_times.clear()
        self.status_label.configure(
            text="⏳ Barkod okutulması bekleniyor...",
            text_color=("gray40", "gray60"),
        )
        self.detail_barcode.configure(text="• Okunan Değer: -")
        self.detail_speed.configure(text="• Okuma Hızı: -")
        self.detail_enter.configure(text="• Sonlandırıcı (Enter): -")
        self.test_entry.focus_set()
