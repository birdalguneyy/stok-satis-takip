import logging
import sys
from app.ui.app import App
from app.web.web_server import get_local_ip, run_web_server_in_thread

logging.basicConfig(level=logging.INFO)

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def main() -> None:
    # 7/24 Mobil Web & AI Sunucusunu arka plan daemon thread'lerinde başlatıyoruz
    run_web_server_in_thread(host="0.0.0.0", port=5000)

    local_ip = get_local_ip()
    print("\n" + "=" * 60)
    print("7/24 MOBIL WEB & YAPAY ZEKA (AI) SUNUCUSU CALISIYOR")
    print(f"Bilgisayar Tarayici Adresi      : http://localhost:5000")
    print(f"Telefon Baglanti Adresi (HTTP)  : http://{local_ip}:5000")
    print(f"Telefon Baglanti Adresi (HTTPS) : https://{local_ip}:5001")
    print("=" * 60 + "\n")

    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
