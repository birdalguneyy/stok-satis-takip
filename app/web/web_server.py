import logging
import os
import socket
import threading
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from flask import Flask, jsonify, request, send_from_directory

from app.config import DATA_DIR
from app.database.cloud_db import CloudDatabase
from app.services.ai_package_service import AIPackageService
from app.utils.camera import decode_barcode_from_frame

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
UPLOADS_DIR = DATA_DIR / "uploads" / "products"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, static_folder=str(STATIC_DIR), template_folder=str(STATIC_DIR))
cloud_db = CloudDatabase()
ai_service = AIPackageService()


def get_local_ip() -> str:
    """Yerel ağ (Wi-Fi) IP adresini tespit eder."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ════════════════════════════════════════════════════════════════════
# WEB ROUTES & API ENDPOINTS
# ════════════════════════════════════════════════════════════════════
@app.route("/")
def index():
    return send_from_directory(str(STATIC_DIR), "index.html")


@app.route("/manifest.json")
def serve_manifest():
    return send_from_directory(str(STATIC_DIR), "manifest.json")


@app.route("/sw.js")
def serve_sw():
    return send_from_directory(str(STATIC_DIR), "sw.js")


@app.route("/static/<path:filename>")
def serve_static_file(filename):
    return send_from_directory(str(STATIC_DIR), filename)


@app.route("/uploads/products/<path:filename>")
def serve_upload(filename):
    return send_from_directory(str(UPLOADS_DIR), filename)


@app.route("/api/info")
def api_info():
    port = int(os.environ.get("PORT", 5000))
    return jsonify({
        "ip": get_local_ip(),
        "port": port,
        "app": "Stok & Satış Takip 7/24 Bulut Mobil Web",
        "has_ai": bool(ai_service.api_key and ai_service.client),
    })


@app.route("/api/settings/gemini-key", methods=["POST"])
def save_gemini_key():
    data = request.json or {}
    key = data.get("key", "").strip()
    if not key:
        return jsonify({"ok": False, "message": "API Key boş olamaz!"}), 400
    ok = ai_service.save_api_key(key)
    return jsonify({"ok": ok, "message": "✨ Gemini Yapay Zeka API Key başarıyla kaydedildi!" if ok else "Kaydetme hatası!"})


@app.route("/api/auth/register", methods=["POST"])
def auth_register():
    data = request.json or {}
    company_name = data.get("company_name", "").strip()
    full_name = data.get("full_name", "").strip()
    phone = data.get("phone", "").strip()
    email = data.get("email", "").strip()
    password = data.get("password", "").strip()

    ok, msg, user = cloud_db.register_user(
        company_name=company_name,
        full_name=full_name,
        phone=phone,
        email=email,
        password=password,
    )
    if not ok:
        return jsonify({"ok": False, "message": msg}), 400
    return jsonify({"ok": True, "message": msg, "user": user})


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    data = request.json or {}
    phone_or_email = data.get("phone_or_email", "").strip()
    password = data.get("password", "").strip()
    remember_me = bool(data.get("remember_me", True))

    ok, msg, user = cloud_db.login_user(
        phone_or_email=phone_or_email,
        password=password,
        remember_me=remember_me,
    )
    if not ok:
        return jsonify({"ok": False, "message": msg}), 401
    return jsonify({"ok": True, "message": msg, "user": user})


@app.route("/api/auth/me", methods=["GET"])
def auth_me():
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if not token:
        token = request.args.get("token", "").strip()
    user = cloud_db.verify_token(token)
    if user:
        return jsonify({"authenticated": True, "user": user})
    return jsonify({"authenticated": False, "message": "Oturum geçersiz"}), 401


def get_current_user_id() -> Optional[int]:
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if not token:
        token = request.args.get("token", "").strip()
    if token:
        user = cloud_db.verify_token(token)
        if user:
            return user.get("id")
    return None


@app.route("/api/categories")
def get_categories():
    user_id = get_current_user_id()
    return jsonify(cloud_db.get_categories(user_id=user_id))


@app.route("/api/products", methods=["GET"])
def get_products():
    user_id = get_current_user_id()
    search = request.args.get("search", "")
    products = cloud_db.get_products(user_id=user_id, search=search)
    return jsonify(products)


@app.route("/api/products/barcode/<barcode>")
def get_product_by_barcode(barcode):
    user_id = get_current_user_id()
    code_clean = barcode.strip()
    prod = cloud_db.get_product_by_barcode(code_clean, user_id=user_id)
    if not prod:
        # Fallback to searching products by ID, name, or code for this user
        products = cloud_db.get_products(user_id=user_id, search=code_clean)
        if products:
            prod = products[0]
    if prod:
        return jsonify({"found": True, "product": prod})
    return jsonify({"found": False, "message": f"'{code_clean}' kodlu veya isimli ürün bulunamadı"}), 404


@app.route("/api/products", methods=["POST"])
def save_product():
    user_id = get_current_user_id()
    data = request.json or {}
    name = data.get("name", "").strip()
    barcode = data.get("barcode", "").strip()
    category = data.get("category", "Genel").strip()
    purchase_price = float(data.get("purchase_price", 0))
    sale_price = float(data.get("sale_price", 0))
    stock_quantity = int(data.get("stock_quantity", 0))
    critical_stock = int(data.get("critical_stock_level", 5))
    image_path = data.get("image_path")
    product_id = data.get("id")

    if not name or not barcode:
        return jsonify({"ok": False, "message": "Ürün adı ve barkod zorunludur!"}), 400

    ok, msg, prod = cloud_db.save_product(
        name=name,
        barcode=barcode,
        category_name=category,
        purchase_price=purchase_price,
        sale_price=sale_price,
        stock_quantity=stock_quantity,
        critical_stock_level=critical_stock,
        image_path=image_path,
        product_id=product_id,
        user_id=user_id,
    )
    return jsonify({"ok": ok, "message": msg, "product": prod})


@app.route("/api/sales", methods=["POST"])
def record_sale():
    user_id = get_current_user_id()
    data = request.json or {}
    items = data.get("items", [])
    note = data.get("note", "Mobil Satış")
    if not items:
        return jsonify({"ok": False, "message": "Sepet boş!"}), 400

    ok, msg = cloud_db.add_sale(items, note=note, user_id=user_id)
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/sales/history", methods=["GET"])
def get_sales_history():
    user_id = get_current_user_id()
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    history = cloud_db.get_sales_history(user_id=user_id, start_date=start_date, end_date=end_date)
    return jsonify({"ok": True, "sales": history})


@app.route("/api/sales/analytics", methods=["GET"])
def get_sales_analytics():
    user_id = get_current_user_id()
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    analytics = cloud_db.get_sales_analytics(user_id=user_id, start_date=start_date, end_date=end_date)
    hours = cloud_db.get_store_hours(user_id=user_id)
    return jsonify({"ok": True, "analytics": analytics, "store_hours": hours})


@app.route("/api/expenses", methods=["GET", "POST"])
def handle_expenses():
    user_id = get_current_user_id()
    if request.method == "POST":
        data = request.json or {}
        title = data.get("title", "")
        amount = float(data.get("amount", 0))
        category = data.get("category", "Fatura")
        expense_date = data.get("expense_date")
        note = data.get("note", "")

        ok, msg, exp = cloud_db.add_expense(
            title=title,
            amount=amount,
            category=category,
            expense_date=expense_date,
            note=note,
            user_id=user_id,
        )
        return jsonify({"ok": ok, "message": msg, "expense": exp})

    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    expenses = cloud_db.get_expenses(user_id=user_id, start_date=start_date, end_date=end_date)
    return jsonify({"ok": True, "expenses": expenses})


@app.route("/api/ai/analyze-sales", methods=["POST"])
def api_analyze_sales():
    user_id = get_current_user_id()
    start_date = request.json.get("start_date") if request.json else None
    end_date = request.json.get("end_date") if request.json else None
    analytics = cloud_db.get_sales_analytics(user_id=user_id, start_date=start_date, end_date=end_date)
    hours = cloud_db.get_store_hours(user_id=user_id)
    res = ai_service.analyze_sales_and_business(analytics, hours)
    return jsonify(res)


@app.route("/api/settings/store-hours", methods=["GET", "POST"])
def handle_store_hours():
    user_id = get_current_user_id()
    if request.method == "POST":
        data = request.json or {}
        weekday = data.get("weekday", "08:00 - 22:00")
        weekend = data.get("weekend", "09:00 - 23:00")
        cloud_db.save_store_hours(weekday, weekend, user_id=user_id)
        return jsonify({"ok": True, "message": "İşletme çalışma saatleri başarıyla kaydedildi."})

    hours = cloud_db.get_store_hours(user_id=user_id)
    return jsonify({"ok": True, "store_hours": hours})


@app.route("/api/barcode/decode", methods=["POST"])
def api_decode_barcode():
    """Yüklenen yüksek çözünürlüklü fotoğraftan pyzbar + zxingcpp ile 7 aşamalı barkod çözer."""
    if "file" not in request.files:
        return jsonify({"ok": False, "message": "Resim bulunamadı!"}), 400

    file = request.files["file"]
    image_bytes = file.read()
    try:
        np_arr = np.frombuffer(image_bytes, np.uint8)
        frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
        if frame is not None:
            results = decode_barcode_from_frame(frame)
            if results:
                return jsonify({"ok": True, "barcode": results[0][0]})
    except Exception as exc:
        logger.error(f"Mobil barkod çözme hatası: {exc}")

    return jsonify({"ok": False, "message": "Barkod algılanamadı! Lütfen barkoda biraz daha odaklanarak tekrar çekin."}), 404


@app.route("/api/ai/scan-package", methods=["POST"])
def ai_scan_package():
    """Mobil kameradan yüklenen ambalaj fotoğrafını Yapay Zeka (Gemini Vision) ile analiz eder."""
    if "file" not in request.files:
        return jsonify({"ok": False, "message": "Resim dosyası gönderilmedi!"}), 400

    file = request.files["file"]
    image_bytes = file.read()
    filename = file.filename or "package.jpg"
    ext = Path(filename).suffix.lower() or ".jpg"

    ok, msg, ai_data = ai_service.analyze_packaging_and_save_image(image_bytes, file_extension=ext)
    return jsonify({"ok": ok, "message": msg, "data": ai_data})


def run_web_server_in_thread(host: str = "0.0.0.0", port: Optional[int] = None) -> list:
    """Flask Web sunucusunu HTTP (Port 5000) ve HTTPS (Port 5001) modlarında çalıştırır."""
    server_port = port or int(os.environ.get("PORT", 5000))
    cert_path = DATA_DIR / "cert.pem"
    key_path = DATA_DIR / "key.pem"

    threads = []

    # 1. Standart HTTP Sunucusu (Port 5000 - PC ve Mobil için Kolay Erişim)
    t_http = threading.Thread(
        target=lambda: app.run(host=host, port=server_port, debug=False, use_reloader=False),
        daemon=True,
    )
    t_http.start()
    threads.append(t_http)
    logger.info(f"7/24 Mobil Web Sunucusu (HTTP) çalışıyor: http://{get_local_ip()}:{server_port}")

    # 2. Opsiyonel HTTPS Sunucusu (Port 5001 - Mobil Canlı Kamera & PWA için)
    if cert_path.exists() and key_path.exists():
        try:
            ssl_port = server_port + 1
            ssl_ctx = (str(cert_path), str(key_path))
            t_https = threading.Thread(
                target=lambda: app.run(host=host, port=ssl_port, ssl_context=ssl_ctx, debug=False, use_reloader=False),
                daemon=True,
            )
            t_https.start()
            threads.append(t_https)
            logger.info(f"7/24 Mobil Web Sunucusu (HTTPS) çalışıyor: https://{get_local_ip()}:{ssl_port}")
        except Exception as exc:
            logger.warning(f"HTTPS sunucusu başlatılamadı: {exc}")

    return threads


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
