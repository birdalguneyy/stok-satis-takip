from typing import Callable, List, Optional

import customtkinter as ctk

from app.ui.theme import ACCENT, FONT_BODY, FONT_SMALL


class BarcodeEntry(ctk.CTkFrame):
    """Barkod okuyucu uyumlu giriş kutusu — otomatik odak + Enter ile submit."""

    def __init__(
        self,
        master,
        placeholder: str = "Barkod okut veya ürün ara...",
        on_submit: Optional[Callable[[str], None]] = None,
        **kwargs,
    ) -> None:
        super().__init__(master, fg_color="transparent", **kwargs)
        self.on_submit = on_submit

        self.entry = ctk.CTkEntry(
            self,
            placeholder_text=placeholder,
            height=48,
            font=FONT_BODY,
            border_color=ACCENT,
            border_width=2,
        )
        self.entry.pack(fill="x", expand=True)
        self.entry.bind("<Return>", self._handle_return)

    def _handle_return(self, _event=None) -> None:
        value = self.entry.get().strip()
        if value and self.on_submit:
            self.on_submit(value)
        self.clear_and_focus()

    def clear_and_focus(self) -> None:
        self.entry.delete(0, "end")
        self.focus()

    def focus(self) -> None:
        self.entry.focus_set()

    def set_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.entry.configure(state=state)
