import platform


def play_success_beep() -> None:
    """Başarılı barkod okuma ses bildirimi (Market kasa tipi bip sesi)."""
    try:
        if platform.system() == "Windows":
            import winsound

            winsound.Beep(2000, 90)
        else:
            print("\a", end="", flush=True)
    except Exception:
        pass


def play_error_beep() -> None:
    """Hatalı / Bulunamadı barkod okuma ses bildirimi."""
    try:
        if platform.system() == "Windows":
            import winsound

            winsound.Beep(600, 200)
        else:
            print("\a", end="", flush=True)
    except Exception:
        pass
