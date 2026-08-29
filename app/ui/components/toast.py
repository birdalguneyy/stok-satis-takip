import customtkinter as ctk

from app.config import TOAST_DURATION_MS
from app.ui.theme import ERROR, FONT_BODY, SUCCESS, WARNING


class Toast(ctk.CTkFrame):
    def __init__(self, master, **kwargs) -> None:
        super().__init__(master, corner_radius=10, **kwargs)
        self.label = ctk.CTkLabel(self, text="", font=FONT_BODY, text_color="white")
        self.label.pack(padx=20, pady=12)
        self._after_id: str | None = None
        self.place(relx=0.5, rely=0.92, anchor="center")
        self.lift()
        self.place_forget()

    def show(self, message: str, level: str = "info") -> None:
        colors = {
            "success": SUCCESS,
            "error": ERROR,
            "warning": WARNING,
            "info": "#424242",
        }
        self.configure(fg_color=colors.get(level, colors["info"]))
        self.label.configure(text=message)
        self.place(relx=0.5, rely=0.92, anchor="center")
        self.lift()

        if self._after_id:
            self.after_cancel(self._after_id)
        self._after_id = self.after(TOAST_DURATION_MS, self.hide)

    def hide(self) -> None:
        self.place_forget()
        self._after_id = None
