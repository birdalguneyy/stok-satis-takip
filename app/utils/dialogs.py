import tkinter.messagebox as messagebox


def confirm(title: str, message: str) -> bool:
    return messagebox.askyesno(title, message)
