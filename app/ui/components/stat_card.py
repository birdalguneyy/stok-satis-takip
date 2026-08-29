import customtkinter as ctk

from app.ui.theme import ACCENT, FONT_BODY, FONT_HEADING, FONT_SMALL


class StatCard(ctk.CTkFrame):
    def __init__(
        self,
        master,
        title: str,
        value: str = "0",
        accent: str = ACCENT,
        **kwargs,
    ) -> None:
        super().__init__(master, corner_radius=12, **kwargs)
        self.accent_bar = ctk.CTkFrame(self, width=4, corner_radius=2, fg_color=accent)
        self.accent_bar.pack(side="left", fill="y", padx=(12, 0), pady=12)

        content = ctk.CTkFrame(self, fg_color="transparent")
        content.pack(side="left", fill="both", expand=True, padx=16, pady=16)

        self.title_label = ctk.CTkLabel(content, text=title, font=FONT_SMALL, text_color="gray")
        self.title_label.pack(anchor="w")

        self.value_label = ctk.CTkLabel(content, text=value, font=FONT_HEADING)
        self.value_label.pack(anchor="w", pady=(4, 0))

    def set_value(self, value: str) -> None:
        self.value_label.configure(text=value)
