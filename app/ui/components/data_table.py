from typing import Any, Callable, Dict, List, Optional

import customtkinter as ctk

from app.ui.components.empty_state import EmptyState
from app.ui.theme import ACCENT, FONT_BODY, FONT_SMALL


class DataTable(ctk.CTkScrollableFrame):
    def __init__(
        self,
        master,
        columns: List[str],
        column_weights: Optional[List[int]] = None,
        on_sort: Optional[Callable[[str], None]] = None,
        empty_message: str = "Kayıt bulunamadı.",
        empty_hint: str = "",
        empty_icon: str = "📦",
        empty_action_text: Optional[str] = None,
        on_empty_action: Optional[Callable[[], None]] = None,
        **kwargs,
    ) -> None:
        super().__init__(master, fg_color="transparent", **kwargs)
        self.columns = columns
        self.column_weights = column_weights
        self.on_sort = on_sort
        self._sort_column: Optional[str] = None
        self._sort_ascending = True
        self._rows: List[ctk.CTkFrame] = []
        self._selected_row_id: Optional[int] = None

        self.header = ctk.CTkFrame(self, fg_color=("gray85", "gray24"), corner_radius=8)
        self.header.pack(fill="x", pady=(0, 6))
        self._header_labels: List[ctk.CTkButton] = []

        for i, col in enumerate(columns):
            btn = ctk.CTkButton(
                self.header,
                text=col,
                font=(FONT_SMALL[0], 11, "bold"),
                fg_color="transparent",
                hover_color=("gray75", "gray32"),
                text_color=("gray20", "gray90"),
                anchor="w",
                command=lambda c=col: self._handle_sort(c),
            )
            btn.grid(row=0, column=i, sticky="ew", padx=4, pady=6)
            weight = self.column_weights[i] if self.column_weights and i < len(self.column_weights) else 1
            self.header.grid_columnconfigure(i, weight=weight, uniform="cols" if not self.column_weights else None)
            self._header_labels.append(btn)

        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.pack(fill="both", expand=True)

        self.empty_state = EmptyState(
            self.body,
            message=empty_message,
            hint=empty_hint,
            icon=empty_icon,
            action_text=empty_action_text,
            on_action=on_empty_action,
        )
        self.empty_state.pack_forget()

    def _handle_sort(self, column: str) -> None:
        if self._sort_column == column:
            self._sort_ascending = not self._sort_ascending
        else:
            self._sort_column = column
            self._sort_ascending = True

        for btn in self._header_labels:
            base = btn.cget("text").split(" ")[0].split("▲")[0].split("▼")[0]
            suffix = ""
            if base == column:
                suffix = " ▲" if self._sort_ascending else " ▼"
            btn.configure(text=f"{base}{suffix}")

        if self.on_sort:
            self.on_sort(column)

    def clear_rows(self) -> None:
        for row in self._rows:
            row.destroy()
        self._rows.clear()
        self._selected_row_id = None

    def show_empty(
        self,
        message: Optional[str] = None,
        hint: str = "",
        icon: Optional[str] = None,
        action_text: Optional[str] = None,
        on_action: Optional[Callable[[], None]] = None,
    ) -> None:
        if message:
            self.empty_state.set_message(
                message=message,
                hint=hint,
                icon=icon,
                action_text=action_text,
                on_action=on_action,
            )
        self.empty_state.pack(fill="both", expand=True)

    def hide_empty(self) -> None:
        self.empty_state.pack_forget()

    def add_row(
        self,
        values: List[Any],
        row_id: Optional[int] = None,
        fg_color: Optional[tuple | str] = None,
        text_colors: Optional[List[str]] = None,
        custom_widgets: Optional[Dict[int, ctk.CTkBaseClass]] = None,
    ) -> ctk.CTkFrame:
        self.hide_empty()
        # Zebra striping: alternate colors for better readability
        index = len(self._rows)
        default_fg = ("gray96", "gray18") if index % 2 == 0 else ("gray90", "gray22")
        bg_color = fg_color or default_fg

        row_frame = ctk.CTkFrame(
            self.body,
            fg_color=bg_color,
            corner_radius=6,
        )
        row_frame.default_fg = bg_color
        row_frame.pack(fill="x", pady=2)
        if row_id is not None:
            row_frame.row_id = row_id

        for i, val in enumerate(values):
            weight = self.column_weights[i] if self.column_weights and i < len(self.column_weights) else 1
            row_frame.grid_columnconfigure(i, weight=weight, uniform="cols" if not self.column_weights else None)

            if custom_widgets and i in custom_widgets:
                widget = custom_widgets[i]
                widget.grid(row=0, column=i, sticky="ew", padx=6, pady=6)
            else:
                color = text_colors[i] if text_colors and i < len(text_colors) else None
                label = ctk.CTkLabel(
                    row_frame,
                    text=str(val),
                    font=FONT_BODY,
                    anchor="w",
                    text_color=color if color else ("gray10", "gray90"),
                )
                label.grid(row=0, column=i, sticky="ew", padx=8, pady=8)

        self._rows.append(row_frame)
        return row_frame

    def set_selected_row(self, row_id: Optional[int]) -> None:
        self._selected_row_id = row_id
        selected_fg = ("#BBDEFB", "#1E3A5F")
        for row in self._rows:
            if getattr(row, "row_id", None) == row_id and row_id is not None:
                row.configure(fg_color=selected_fg)
            else:
                default_fg = getattr(row, "default_fg", ("gray92", "gray18"))
                row.configure(fg_color=default_fg)

    @property
    def selected_row_id(self) -> Optional[int]:
        return self._selected_row_id

    @property
    def sort_column(self) -> Optional[str]:
        return self._sort_column

    @property
    def sort_ascending(self) -> bool:
        return self._sort_ascending

