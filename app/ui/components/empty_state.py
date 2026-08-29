from typing import Callable, Optional

import customtkinter as ctk

from app.ui.theme import ACCENT, FONT_BODY, FONT_HEADING, FONT_SMALL


class EmptyState(ctk.CTkFrame):
    def __init__(
        self,
        master,
        message: str = "Kayıt bulunamadı.",
        hint: str = "",
        icon: str = "📦",
        action_text: Optional[str] = None,
        on_action: Optional[Callable[[], None]] = None,
        **kwargs,
    ) -> None:
        super().__init__(master, fg_color="transparent", **kwargs)
        self.on_action = on_action

        self.icon_label = ctk.CTkLabel(
            self,
            text=icon,
            font=(FONT_HEADING[0], 36),
            text_color=("gray60", "gray40"),
        )
        self.icon_label.pack(pady=(24, 6))

        self.message_label = ctk.CTkLabel(
            self,
            text=message,
            font=FONT_HEADING,
            text_color=("gray30", "gray80"),
        )
        self.message_label.pack(pady=(0, 4))

        self.hint_label = ctk.CTkLabel(
            self,
            text=hint,
            font=FONT_SMALL,
            text_color="gray",
        )
        if hint:
            self.hint_label.pack(pady=(0, 12))

        self.action_button = ctk.CTkButton(
            self,
            text=action_text or "",
            font=FONT_SMALL,
            fg_color=ACCENT,
            height=32,
            command=self._handle_action,
        )
        if action_text and on_action:
            self.action_button.pack(pady=(6, 24))

    def _handle_action(self) -> None:
        if self.on_action:
            self.on_action()

    def set_message(
        self,
        message: str,
        hint: str = "",
        icon: Optional[str] = None,
        action_text: Optional[str] = None,
        on_action: Optional[Callable[[], None]] = None,
    ) -> None:
        if icon:
            self.icon_label.configure(text=icon)
        self.message_label.configure(text=message)
        self.hint_label.configure(text=hint)
        if hint:
            self.hint_label.pack(pady=(0, 12))
        else:
            self.hint_label.pack_forget()

        if on_action:
            self.on_action = on_action

        if action_text and self.on_action:
            self.action_button.configure(text=action_text)
            self.action_button.pack(pady=(6, 24))
        else:
            self.action_button.pack_forget()

