import os
import subprocess
import sys


def build() -> None:
    print("=" * 60)
    print("Stok ve Satis Takip - PyInstaller Tek .exe Derleyici")
    print("=" * 60)

    req_file = os.path.join(os.path.dirname(__file__), "requirements.txt")
    if os.path.exists(req_file):
        print("Bagimliliklar kontrol ediliyor...")
        subprocess.run([sys.executable, "-m", "pip", "install", "-r", req_file], check=True)

    spec_file = os.path.join(os.path.dirname(__file__), "stok_satis_takip.spec")
    print(f"Spec dosyasi derleniyor: {spec_file}")
    
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        spec_file,
    ]
    
    result = subprocess.run(cmd)
    if result.returncode == 0:
        exe_path = os.path.abspath(os.path.join("dist", "StokSatisTakip.exe"))
        print("\n" + "=" * 60)
        print("[SUCCESS] Derleme Basariyla Tamamlandi!")
        print(f"Calistirilabilir .exe dosyasi: {exe_path}")
        print("=" * 60)
    else:
        print("\n[ERROR] Derleme sirasinda bir hata olustu.")
        sys.exit(result.returncode)


if __name__ == "__main__":
    build()


