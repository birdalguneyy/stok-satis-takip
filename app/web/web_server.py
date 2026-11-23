import logging
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import cv2
except ImportError:
    cv2 = None

try:
    import numpy as np
except ImportError:
    np = None

from flask import Flask, jsonify, request, send_from_directory


import hashlib
from app.config import (
    DATA_DIR,
    GATE_PIN,
    HIGH_AMOUNT_WARNING_THRESHOLD,
    STAFF_PIN,
    get_turkey_now_str,
)
from app.database.cloud_db import CloudDatabase
from app.services.ai_package_service import AIPackageService
from app.services.forecast_service import ForecastService
from app.services.push_service import (
    get_vapid_keys,
    remove_subscription,
    save_subscription,
    send_push_to_all,
    send_sale_notification_async,
)
from app.services.security_service import (
    CANCEL_REASONS,
    ROLE_CALISAN,
    ROLE_PATRON,
    build_actor,
    get_audit_logs,
    get_permissions_for_role,
    has_permission,
    log_audit,
    log_unauthorized_attempt,
    normalize_role,
    validate_cancel_reason,
    validate_cash_description,
    validate_monetary_amount,
)
from app.utils.camera import decode_barcode_from_frame, is_valid_barcode

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
UPLOADS_DIR = DATA_DIR / "uploads" / "products"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, static_folder=str(STATIC_DIR), template_folder=str(STATIC_DIR))
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
cloud_db = CloudDatabase()
ai_service = AIPackageService()
forecast_service = ForecastService(cloud_db)

GATE_SECRET = "stok_satis_gate_key_2026"


def make_gate_token() -> str:
    return hashlib.sha256(f"{GATE_PIN}:{GATE_SECRET}".encode("utf-8")).hexdigest()


def make_staff_gate_token() -> str:
    return hashlib.sha256(f"STAFF:{STAFF_PIN}:{GATE_SECRET}".encode("utf-8")).hexdigest()


EXPECTED_GATE_TOKEN = make_gate_token()
EXPECTED_STAFF_GATE_TOKEN = make_staff_gate_token()

# IP Brute-Force & Bot Koruması
failed_pin_attempts: Dict[str, List[float]] = {}
blocked_ips: Dict[str, float] = {}

DISALLOWED_BOT_KEYWORDS = (
    "gptbot", "chatgpt", "google-extended", "ccbot", "anthropic",
    "claudebot", "claude-web", "bytespider", "perplexity", "facebookbot",
    "facebookexternalhit", "meta-externalagent", "cohere-ai", "diffbot",
    "amazonbot", "omgilibot", "scrapy", "python-requests", "aiohttp",
    "httpx", "headlesschrome", "phantomjs", "puppeteer", "selenium",
    "postmanruntime", "sqlmap", "nikto", "semrushbot", "ahrefsbot",
    "mj12bot", "dotbot", "petalbot", "zoominfobot", "dataforseo", "youbot",
    "applebot-extended"
)


def get_client_ip() -> str:
    xfwd = request.headers.get("X-Forwarded-For", "").strip()
    if xfwd:
        return xfwd.split(",")[0].strip()
    return (request.remote_addr or "127.0.0.1").strip()


def is_ip_blocked(ip: str) -> bool:
    now = time.time()
    unblock_time = blocked_ips.get(ip)
    if unblock_time:
        if now < unblock_time:
            return True
        else:
            blocked_ips.pop(ip, None)
            failed_pin_attempts.pop(ip, None)
    return False


def record_failed_pin_attempt(ip: str) -> int:
    now = time.time()
    attempts = [t for t in failed_pin_attempts.get(ip, []) if now - t < 300]
    attempts.append(now)
    failed_pin_attempts[ip] = attempts
    if len(attempts) >= 5:
        blocked_ips[ip] = now + 900  # 15 dakika engelle
    return len(attempts)


def record_successful_pin_attempt(ip: str):
    failed_pin_attempts.pop(ip, None)
    blocked_ips.pop(ip, None)


VALID_GATE_PINS = {str(GATE_PIN).strip(), "2805", "28052024", "0528", "05282024"}


def get_gate_role_from_request() -> Optional[str]:
    token = (
        request.headers.get("X-Gate-Token", "").strip()
        or request.cookies.get("stok_gate_token", "").strip()
        or request.args.get("gate_token", "").strip()
    )
    pin_hdr = request.headers.get("X-Gate-PIN", "").strip()
    if token == EXPECTED_GATE_TOKEN or pin_hdr in VALID_GATE_PINS:
        return ROLE_PATRON
    if token == EXPECTED_STAFF_GATE_TOKEN or (STAFF_PIN and pin_hdr == str(STAFF_PIN).strip()):
        return ROLE_CALISAN
    return None


def is_gate_unlocked_request() -> bool:
    return get_gate_role_from_request() is not None


_detected_external_url: Optional[str] = os.environ.get("RENDER_EXTERNAL_URL")


@app.before_request
def gatekeeper_check():
    global _detected_external_url
    path = request.path
    if path in ("/health", "/ping"):
        return None

    host = (request.headers.get("Host") or "").strip()
    host_lower = host.lower()
    if (
        not _detected_external_url
        and host
        and not host_lower.startswith(("localhost", "127.0.0.1", "0.0.0.0", "192.168.", "10."))
    ):
        proto = (request.headers.get("X-Forwarded-Proto") or "https").split(",")[0].strip()
        _detected_external_url = f"{proto}://{host}"

    # 1. Bot ve AI Crawler Kontrolü (Tüm yollar için geçerli!)
    user_agent = (request.headers.get("User-Agent") or "").lower().strip()
    if any(bot in user_agent for bot in DISALLOWED_BOT_KEYWORDS):
        return jsonify({
            "ok": False,
            "error": "Forbidden",
            "message": "🤖 Güvenlik Politikası: Otomatik botların ve yapay zeka tarayıcılarının erişimi engellenmiştir."
        }), 403

    # Muaf tutulan yollar: Statik dosyalar, favicon, manifest, sw, robots.txt, gate API'leri ve sağlık kontrolleri
    if (
        path == "/"
        or path.startswith("/static/")
        or path in ("/favicon.ico", "/manifest.json", "/sw.js", "/robots.txt", "/health", "/ping")
        or path.startswith("/api/gate/")
        or path == "/api/farm/auth-check"
    ):
        return None

    # Tüm API ve veri işlemleri için PIN doğrulaması zorunludur
    if not is_gate_unlocked_request():
        return jsonify({
            "ok": False,
            "gate_locked": True,
            "message": "🔒 Yetkisiz erişim. Lütfen güvenlik PIN kodunu giriniz."
        }), 401


@app.after_request
def add_security_headers(response):
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive, nosnippet, noimageindex"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if (
        request.path == "/"
        or request.path.endswith(".html")
        or request.path in ("/sw.js", "/manifest.json")
        or request.path.startswith("/api/")
    ):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0, post-check=0, pre-check=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.route("/robots.txt")
def robots_txt():
    robots_path = Path(__file__).resolve().parent / "static" / "robots.txt"
    if robots_path.exists():
        return send_from_directory(str(robots_path.parent), "robots.txt", mimetype="text/plain")
    return "User-agent: *\nDisallow: /\n", 200, {"Content-Type": "text/plain"}


@app.route("/ping", methods=["GET", "HEAD"])
@app.route("/health", methods=["GET", "HEAD"])
def health_ping():
    """Render.com uyku önleyici (keep-alive) ve sunucu/bulut sağlık kontrolü."""
    has_fb = bool(cloud_db.ensure_firebase())
    if has_fb and not cloud_db._hydration_completed:
        threading.Thread(target=cloud_db.ensure_initial_hydration, daemon=True).start()
    return jsonify({
        "ok": True,
        "online": True,
        "has_firebase": has_fb,
        "hydration_completed": bool(cloud_db._hydration_completed),
        "timestamp": time.time(),
    })


@app.route("/api/gate/verify", methods=["POST"])
def gate_verify():
    client_ip = get_client_ip()
    if is_ip_blocked(client_ip):
        return jsonify({
            "ok": False,
            "blocked": True,
            "message": "🚫 Güvenlik: Çok fazla hatalı PIN denemesi yapıldı. Erişiminiz 15 dakika süreyle geçici olarak engellendi."
        }), 429

    data = request.json or {}
    pin = str(data.get("pin", "")).strip()
    actor_name_in = str(data.get("actor_name", "")).strip()
    if pin in VALID_GATE_PINS:
        record_successful_pin_attempt(client_ip)
        token = EXPECTED_GATE_TOKEN
        is_master = pin in ("0528", "05282024")
        actor = build_actor(name=actor_name_in or "Patron", role=ROLE_PATRON, ip_address=client_ip)
        log_audit(
            action="AUTH_PIN_LOGIN",
            entity_type="security",
            entity_id="gate",
            new_value={"role": ROLE_PATRON, "actor_name": actor["name"], "is_master": is_master},
            actor=actor,
            ip_address=client_ip,
            db=cloud_db.db,
        )
        resp = jsonify({
            "ok": True,
            "token": token,
            "role": ROLE_PATRON,
            "is_master": is_master,
            "actor_name": actor["name"],
            "permissions": get_permissions_for_role(ROLE_PATRON),
            "message": "Patron girişi başarılı!",
        })
        resp.set_cookie(
            "stok_gate_token",
            token,
            max_age=86400 * 30,
            httponly=False,
            samesite="Lax"
        )
        return resp

    if STAFF_PIN and pin == str(STAFF_PIN).strip():
        record_successful_pin_attempt(client_ip)
        token = EXPECTED_STAFF_GATE_TOKEN
        actor = build_actor(name=actor_name_in or "Çalışan", role=ROLE_CALISAN, ip_address=client_ip)
        log_audit(
            action="AUTH_PIN_LOGIN",
            entity_type="security",
            entity_id="gate",
            new_value={"role": ROLE_CALISAN, "actor_name": actor["name"], "is_master": False},
            actor=actor,
            ip_address=client_ip,
            db=cloud_db.db,
        )
        resp = jsonify({
            "ok": True,
            "token": token,
            "role": ROLE_CALISAN,
            "is_master": False,
            "actor_name": actor["name"],
            "permissions": get_permissions_for_role(ROLE_CALISAN),
            "message": "Çalışan girişi başarılı!",
        })
        resp.set_cookie(
            "stok_gate_token",
            token,
            max_age=86400 * 30,
            httponly=False,
            samesite="Lax"
        )
        return resp

    attempts_count = record_failed_pin_attempt(client_ip)
    log_unauthorized_attempt(
        action="GATE_PIN_FAILED",
        resource="/api/gate/verify",
        actor=build_actor(name="Bilinmeyen", role=ROLE_CALISAN, ip_address=client_ip),
        ip_address=client_ip,
        details={"attempts": attempts_count},
        db=cloud_db.db,
    )
    remaining = max(0, 5 - attempts_count)
    if remaining == 0:
        return jsonify({
            "ok": False,
            "blocked": True,
            "message": "🚫 Güvenlik: 5 kez hatalı PIN girildi. Erişiminiz 15 dakika süreyle engellendi."
        }), 429
    return jsonify({
        "ok": False,
        "message": f"Geçersiz PIN şifresi! ({remaining} deneme hakkı kaldı)"
    }), 401


@app.route("/api/gate/status", methods=["GET"])
def gate_status():
    role = get_gate_role_from_request()
    unlocked = role is not None
    return jsonify({
        "ok": True,
        "unlocked": unlocked,
        "role": role,
        "permissions": get_permissions_for_role(role) if role else [],
        "cancel_reasons": CANCEL_REASONS,
        "high_amount_threshold": HIGH_AMOUNT_WARNING_THRESHOLD,
    })


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


@app.route("/api/settings/gemini-key", methods=["GET", "POST"])
def handle_gemini_key():
    denied = enforce_permission("settings:manage", "SETTINGS_GEMINI_KEY")
    if denied:
        return denied
    if request.method == "POST":
        data = request.json or {}
        key = (data.get("key") or data.get("api_key") or "").strip()
        if not key:
            return jsonify({"ok": False, "message": "API Key boş olamaz!"}), 400
        ok = ai_service.save_api_key(key)
        cloud_db.save_gemini_key(key)
        return jsonify({"ok": ok, "message": "✨ Gemini API Key Firebase Firestore'a ve sisteme başarıyla kaydedildi!" if ok else "Kaydetme hatası!"})

    key = cloud_db.get_gemini_key() or ai_service.api_key or ""
    return jsonify({
        "ok": True,
        "has_key": bool(key),
        "key_preview": (key[:8] + "..." + key[-4:]) if len(key) > 12 else ""
    })



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


@app.route("/api/auth/stores", methods=["GET"])
def get_stores():
    """Tüm kayıtlı mağazaları listeler."""
    stores = cloud_db.get_all_stores()
    return jsonify({"ok": True, "stores": stores})


@app.route("/api/auth/switch-store", methods=["POST"])
def switch_store():
    """Hızlı mağaza geçişi yapar."""
    data = request.json or {}
    store_id = data.get("user_id") or data.get("id")
    if not store_id:
        return jsonify({"ok": False, "message": "Mağaza ID belirtilmedi!"}), 400
    try:
        store_id = int(store_id)
    except (ValueError, TypeError):
        return jsonify({"ok": False, "message": "Geçersiz mağaza ID!"}), 400

    ok, msg, user = cloud_db.quick_switch_user(store_id)
    if not ok:
        return jsonify({"ok": False, "message": msg}), 400
    return jsonify({"ok": True, "message": msg, "user": user})


def get_current_user_id() -> int:
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if not token:
        token = request.args.get("token", "").strip()
    if token:
        user = cloud_db.verify_token(token)
        if user and user.get("id"):
            return user["id"]
    resolved = cloud_db._resolve_user_id()
    return resolved if resolved is not None else 1


def get_current_actor() -> Dict[str, Any]:
    """İstekten aktif kullanıcı rolünü ve kimliğini çözümler (Patron veya Çalışan)."""
    gate_role = get_gate_role_from_request() or ROLE_PATRON
    role = gate_role
    actor_name = request.headers.get("X-Actor-Name", "").strip()
    uid = get_current_user_id()

    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if not token:
        token = request.args.get("token", "").strip()
    if token:
        user = cloud_db.verify_token(token)
        if user:
            if not actor_name:
                actor_name = user.get("full_name") or user.get("company_name") or ""
            if normalize_role(user.get("role")) == ROLE_CALISAN:
                role = ROLE_CALISAN

    if not actor_name:
        actor_name = "Patron" if role == ROLE_PATRON else "Çalışan"

    return build_actor(
        name=actor_name,
        role=role,
        user_id=uid,
        ip_address=get_client_ip(),
    )


def enforce_permission(permission: str, action_label: Optional[str] = None):
    """Yetki kontrolü yapar; yetkisiz denemeyi 403 ile engeller ve audit_logs tablosuna yazar."""
    actor = get_current_actor()
    if has_permission(actor["role"], permission):
        return None

    label = action_label or permission
    log_unauthorized_attempt(
        action=label,
        resource=f"{request.method} {request.path}",
        actor=actor,
        user_id=actor.get("user_id"),
        ip_address=actor.get("ip_address"),
        details={
            "required_permission": permission,
            "query": dict(request.args),
        },
        db=cloud_db.db,
    )
    return jsonify({
        "ok": False,
        "forbidden": True,
        "role": actor["role"],
        "message": "🚫 Bu işlem için yetkiniz bulunmamaktadır. Sadece Patron yetkilidir.",
    }), 403


@app.route("/api/auth/role", methods=["GET"])
def get_auth_role():
    actor = get_current_actor()
    return jsonify({
        "ok": True,
        "role": actor["role"],
        "actor_name": actor["name"],
        "permissions": get_permissions_for_role(actor["role"]),
        "cancel_reasons": CANCEL_REASONS,
        "high_amount_threshold": HIGH_AMOUNT_WARNING_THRESHOLD,
        "today": get_turkey_now_str("%Y-%m-%d"),
    })



# ════════════════════════════════════════════════════════════════════
# GERÇEK ZAMANLI VERİ SENKRONİZASYONU (LIVE INSTANT SYNC)
# ════════════════════════════════════════════════════════════════════
_data_version_lock = threading.Lock()
_user_data_versions: dict[int, int] = {}
_version_events: dict[int, threading.Event] = {}


def notify_data_change(user_id: Optional[int] = None) -> int:
    """Telefon veya PC'de herhangi bir değişiklik olduğunda diğer tüm cihazlara anında haber verir."""
    uid = user_id or 1
    with _data_version_lock:
        current = _user_data_versions.get(uid, 0) + 1
        _user_data_versions[uid] = current
        if uid in _version_events:
            _version_events[uid].set()
            _version_events[uid] = threading.Event()
        else:
            _version_events[uid] = threading.Event()
        return current


# ════════════════════════════════════════════════════════════════════
# SATIŞ MÜKERRER KAYIT & KİLİT MEKANİZMASI (IDEMPOTENCY / ANTI-DUPLICATE)
# ════════════════════════════════════════════════════════════════════
_sale_idempotency_lock = threading.Lock()
_sale_idempotency_cache: dict[str, dict[str, Any]] = {}


def get_data_version(user_id: Optional[int] = None) -> int:
    uid = user_id or 1
    with _data_version_lock:
        return _user_data_versions.get(uid, 0)


@app.route("/api/events/poll", methods=["GET"])
def poll_events():
    user_id = get_current_user_id()
    if user_id is None:
        return jsonify({"ok": False, "authenticated": False}), 401

    try:
        client_version = int(request.args.get("version", 0))
    except Exception:
        client_version = 0

    current_version = get_data_version(user_id)
    if current_version > client_version:
        return jsonify({"ok": True, "changed": True, "version": current_version})

    with _data_version_lock:
        if user_id not in _version_events:
            _version_events[user_id] = threading.Event()
        event = _version_events[user_id]

    event.wait(timeout=8.0)
    current_version = get_data_version(user_id)
    return jsonify({
        "ok": True,
        "changed": current_version > client_version,
        "version": current_version,
    })


@app.route("/api/sync/trigger", methods=["GET", "POST"])
def trigger_sync():
    user_id = get_current_user_id()
    if user_id is None:
        return jsonify({"ok": False, "authenticated": False, "message": "Lütfen önce giriş yapınız!"}), 401
    res = cloud_db.sync_offline_data_with_firebase(user_id=user_id, allow_pull=True)
    notify_data_change(user_id)
    return jsonify({"ok": True, "result": res})


@app.route("/api/sync/full", methods=["GET", "POST"])
def full_sync():
    """Tüm aktif ürünleri ve verileri Firebase Firestore ile tam senkronize eder."""
    user_id = get_current_user_id()
    if user_id is None:
        return jsonify({"ok": False, "authenticated": False, "message": "Lütfen önce giriş yapınız!"}), 401
    res = cloud_db.force_full_sync_with_firebase(user_id=user_id)
    notify_data_change(user_id)
    return jsonify({"ok": True, "result": res})


@app.route("/api/sync/status", methods=["GET"])
def sync_status():
    user_id = get_current_user_id()
    has_firebase = bool(cloud_db.ensure_firebase())
    
    un_prods = 0
    un_sales = 0
    un_exp = 0
    if user_id is not None:
        with cloud_db.db.get_connection() as conn:
            un_prods = conn.execute("SELECT COUNT(*) as cnt FROM products WHERE synced_to_cloud = 0 AND (user_id = ? OR user_id IS NULL)", (user_id,)).fetchone()["cnt"]
            un_sales = conn.execute("SELECT COUNT(*) as cnt FROM sales WHERE synced_to_cloud = 0 AND (user_id = ? OR user_id IS NULL)", (user_id,)).fetchone()["cnt"]
            un_exp = conn.execute("SELECT COUNT(*) as cnt FROM expenses WHERE synced_to_cloud = 0 AND (user_id = ? OR user_id IS NULL)", (user_id,)).fetchone()["cnt"]
    total_unsynced = un_prods + un_sales + un_exp

    src = cloud_db.firebase_cred_source or ""
    is_perm = bool(src and ("Ortam Değişkeni" in src or "secrets" in src))
    is_ephem = bool(src and "Yerel Dosya" in src)
    health = cloud_db.get_firebase_health_info()

    return jsonify({
        "ok": True,
        "has_firebase": has_firebase,
        "healthy": health["healthy"],
        "consecutive_errors": health["consecutive_errors"],
        "quota_blocked": health["quota_blocked"],
        "authenticated": bool(user_id is not None),
        "project_id": cloud_db.firebase_project_id,
        "cred_source": cloud_db.firebase_cred_source,
        "last_error": cloud_db.firebase_last_error,
        "is_permanent": is_perm,
        "is_ephemeral": is_ephem,
        "unsynced_items": total_unsynced,
        "details": {
            "products": un_prods,
            "sales": un_sales,
            "expenses": un_exp,
        }
    })


@app.route("/api/firebase/config", methods=["POST"])
def configure_firebase():
    """Web arayüzünden Firebase Service Account JSON kimlik bilgilerini yükler ve kaydeder."""
    denied = enforce_permission("settings:manage", "FIREBASE_CONFIG")
    if denied:
        return denied
    data = request.json or {}
    cred_content = data.get("credentials")
    if not cred_content:
        return jsonify({"ok": False, "message": "Lütfen geçerli bir Firebase Service Account JSON metni sağlayınız!"}), 400

    ok, msg = cloud_db.save_firebase_credentials(cred_content)
    if ok:
        src = cloud_db.firebase_cred_source or ""
        return jsonify({
            "ok": True,
            "message": msg,
            "project_id": cloud_db.firebase_project_id,
            "cred_source": cloud_db.firebase_cred_source,
            "is_permanent": bool(src and ("Ortam Değişkeni" in src or "secrets" in src)),
            "is_ephemeral": bool(src and "Yerel Dosya" in src),
        })
    return jsonify({"ok": False, "message": msg, "last_error": cloud_db.firebase_last_error}), 400


@app.route("/api/firebase/status", methods=["GET"])
def firebase_status():
    """Firebase bağlantı ve ortam teşhis bilgilerini döndürür."""
    connected = bool(cloud_db.ensure_firebase())
    src = cloud_db.firebase_cred_source or ""
    return jsonify({
        "ok": True,
        "connected": connected,
        "project_id": cloud_db.firebase_project_id,
        "cred_source": cloud_db.firebase_cred_source,
        "last_error": cloud_db.firebase_last_error,
        "file_exists": (DATA_DIR / "firebase_credentials.json").exists(),
        "is_permanent": bool(src and ("Ortam Değişkeni" in src or "secrets" in src)),
        "is_ephemeral": bool(src and "Yerel Dosya" in src),
    })


@app.route("/api/firebase/export-config", methods=["GET"])
def export_firebase_config():
    """Kullanıcının Render panelinde FIREBASE_CREDENTIALS_JSON ortam değişkenine ekleyebilmesi için JSON metnini döndürür."""
    denied = enforce_permission("settings:manage", "FIREBASE_EXPORT_CONFIG")
    if denied:
        return denied
    user_id = get_current_user_id()
    if user_id is None:
        return jsonify({"ok": False, "message": "Yetkisiz erişim! Lütfen önce giriş yapınız."}), 401

    # 1. Dosyadan oku
    cred_file = DATA_DIR / "firebase_credentials.json"
    if cred_file.exists():
        try:
            return jsonify({"ok": True, "raw_json": cred_file.read_text(encoding="utf-8")})
        except Exception as e:
            return jsonify({"ok": False, "message": f"Dosya okuma hatası: {e}"}), 500

    # 2. Ortam değişkenlerinden oku
    for env_k in ["FIREBASE_CREDENTIALS_JSON", "FIREBASE_SERVICE_ACCOUNT", "GOOGLE_APPLICATION_CREDENTIALS"]:
        env_val = os.environ.get(env_k)
        if env_val and env_val.strip():
            return jsonify({"ok": True, "raw_json": env_val.strip()})

    return jsonify({"ok": False, "message": "Sunucuda kayıtlı Firebase kimlik bilgisi bulunamadı."}), 404


@app.route("/api/categories")
def get_categories():
    user_id = get_current_user_id()
    if user_id is None:
        return jsonify([])
    return jsonify(cloud_db.get_categories(user_id=user_id))


@app.route("/api/products", methods=["GET"])
def get_products():
    try:
        user_id = get_current_user_id()
        if user_id is None:
            return jsonify([])
        search = request.args.get("search", "")
        products = cloud_db.get_products(user_id=user_id, search=search)
        actor = get_current_actor()
        if not has_permission(actor["role"], "profit:view"):
            for p in products:
                p.pop("purchase_price", None)
        return jsonify(products)
    except Exception as e:
        logger.error(f"/api/products rotası hatası: {e}", exc_info=True)
        try:
            with cloud_db.db.get_connection() as conn:
                rows = conn.execute("SELECT p.*, c.name as category_name FROM products p LEFT JOIN categories c ON p.category_id = c.id WHERE p.is_active = 1").fetchall()
                return jsonify([dict(r) for r in rows])
        except Exception:
            return jsonify([])


@app.route("/api/products/barcode/<barcode>")
def get_product_by_barcode(barcode):
    user_id = get_current_user_id()
    if user_id is None:
        return jsonify({"found": False, "message": "Lütfen önce giriş yapınız!"}), 401
    code_clean = barcode.strip()
    prod = cloud_db.get_product_by_barcode(code_clean, user_id=user_id)
    if not prod:
        # Fallback to searching products by ID, name, or code for this user
        products = cloud_db.get_products(user_id=user_id, search=code_clean)
        if products:
            prod = products[0]
    if prod:
        actor = get_current_actor()
        if not has_permission(actor["role"], "profit:view"):
            prod = dict(prod)
            prod.pop("purchase_price", None)
        return jsonify({"found": True, "product": prod})
    return jsonify({"found": False, "message": f"'{code_clean}' kodlu veya isimli ürün bulunamadı"}), 404


@app.route("/api/products", methods=["POST"])
def save_product():
    denied = enforce_permission("product:manage", "PRODUCT_SAVE")
    if denied:
        return denied
    user_id = get_current_user_id()
    if user_id is None:
        return jsonify({"ok": False, "authenticated": False, "message": "Lütfen önce giriş yapınız!"}), 401
    data = request.json or {}
    name = data.get("name", "").strip()
    barcode = data.get("barcode", "").strip()
    category = data.get("category", "Genel").strip()
    purchase_price = float(data.get("purchase_price", 0))
    sale_price = float(data.get("sale_price", 0))
    stock_quantity = float(data.get("stock_quantity", 0))
    critical_stock = float(data.get("critical_stock_level", 5))
    unit = str(data.get("unit", "adet")).strip().lower()
    image_path = data.get("image_path")
    raw_id = data.get("id") or data.get("product_id")
    product_id = int(raw_id) if raw_id else None

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
        unit=unit,
    )
    if ok:
        notify_data_change(user_id)
    return jsonify({"ok": ok, "message": msg, "product": prod})


@app.route("/api/products/<int:product_id>", methods=["PUT", "POST"])
def update_product_route(product_id: int):
    """Mevcut bir ürünün tüm bilgilerini veya seçili alanlarını günceller."""
    denied = enforce_permission("product:manage", "PRODUCT_UPDATE")
    if denied:
        return denied
    user_id = get_current_user_id()
    if user_id is None:
        return jsonify({"ok": False, "authenticated": False, "message": "Lütfen önce giriş yapınız!"}), 401

    data = request.json or {}
    curr = cloud_db.get_product_by_id(product_id, user_id=user_id)
    if not curr:
        return jsonify({"ok": False, "message": "Düzenlenecek ürün bulunamadı!"}), 404

    name = str(data.get("name") if data.get("name") is not None else curr.get("name", "")).strip()
    barcode = str(data.get("barcode") if data.get("barcode") is not None else curr.get("barcode", "")).strip()
    category = str(data.get("category") if data.get("category") is not None else (curr.get("category_name") or "Genel")).strip()
    
    try:
        purchase_price = float(data.get("purchase_price") if data.get("purchase_price") is not None else curr.get("purchase_price", 0.0))
        sale_price = float(data.get("sale_price") if data.get("sale_price") is not None else curr.get("sale_price", 0.0))
        stock_quantity = float(data.get("stock_quantity") if data.get("stock_quantity") is not None else curr.get("stock_quantity", 0))
        critical_stock = float(data.get("critical_stock_level") if data.get("critical_stock_level") is not None else curr.get("critical_stock_level", 5))
    except (ValueError, TypeError) as e:
        return jsonify({"ok": False, "message": f"Geçersiz sayısal değer: {e}"}), 400

    unit = str(data.get("unit") if data.get("unit") is not None else curr.get("unit", "adet")).strip().lower()
    image_path = data.get("image_path")  # None ise mevcut korunur, '__REMOVE__' ise silinir

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
        unit=unit,
    )
    if ok:
        notify_data_change(user_id)
    return jsonify({"ok": ok, "message": msg, "product": prod})


@app.route("/api/products/<int:product_id>/image", methods=["POST"])
def upload_product_image_route(product_id: int):
    """Bir ürüne mobil kameradan veya galeriden yeni fotoğraf yükler / değiştirir."""
    user_id = get_current_user_id()
    if user_id is None:
        return jsonify({"ok": False, "authenticated": False, "message": "Lütfen önce giriş yapınız!"}), 401

    image_path_str = ""
    if "file" in request.files:
        file = request.files["file"]
        if file.filename:
            image_bytes = file.read()
            try:
                from PIL import Image, ImageOps
                import io, base64
                pil_img = Image.open(io.BytesIO(image_bytes))
                pil_img = ImageOps.exif_transpose(pil_img).convert("RGB")
                pil_img.thumbnail((320, 320), Image.LANCZOS)
                buf = io.BytesIO()
                pil_img.save(buf, format="JPEG", quality=50, optimize=True)
                b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
                image_path_str = f"data:image/jpeg;base64,{b64}"
            except Exception as e:
                return jsonify({"ok": False, "message": f"Resim işlenemedi: {e}"}), 400
    elif request.is_json:
        data = request.json or {}
        image_path_str = data.get("image_path", "").strip()

    if not image_path_str:
        return jsonify({"ok": False, "message": "Yüklenecek resim bulunamadı!"}), 400

    ok, msg, clean_img = cloud_db.update_product_image(product_id, image_path_str, user_id=user_id)
    if ok:
        notify_data_change(user_id)
    return jsonify({"ok": ok, "message": msg, "image_path": clean_img})


@app.route("/api/products/<int:product_id>/image", methods=["DELETE"])
def delete_product_image_route(product_id: int):
    """Bir ürünün fotoğrafını kaldırır."""
    user_id = get_current_user_id()
    if user_id is None:
        return jsonify({"ok": False, "authenticated": False, "message": "Lütfen önce giriş yapınız!"}), 401

    ok, msg, clean_img = cloud_db.update_product_image(product_id, "__REMOVE__", user_id=user_id)
    if ok:
        notify_data_change(user_id)
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/products/<int:product_id>/price", methods=["POST"])
def update_product_price_route(product_id: int):
    """Ürünün satış fiyatını hızlıca günceller."""
    denied = enforce_permission("product:manage", "PRODUCT_PRICE_UPDATE")
    if denied:
        return denied
    user_id = get_current_user_id()
    if user_id is None:
        return jsonify({"ok": False, "authenticated": False, "message": "Lütfen önce giriş yapınız!"}), 401

    data = request.json or {}
    try:
        sale_price = float(data.get("sale_price", 0))
    except (ValueError, TypeError):
        return jsonify({"ok": False, "message": "Geçersiz satış fiyatı!"}), 400

    purchase_price = data.get("purchase_price")
    if purchase_price is not None:
        try:
            purchase_price = float(purchase_price)
        except (ValueError, TypeError):
            purchase_price = None

    ok, msg = cloud_db.update_product_price(product_id, sale_price, purchase_price=purchase_price, user_id=user_id)
    if ok:
        notify_data_change(user_id)
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/products/<int:product_id>", methods=["DELETE"])
def delete_product_route(product_id):
    denied = enforce_permission("product:manage", "PRODUCT_DELETE")
    if denied:
        return denied
    user_id = get_current_user_id()
    if user_id is None:
        return jsonify({"ok": False, "authenticated": False, "message": "Lütfen önce giriş yapınız!"}), 401
    ok, msg = cloud_db.delete_product(product_id, user_id=user_id)
    if ok:
        notify_data_change(user_id)
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/products/<int:product_id>/stock", methods=["POST"])
def update_product_stock_route(product_id):
    denied = enforce_permission("product:manage", "PRODUCT_STOCK_UPDATE")
    if denied:
        return denied
    user_id = get_current_user_id()
    if user_id is None:
        return jsonify({"ok": False, "authenticated": False, "message": "Lütfen önce giriş yapınız!"}), 401
    data = request.json or {}
    try:
        stock = float(data.get("stock_quantity", 0))
    except (ValueError, TypeError):
        return jsonify({"ok": False, "message": "Geçersiz stok miktarı!"}), 400

    ok, msg = cloud_db.update_product_stock(product_id, stock, user_id=user_id)
    if ok:
        notify_data_change(user_id)
    return jsonify({"ok": ok, "message": msg})


# ════════════════════════════════════════════════════════════════════
# WEBRTC P2P GÖRSEL AKTARIM SIGNALING ENDPOINTS
# ════════════════════════════════════════════════════════════════════
webrtc_sessions: dict = {}


@app.route("/api/webrtc/create-pair", methods=["POST"])
def webrtc_create_pair():
    import random
    code = f"{random.randint(100000, 999999)}"
    webrtc_sessions[code] = {
        "pc_signals": [],
        "phone_signals": [],
        "created_at": os.times().elapsed,
    }
    return jsonify({"ok": True, "pair_code": code})


@app.route("/api/webrtc/signal", methods=["POST"])
def webrtc_post_signal():
    data = request.json or {}
    code = str(data.get("pair_code", "")).strip()
    sender = data.get("sender")  # "pc" or "phone"
    payload = data.get("payload")

    if not code or code not in webrtc_sessions:
        return jsonify({"ok": False, "message": "Geçersiz veya bulunamayan eşleşme kodu!"}), 404

    target_key = "phone_signals" if sender == "pc" else "pc_signals"
    webrtc_sessions[code][target_key].append(payload)
    return jsonify({"ok": True})


@app.route("/api/webrtc/signal", methods=["GET"])
def webrtc_get_signal():
    code = request.args.get("pair_code", "").strip()
    role = request.args.get("role", "").strip()  # "pc" or "phone"

    if not code or code not in webrtc_sessions:
        return jsonify({"ok": False, "signals": []})

    my_signals_key = f"{role}_signals"
    signals = list(webrtc_sessions[code].get(my_signals_key, []))
    webrtc_sessions[code][my_signals_key] = []
    return jsonify({"ok": True, "signals": signals})



@app.route("/api/sales/recent", methods=["GET"])
def get_recent_sales_route():
    """POS ekranının kenarındaki 'Son Yapılan 5 İşlem' paneli için son satışları döner."""
    user_id = get_current_user_id()
    if user_id is None:
        return jsonify({"ok": False, "authenticated": False, "sales": []}), 401
    try:
        limit = int(request.args.get("limit", 5))
    except (ValueError, TypeError):
        limit = 5
    recent_sales = cloud_db.get_recent_sales(user_id=user_id, limit=limit)
    return jsonify({
        "ok": True,
        "sales": recent_sales,
        "has_firebase": bool(cloud_db.ensure_firebase()),
    })


@app.route("/api/sales", methods=["POST"])
def record_sale():
    user_id = get_current_user_id()
    if user_id is None:
        return jsonify({"ok": False, "authenticated": False, "message": "Lütfen önce giriş yapınız!"}), 401
    actor = get_current_actor()
    data = request.json or {}

    # Render cold-start sonrası Firebase kimlik bilgisi silinmişse istemci yedeğinden anında geri yükle
    fb_backup = data.get("firebase_credentials_backup")
    if fb_backup and not cloud_db.ensure_firebase():
        try:
            cloud_db.save_firebase_credentials(fb_backup if isinstance(fb_backup, str) else json.dumps(fb_backup))
        except Exception as e_fb:
            logger.warning(f"Satış sırasında Firebase yedek geri yükleme uyarısı: {e_fb}")

    items = data.get("items", [])
    note = data.get("note", "Mobil Satış")
    channel = data.get("channel", "magaza")
    payment_method = data.get("payment_method")
    total_amount = data.get("total_amount")
    customer_id = data.get("customer_id")
    customer_name = data.get("customer_name")
    paid_cash_amount = data.get("paid_cash_amount")
    paid_pos_amount = data.get("paid_pos_amount")
    overpayment_mode = data.get("overpayment_mode")
    idempotency_key = data.get("idempotency_key")
    sold_at = data.get("sold_at")
    confirm_high_amount = bool(data.get("confirm_high_amount", False))
    if not items:
        return jsonify({"ok": False, "message": "Sepet boş!"}), 400

    # Gün açılışı ve kapanış kilidi kontrolü
    target_bdate = str(sold_at).strip()[:10] if (sold_at and str(sold_at).strip()) else get_turkey_now_str("%Y-%m-%d")
    cash_day = cloud_db.get_cash_day(business_date=target_bdate, user_id=user_id)
    if cash_day and str(cash_day.get("status") or "").lower() == "closed":
        return jsonify({
            "ok": False,
            "day_closed": True,
            "business_date": target_bdate,
            "message": f"🔒 {target_bdate} günü kapatılmıştır! Kapanan günde satış girilemez; sadece Patron günü yeniden açabilir.",
        }), 409
    if not cash_day or cash_day.get("status") != "open":
        return jsonify({
            "ok": False,
            "day_not_open": True,
            "business_date": target_bdate,
            "message": "⚠️ Gün açılışı yapılmadı! Satış girebilmek için önce açılış kasasını girerek günü açınız.",
        }), 409

    # Çalışan geçmiş veya gelecek güne tarihli satış giremez
    if actor["role"] == ROLE_CALISAN and sold_at and str(sold_at).strip():
        today_str = get_turkey_now_str("%Y-%m-%d")
        req_date = str(sold_at).strip()[:10]
        if req_date != today_str:
            log_unauthorized_attempt(
                action="BACKDATED_SALE_CREATE",
                resource="POST /api/sales",
                actor=actor,
                user_id=user_id,
                ip_address=actor.get("ip_address"),
                details={"requested_sold_at": sold_at},
                db=cloud_db.db,
            )
            return jsonify({
                "ok": False,
                "forbidden": True,
                "message": "🚫 Çalışan sadece bugünün tarihine satış girebilir.",
            }), 403

    # Kalem ve toplam tutar doğrulaması (negatif / aşırı tutar kontrolü)
    calc_total = 0.0
    for it in items:
        try:
            u_price = float(it.get("unit_price", 0))
            qty = float(it.get("quantity", 1))
        except (ValueError, TypeError):
            return jsonify({"ok": False, "message": "Geçersiz ürün fiyatı veya miktar!"}), 400
        if u_price < 0 or qty <= 0:
            return jsonify({"ok": False, "message": "Birim fiyat negatif, miktar 0 veya negatif olamaz!"}), 400
        calc_total += float(it.get("subtotal", u_price * qty))

    effective_total = total_amount if total_amount is not None else calc_total
    val_check = validate_monetary_amount(
        effective_total,
        field_label="Satış tutarı",
        allow_zero=True,
        confirm_high_amount=confirm_high_amount,
    )
    if not val_check["ok"]:
        status_code = 409 if val_check["requires_confirmation"] else 400
        return jsonify({
            "ok": False,
            "message": val_check["message"],
            "requires_confirmation": val_check["requires_confirmation"],
            "amount": val_check["value"],
            "warning_threshold": val_check["warning_threshold"],
        }), status_code

    # Benzersiz imza / key ile çift tıklama ve mükerrer istek koruması
    now_ts = time.time()
    if idempotency_key:
        cache_key = f"u{user_id}_k_{idempotency_key}"
    else:
        items_sig = tuple(sorted((int(it.get("product_id", 0)), float(it.get("quantity", 1))) for it in items))
        cache_key = f"u{user_id}_sig_{hash(items_sig)}_{total_amount}_{customer_name}"

    with _sale_idempotency_lock:
        # 60 saniyeden eski kayıtları temizle
        expired = [k for k, v in _sale_idempotency_cache.items() if now_ts - v.get("timestamp", 0) > 60.0]
        for k in expired:
            _sale_idempotency_cache.pop(k, None)

        entry = _sale_idempotency_cache.get(cache_key)
        if entry:
            if entry.get("in_progress"):
                logger.warning("Çift tıklama / eşzamanlı satış engellendi: %s", cache_key)
                return jsonify({"ok": True, "message": "Satış işlemi kaydediliyor, lütfen bekleyiniz...", "duplicate": True})
            if now_ts - entry.get("timestamp", 0) < 15.0:
                logger.warning("Son 15 saniyede tamamlanmış mükerrer satış engellendi: %s", cache_key)
                return jsonify(entry.get("response", {"ok": True, "message": "Satış zaten işlendi.", "duplicate": True}))

        _sale_idempotency_cache[cache_key] = {"timestamp": now_ts, "in_progress": True}

    total_override = None
    if total_amount is not None:
        try:
            total_override = float(total_amount)
        except (ValueError, TypeError):
            total_override = None

    try:
        ok, msg = cloud_db.add_sale(
            items,
            note=note,
            user_id=user_id,
            channel=channel,
            total_amount_override=total_override,
            customer_name=customer_name,
            sold_at=sold_at,
            actor=actor,
            payment_method=payment_method,
            customer_id=customer_id,
            paid_cash_amount=paid_cash_amount,
            paid_pos_amount=paid_pos_amount,
            overpayment_mode=overpayment_mode,
        )
        recent_sales = []
        if ok:
            notify_data_change(user_id)
            recent_sales = cloud_db.get_recent_sales(user_id=user_id, limit=5)
            send_sale_notification_async(
                items=items,
                total_amount=total_override,
                channel=channel,
                customer_name=customer_name,
                user_id=user_id,
            )
        resp_data = {
            "ok": ok,
            "message": msg,
            "recent_sales": recent_sales,
            "has_firebase": bool(cloud_db.firestore_db),
        }
        with _sale_idempotency_lock:
            _sale_idempotency_cache[cache_key] = {"timestamp": time.time(), "in_progress": False, "response": resp_data}
        return jsonify(resp_data), (200 if ok else 400)
    except Exception as exc:
        with _sale_idempotency_lock:
            _sale_idempotency_cache.pop(cache_key, None)
        logger.error("Satış kaydı sırasında hata: %s", exc, exc_info=True)
        return jsonify({"ok": False, "message": f"Satış kaydedilirken hata oluştu: {str(exc)}"}), 500


@app.route("/api/sales/online", methods=["POST"])
def record_online_sale():
    """İnternetten satılan ürünün adet bilgisini alıp doğrudan stoktan düşer ve satış kaydeder."""
    user_id = get_current_user_id()
    if user_id is None:
        return jsonify({"ok": False, "authenticated": False, "message": "Lütfen önce giriş yapınız!"}), 401

    data = request.json or {}
    product_id = data.get("product_id")
    barcode = str(data.get("barcode", "")).strip()

    try:
        qty = float(data.get("quantity", 1))
    except (ValueError, TypeError):
        qty = 1.0

    if qty <= 0:
        return jsonify({"ok": False, "message": "Geçersiz satış miktarı!"}), 400

    prod = None
    if product_id:
        prods = cloud_db.get_products(user_id=user_id)
        prod = next((p for p in prods if p["id"] == int(product_id)), None)
    elif barcode:
        prod = cloud_db.get_product_by_barcode(barcode, user_id=user_id)

    if not prod:
        return jsonify({"ok": False, "message": "Satışı yapılacak ürün bulunamadı!"}), 404

    current_stock = float(prod.get("stock_quantity", 0))
    p_unit = prod.get("unit", "adet")
    if current_stock < qty:
        return jsonify({
            "ok": False,
            "message": f"Yetersiz stok! Mevcut stok: {current_stock:g} {p_unit}, girilen satış: {qty:g} {p_unit}."
        }), 400

    unit_price = float(data.get("unit_price") if data.get("unit_price") is not None else prod.get("sale_price", 0.0))
    confirm_high_amount = bool(data.get("confirm_high_amount", False))
    online_total = round(unit_price * qty, 2)
    val_check = validate_monetary_amount(
        online_total,
        field_label="İnternet satış tutarı",
        allow_zero=True,
        confirm_high_amount=confirm_high_amount,
    )
    if not val_check["ok"]:
        status_code = 409 if val_check["requires_confirmation"] else 400
        return jsonify({
            "ok": False,
            "message": val_check["message"],
            "requires_confirmation": val_check["requires_confirmation"],
            "amount": val_check["value"],
            "warning_threshold": val_check["warning_threshold"],
        }), status_code

    platform = (data.get("platform") or data.get("note") or "İnternet Satışı").strip()
    actor = get_current_actor()

    cart_items = [{
        "product_id": prod["id"],
        "product_name": prod["name"],
        "barcode": prod["barcode"],
        "unit_price": unit_price,
        "quantity": qty,
        "subtotal": online_total,
        "unit": p_unit,
    }]

    # Mükerrer internet satışı koruması
    cache_key = f"u{user_id}_online_{prod['id']}_{qty}_{unit_price}"
    now_ts = time.time()
    with _sale_idempotency_lock:
        entry = _sale_idempotency_cache.get(cache_key)
        if entry:
            if entry.get("in_progress"):
                return jsonify({"ok": True, "message": "İnternet satışı kaydediliyor...", "duplicate": True})
            if now_ts - entry.get("timestamp", 0) < 10.0:
                return jsonify(entry.get("response", {"ok": True, "message": "Satış zaten işlendi.", "duplicate": True}))
        _sale_idempotency_cache[cache_key] = {"timestamp": now_ts, "in_progress": True}

    try:
        sold_at = data.get("sold_at")
        ok, msg = cloud_db.add_sale(
            cart_items,
            note=f"🌐 {platform}",
            user_id=user_id,
            channel="internet",
            actor=actor,
            sold_at=sold_at,
        )
        recent_sales = []
        if ok:
            notify_data_change(user_id)
            recent_sales = cloud_db.get_recent_sales(user_id=user_id, limit=5)
            send_sale_notification_async(
                items=cart_items,
                total_amount=None,
                channel="internet",
                customer_name=platform,
                user_id=user_id,
            )
        resp_data = {
            "ok": ok,
            "message": f"🌐 {qty} adet '{prod['name']}' internet satışı başarıyla kaydedildi." if ok else msg,
            "product_name": prod["name"],
            "remaining_stock": max(0, current_stock - qty),
            "recent_sales": recent_sales,
        }
        with _sale_idempotency_lock:
            _sale_idempotency_cache[cache_key] = {"timestamp": time.time(), "in_progress": False, "response": resp_data}
        return jsonify(resp_data)
    except Exception as exc:
        with _sale_idempotency_lock:
            _sale_idempotency_cache.pop(cache_key, None)
        logger.error("İnternet satışı sırasında hata: %s", exc, exc_info=True)
        return jsonify({"ok": False, "message": f"Satış kaydedilirken hata oluştu: {str(exc)}"}), 500


@app.route("/api/sales/forecast", methods=["GET"])
def get_sales_forecast():
    """Cihazın donanım gücünden (çok çekirdekli CPU) yararlanarak
    yoğun günler ve talep tahminlerini üretir.
    """
    denied = enforce_permission("report:view", "SALES_FORECAST_VIEW")
    if denied:
        return denied
    user_id = get_current_user_id()
    if user_id is None:
        return jsonify({"ok": False, "authenticated": False, "message": "Lütfen önce giriş yapınız!"}), 401

    forecast = forecast_service.generate_comprehensive_forecast(user_id=user_id)
    return jsonify(forecast)


@app.route("/api/sales/<sale_id>", methods=["DELETE"])
@app.route("/api/sales/<sale_id>/cancel", methods=["POST"])
def delete_sale_route(sale_id):
    denied = enforce_permission("sale:cancel", "SALE_CANCEL")
    if denied:
        return denied
    user_id = get_current_user_id()
    if user_id is None:
        return jsonify({"ok": False, "authenticated": False, "message": "Lütfen önce giriş yapınız!"}), 401

    data = request.get_json(silent=True) or {}
    restore_stock_raw = data.get("restore_stock", request.args.get("restore_stock", "true"))
    restore_stock = str(restore_stock_raw).lower() != "false" if not isinstance(restore_stock_raw, bool) else restore_stock_raw
    cancel_reason = (data.get("cancel_reason") or data.get("reason") or request.args.get("cancel_reason") or request.args.get("reason") or "").strip()
    cancel_note = (data.get("cancel_note") or data.get("note") or request.args.get("cancel_note") or "").strip()

    ok_r, msg_r, clean_reason, clean_note = validate_cancel_reason(cancel_reason, cancel_note)
    if not ok_r:
        return jsonify({"ok": False, "message": msg_r}), 400

    # Kapanan gün kilidi: satışın ait olduğu gün kapalıysa iptal edilemez
    if not str(sale_id).strip().startswith("farm_"):
        try:
            with cloud_db.db.get_connection() as conn:
                s_dt_row = conn.execute("SELECT sold_at FROM sales WHERE id = ?", (int(sale_id),)).fetchone()
            if s_dt_row and s_dt_row["sold_at"]:
                s_bdate = str(s_dt_row["sold_at"]).strip()[:10]
                if cloud_db.is_cash_day_closed(business_date=s_bdate, user_id=user_id):
                    return jsonify({
                        "ok": False,
                        "day_closed": True,
                        "message": f"🔒 {s_bdate} günü kapatılmıştır! Kapanan günde satış iptal edilemez; önce Patron günü yeniden açmalıdır.",
                    }), 409
        except Exception:
            pass

    actor = get_current_actor()
    ok, msg = cloud_db.cancel_sale(
        sale_id,
        cancel_reason=clean_reason,
        cancel_note=clean_note,
        user_id=user_id,
        restore_stock=restore_stock,
        actor=actor,
    )
    if ok:
        notify_data_change(user_id)
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)


@app.route("/api/sales/<sale_id>/datetime", methods=["POST", "PUT"])
def update_sale_datetime_route(sale_id):
    denied = enforce_permission("sale:edit", "SALE_DATETIME_UPDATE")
    if denied:
        return denied
    user_id = get_current_user_id()
    if user_id is None:
        return jsonify({"ok": False, "authenticated": False, "message": "Lütfen önce giriş yapınız!"}), 401

    data = request.json or {}
    new_dt = data.get("sold_at") or data.get("datetime") or data.get("date")
    if not new_dt:
        return jsonify({"ok": False, "message": "Geçerli bir tarih ve saat seçilmelidir!"}), 400

    # Kapanan gün kilidi
    new_bdate = str(new_dt).strip()[:10]
    if cloud_db.is_cash_day_closed(business_date=new_bdate, user_id=user_id):
        return jsonify({"ok": False, "day_closed": True, "message": f"🔒 {new_bdate} günü kapatılmıştır! Düzenleme için Patron günü yeniden açmalıdır."}), 409
    if not str(sale_id).strip().startswith("farm_"):
        try:
            with cloud_db.db.get_connection() as conn:
                s_dt_row = conn.execute("SELECT sold_at FROM sales WHERE id = ?", (int(sale_id),)).fetchone()
            if s_dt_row and s_dt_row["sold_at"]:
                old_bdate = str(s_dt_row["sold_at"]).strip()[:10]
                if cloud_db.is_cash_day_closed(business_date=old_bdate, user_id=user_id):
                    return jsonify({"ok": False, "day_closed": True, "message": f"🔒 {old_bdate} günü kapatılmıştır! Düzenleme için Patron günü yeniden açmalıdır."}), 409
        except Exception:
            pass

    actor = get_current_actor()
    ok, msg = cloud_db.update_sale_datetime(sale_id, str(new_dt), user_id=user_id, actor=actor)
    if ok:
        notify_data_change(user_id)
        raw = str(new_dt).strip().replace("T", " ")
        if len(raw) == 10:
            formatted_dt = f"{raw} 12:00:00"
        elif len(raw) == 16:
            formatted_dt = f"{raw}:00"
        elif len(raw) >= 19:
            formatted_dt = raw[:19]
        else:
            formatted_dt = raw
        return jsonify({"ok": True, "message": msg, "sold_at": formatted_dt})
    return jsonify({"ok": False, "message": msg}), 400


@app.route("/api/sales/bulk-delete", methods=["POST"])
@app.route("/api/sales/bulk-cancel", methods=["POST"])
def bulk_delete_sales_route():
    denied = enforce_permission("sale:cancel", "SALE_BULK_CANCEL")
    if denied:
        return denied
    user_id = get_current_user_id()
    if user_id is None:
        return jsonify({"ok": False, "authenticated": False, "message": "Lütfen önce giriş yapınız!"}), 401

    data = request.json or {}
    sale_ids = data.get("sale_ids", [])
    restore_stock = bool(data.get("restore_stock", True))
    cancel_reason = (data.get("cancel_reason") or data.get("reason") or "").strip()
    cancel_note = (data.get("cancel_note") or data.get("note") or "").strip()

    if not sale_ids:
        return jsonify({"ok": False, "message": "İptal edilecek satış seçilmedi!"}), 400

    ok_r, msg_r, clean_reason, clean_note = validate_cancel_reason(cancel_reason, cancel_note)
    if not ok_r:
        return jsonify({"ok": False, "message": msg_r}), 400

    # Kapanan gün kilidi: seçilen satışlardan biri kapalı güne aitse engelle
    for sid in sale_ids:
        if not str(sid).strip().startswith("farm_"):
            try:
                with cloud_db.db.get_connection() as conn:
                    s_dt_row = conn.execute("SELECT sold_at FROM sales WHERE id = ?", (int(sid),)).fetchone()
                if s_dt_row and s_dt_row["sold_at"]:
                    s_bdate = str(s_dt_row["sold_at"]).strip()[:10]
                    if cloud_db.is_cash_day_closed(business_date=s_bdate, user_id=user_id):
                        return jsonify({
                            "ok": False,
                            "day_closed": True,
                            "message": f"🔒 {s_bdate} günü kapatılmıştır! Kapanan günde satış iptal edilemez; önce Patron günü yeniden açmalıdır.",
                        }), 409
            except Exception:
                pass

    actor = get_current_actor()
    ok, msg, count = cloud_db.delete_sales_bulk(
        sale_ids,
        user_id=user_id,
        restore_stock=restore_stock,
        cancel_reason=clean_reason,
        cancel_note=clean_note,
        actor=actor,
        require_reason=True,
    )
    if ok:
        notify_data_change(user_id)
    return jsonify({"ok": ok, "message": msg, "count": count}), (200 if ok else 400)


@app.route("/api/sales/history", methods=["GET"])
def get_sales_history():
    user_id = get_current_user_id()
    if user_id is None:
        return jsonify({"ok": False, "authenticated": False, "sales": []}), 401
    actor = get_current_actor()
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    customer_name = request.args.get("customer_name")

    # Çalışan sadece bugünün satışlarını görebilir; geçmiş gün sorgusu engellenir ve loglanır
    if not has_permission(actor["role"], "sale:view_history"):
        today_str = get_turkey_now_str("%Y-%m-%d")
        if (start_date and start_date.strip() != today_str) or (end_date and end_date.strip() != today_str):
            log_unauthorized_attempt(
                action="PAST_SALES_HISTORY_ACCESS",
                resource="GET /api/sales/history",
                actor=actor,
                user_id=user_id,
                ip_address=actor.get("ip_address"),
                details={"start_date": start_date, "end_date": end_date, "today": today_str},
                db=cloud_db.db,
            )
            return jsonify({
                "ok": False,
                "forbidden": True,
                "message": "🚫 Çalışan sadece bugünün satışlarını görüntüleyebilir. Geçmiş günler için Patron yetkisi gerekir.",
                "sales": [],
            }), 403
        start_date = today_str
        end_date = today_str

    history = cloud_db.get_sales_history(
        user_id=user_id,
        start_date=start_date,
        end_date=end_date,
        customer_name=customer_name,
    )
    return jsonify({"ok": True, "sales": history, "role": actor["role"]})


@app.route("/api/sales/analytics", methods=["GET"])
def get_sales_analytics():
    denied = enforce_permission("report:view", "SALES_ANALYTICS_VIEW")
    if denied:
        return denied
    user_id = get_current_user_id()
    if user_id is None:
        return jsonify({"ok": False, "authenticated": False, "message": "Lütfen önce giriş yapınız!"}), 401
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    analytics = cloud_db.get_sales_analytics(user_id=user_id, start_date=start_date, end_date=end_date)
    hours = cloud_db.get_store_hours(user_id=user_id)
    return jsonify({"ok": True, "analytics": analytics, "store_hours": hours})


@app.route("/api/expenses", methods=["GET", "POST"])
def handle_expenses():
    perm = "expense:create" if request.method == "POST" else "expense:view"
    denied = enforce_permission(perm, "EXPENSE_ACCESS")
    if denied:
        return denied
    user_id = get_current_user_id()
    if user_id is None:
        return jsonify({"ok": False, "authenticated": False, "expenses": []}), 401
    if request.method == "POST":
        data = request.json or {}
        title = data.get("title", "")
        raw_amount = data.get("amount", 0)
        confirm_high_amount = bool(data.get("confirm_high_amount", False))
        val_check = validate_monetary_amount(
            raw_amount,
            field_label="Gider tutarı",
            allow_zero=False,
            confirm_high_amount=confirm_high_amount,
        )
        if not val_check["ok"]:
            status_code = 409 if val_check["requires_confirmation"] else 400
            return jsonify({
                "ok": False,
                "message": val_check["message"],
                "requires_confirmation": val_check["requires_confirmation"],
                "amount": val_check["value"],
                "warning_threshold": val_check["warning_threshold"],
            }), status_code

        amount = val_check["value"]
        category = data.get("category", "Fatura")
        payment_method = data.get("payment_method", "nakit")
        expense_date = data.get("expense_date") or get_turkey_now_str("%Y-%m-%d")
        note = data.get("note", "")
        actor = get_current_actor()

        if cloud_db.is_cash_day_closed(business_date=str(expense_date)[:10], user_id=user_id):
            return jsonify({
                "ok": False,
                "day_closed": True,
                "message": f"🔒 {str(expense_date)[:10]} günü kapatılmıştır! Kapanan güne gider eklenemez.",
            }), 409

        ok, msg, exp = cloud_db.add_expense(
            title=title,
            amount=amount,
            category=category,
            expense_date=expense_date,
            note=note,
            user_id=user_id,
            actor=actor,
            payment_method=payment_method,
        )
        if ok:
            notify_data_change(user_id)
        return jsonify({"ok": ok, "message": msg, "expense": exp}), (200 if ok else 400)

    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    expenses = cloud_db.get_expenses(user_id=user_id, start_date=start_date, end_date=end_date)
    return jsonify({"ok": True, "expenses": expenses})


@app.route("/api/expenses/<int:expense_id>", methods=["DELETE"])
@app.route("/api/expenses/<int:expense_id>/cancel", methods=["POST"])
def cancel_expense_route(expense_id: int):
    denied = enforce_permission("expense:cancel", "EXPENSE_CANCEL")
    if denied:
        return denied
    user_id = get_current_user_id()
    with cloud_db.db.get_connection() as conn:
        e_row = conn.execute("SELECT expense_date FROM expenses WHERE id = ?", (expense_id,)).fetchone()
    if e_row and e_row["expense_date"] and cloud_db.is_cash_day_closed(business_date=str(e_row["expense_date"])[:10], user_id=user_id):
        return jsonify({"ok": False, "day_closed": True, "message": "🔒 Kapanan güne ait gider iptal edilemez!"}), 409

    data = request.get_json(silent=True) or {}
    cancel_reason = (data.get("cancel_reason") or data.get("reason") or request.args.get("cancel_reason") or "").strip()
    cancel_note = (data.get("cancel_note") or data.get("note") or request.args.get("cancel_note") or "").strip()
    actor = get_current_actor()

    ok, msg = cloud_db.cancel_expense(
        expense_id,
        cancel_reason=cancel_reason,
        cancel_note=cancel_note,
        user_id=user_id,
        actor=actor,
    )
    if ok:
        notify_data_change(user_id)
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)


@app.route("/api/cash-day/status", methods=["GET"])
def get_cash_day_status():
    user_id = get_current_user_id()
    actor = get_current_actor()
    today_str = get_turkey_now_str("%Y-%m-%d")
    req_date = (request.args.get("date") or today_str).strip()[:10]
    if req_date != today_str and not has_permission(actor["role"], "cash:view"):
        log_unauthorized_attempt(
            action="PAST_CASH_DAY_ACCESS",
            resource="GET /api/cash-day/status",
            actor=actor,
            user_id=user_id,
            ip_address=actor.get("ip_address"),
            details={"requested_date": req_date},
            db=cloud_db.db,
        )
        return jsonify({"ok": False, "forbidden": True, "message": "🚫 Geçmiş gün kasa bilgisi için Patron yetkisi gerekir."}), 403
    day = cloud_db.get_cash_day(business_date=req_date, user_id=user_id)
    return jsonify({
        "ok": True,
        "business_date": req_date,
        "is_open": bool(day and day.get("status") == "open"),
        "day": day,
    })


@app.route("/api/cash-day/open", methods=["POST"])
def open_cash_day_route():
    denied = enforce_permission("cash_day:open", "CASH_DAY_OPEN")
    if denied:
        return denied
    user_id = get_current_user_id()
    actor = get_current_actor()
    data = request.json or {}
    today_str = get_turkey_now_str("%Y-%m-%d")
    b_date = str(data.get("business_date") or today_str).strip()[:10]

    if b_date != today_str and not has_permission(actor["role"], "day:reopen"):
        log_unauthorized_attempt(
            action="PAST_CASH_DAY_OPEN",
            resource="POST /api/cash-day/open",
            actor=actor,
            user_id=user_id,
            ip_address=actor.get("ip_address"),
            details={"requested_date": b_date},
            db=cloud_db.db,
        )
        return jsonify({"ok": False, "forbidden": True, "message": "🚫 Çalışan sadece bugünün gün açılışını yapabilir."}), 403

    confirm_high = bool(data.get("confirm_high_amount", False))
    val_check = validate_monetary_amount(
        data.get("opening_amount", 0),
        field_label="Açılış kasası",
        allow_zero=True,
        confirm_high_amount=confirm_high,
    )
    if not val_check["ok"]:
        code = 409 if val_check["requires_confirmation"] else 400
        return jsonify({
            "ok": False,
            "message": val_check["message"],
            "requires_confirmation": val_check["requires_confirmation"],
            "amount": val_check["value"],
        }), code

    ok, msg, day_rec = cloud_db.open_cash_day(
        opening_amount=val_check["value"],
        business_date=b_date,
        opening_note=str(data.get("opening_note") or data.get("note") or "").strip(),
        user_id=user_id,
        actor=actor,
    )
    if ok:
        notify_data_change(user_id)
    return jsonify({"ok": ok, "message": msg, "day": day_rec}), (200 if ok else 400)


@app.route("/api/cash-day/update-values", methods=["POST"])
def update_cash_day_values_route():
    """Sabah kasa, akşam kasa, POS toplamı ve gün içi giderleri kaydeder; tüm tutar değişikliklerini saat + eski/yeni değer ile loglar."""
    denied = enforce_permission("cash_day:open", "CASH_DAY_UPDATE_VALUES")
    if denied:
        return denied
    user_id = get_current_user_id()
    actor = get_current_actor()
    data = request.json or {}
    today_str = get_turkey_now_str("%Y-%m-%d")
    b_date = str(data.get("business_date") or data.get("date") or today_str).strip()[:10]

    if b_date != today_str and not has_permission(actor["role"], "day:reopen"):
        log_unauthorized_attempt(
            action="PAST_CASH_DAY_UPDATE",
            resource="POST /api/cash-day/update-values",
            actor=actor,
            user_id=user_id,
            ip_address=actor.get("ip_address"),
            details={"requested_date": b_date},
            db=cloud_db.db,
        )
        return jsonify({"ok": False, "forbidden": True, "message": "🚫 Çalışan sadece bugünün kasa değerlerini güncelleyebilir."}), 403

    confirm_high = bool(data.get("confirm_high_amount", False))
    parsed: Dict[str, Optional[float]] = {"opening_amount": None, "counted_cash": None, "entered_pos_total": None}
    labels = {
        "opening_amount": "Sabah kasa",
        "counted_cash": "Akşam kasa",
        "entered_pos_total": "POS toplamı",
    }
    for key, label in labels.items():
        raw = data.get(key)
        if raw is not None and str(raw).strip() != "":
            chk = validate_monetary_amount(raw, field_label=label, allow_zero=True, confirm_high_amount=confirm_high)
            if not chk["ok"]:
                return jsonify({
                    "ok": False,
                    "message": chk["message"],
                    "requires_confirmation": chk["requires_confirmation"],
                }), (409 if chk["requires_confirmation"] else 400)
            parsed[key] = chk["value"]

    for exp_item in (data.get("new_expenses") or []):
        exp_chk = validate_monetary_amount(
            exp_item.get("amount"),
            field_label="Gider tutarı",
            allow_zero=False,
            confirm_high_amount=confirm_high,
        )
        if not exp_chk["ok"]:
            return jsonify({
                "ok": False,
                "message": exp_chk["message"],
                "requires_confirmation": exp_chk["requires_confirmation"],
            }), (409 if exp_chk["requires_confirmation"] else 400)

    ok, msg, summary = cloud_db.update_cash_day_values(
        business_date=b_date,
        opening_amount=parsed["opening_amount"],
        counted_cash=parsed["counted_cash"],
        entered_pos_total=parsed["entered_pos_total"],
        new_expenses=data.get("new_expenses") or [],
        reason=str(data.get("reason") or "").strip(),
        user_id=user_id,
        actor=actor,
    )
    if ok:
        notify_data_change(user_id)
    return jsonify({"ok": ok, "message": msg, "summary": summary}), (200 if ok else 400)


@app.route("/api/cash-day/history", methods=["GET"])
def get_cash_day_history_route():
    """Sabah kasa / akşam kasa / POS değişiklik geçmişini (saat kaçta kaç yazıldı -> sonra kaç yazıldı) döner."""
    user_id = get_current_user_id()
    actor = get_current_actor()
    today_str = get_turkey_now_str("%Y-%m-%d")
    req_date = (request.args.get("date") or today_str).strip()[:10]
    if req_date != today_str and not has_permission(actor["role"], "cash:view"):
        log_unauthorized_attempt(
            action="PAST_CASH_HISTORY_ACCESS",
            resource="GET /api/cash-day/history",
            actor=actor,
            user_id=user_id,
            ip_address=actor.get("ip_address"),
            details={"requested_date": req_date},
            db=cloud_db.db,
        )
        return jsonify({"ok": False, "forbidden": True, "message": "🚫 Geçmiş gün kasa değişiklik geçmişi için Patron yetkisi gerekir."}), 403

    history = cloud_db.get_cash_day_value_history(business_date=req_date, user_id=user_id)
    return jsonify({"ok": True, "business_date": req_date, "history": history})


@app.route("/api/cash-day/summary", methods=["GET"])
def get_cash_day_summary_route():
    user_id = get_current_user_id()
    actor = get_current_actor()
    today_str = get_turkey_now_str("%Y-%m-%d")
    req_date = (request.args.get("date") or today_str).strip()[:10]
    if req_date != today_str and not has_permission(actor["role"], "cash:view"):
        log_unauthorized_attempt(
            action="PAST_CASH_SUMMARY_ACCESS",
            resource="GET /api/cash-day/summary",
            actor=actor,
            user_id=user_id,
            ip_address=actor.get("ip_address"),
            details={"requested_date": req_date},
            db=cloud_db.db,
        )
        return jsonify({"ok": False, "forbidden": True, "message": "🚫 Geçmiş gün kasa özetini sadece Patron görüntüleyebilir."}), 403

    summary = cloud_db.get_cash_summary(business_date=req_date, user_id=user_id)
    return jsonify({"ok": True, "summary": summary, "role": actor["role"]})


@app.route("/api/cash-movements", methods=["GET", "POST"])
def handle_cash_movements():
    user_id = get_current_user_id()
    actor = get_current_actor()

    if request.method == "POST":
        data = request.json or {}
        movement_type = str(data.get("movement_type", "tahsilat")).strip().lower()
        category = str(data.get("category", "genel")).strip().lower() or "genel"

        # Rol yetki kontrolü: Patron çekimi ve nakit çıkış sadece Patron'a, nakit giriş (para üstü vb.) Çalışan'a da açık
        if category == "patron_cekis":
            denied = enforce_permission("cash:patron_withdraw", "PATRON_CASH_WITHDRAW")
            if denied:
                return denied
            movement_type = "kasa_cikis"
        elif movement_type == "kasa_giris":
            denied = enforce_permission("cash:in", "CASH_IN_CREATE")
            if denied:
                return denied
        else:
            denied = enforce_permission("cash:create", "CASH_MOVEMENT_CREATE")
            if denied:
                return denied

        description = data.get("description", "")
        confirm_short_desc = bool(data.get("confirm_short_description", False))
        desc_check = validate_cash_description(
            description,
            movement_type,
            confirm_short_description=confirm_short_desc,
        )
        if not desc_check["ok"]:
            status_code = 409 if desc_check["requires_confirmation"] else 400
            return jsonify({
                "ok": False,
                "message": desc_check["message"],
                "requires_confirmation": desc_check["requires_confirmation"],
                "warning_type": desc_check.get("warning_type"),
            }), status_code

        customer_name = data.get("customer_name")
        movement_date = data.get("movement_date")
        note = data.get("note", "")
        confirm_high_amount = bool(data.get("confirm_high_amount", False))

        mov_bdate = str(movement_date).strip()[:10] if (movement_date and str(movement_date).strip()) else get_turkey_now_str("%Y-%m-%d")
        if cloud_db.is_cash_day_closed(business_date=mov_bdate, user_id=user_id):
            return jsonify({
                "ok": False,
                "day_closed": True,
                "message": f"🔒 {mov_bdate} günü kapatılmıştır! Kapanan güne kasa hareketi eklenemez.",
            }), 409

        val_check = validate_monetary_amount(
            data.get("amount", 0),
            field_label="İşlem tutarı",
            allow_zero=False,
            confirm_high_amount=confirm_high_amount,
        )
        if not val_check["ok"]:
            status_code = 409 if val_check["requires_confirmation"] else 400
            return jsonify({
                "ok": False,
                "message": val_check["message"],
                "requires_confirmation": val_check["requires_confirmation"],
                "amount": val_check["value"],
                "warning_threshold": val_check["warning_threshold"],
            }), status_code

        ok, msg, rec = cloud_db.add_cash_movement(
            movement_type=movement_type,
            amount=val_check["value"],
            description=desc_check["value"],
            category=category,
            customer_name=customer_name,
            movement_date=movement_date,
            note=note,
            user_id=user_id,
            actor=actor,
        )
        if ok:
            notify_data_change(user_id)
        return jsonify({"ok": ok, "message": msg, "movement": rec}), (200 if ok else 400)

    # GET
    denied = enforce_permission("cash:view", "CASH_MOVEMENT_VIEW")
    if denied:
        return denied
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    m_type = request.args.get("movement_type")
    res = cloud_db.get_cash_movements(
        user_id=user_id,
        start_date=start_date,
        end_date=end_date,
        movement_type=m_type,
        include_cancelled=True,
    )
    return jsonify({"ok": True, **res})


@app.route("/api/cash-movements/<int:movement_id>", methods=["DELETE"])
@app.route("/api/cash-movements/<int:movement_id>/cancel", methods=["POST"])
def cancel_cash_movement_route(movement_id: int):
    denied = enforce_permission("cash:cancel", "CASH_MOVEMENT_CANCEL")
    if denied:
        return denied
    user_id = get_current_user_id()
    with cloud_db.db.get_connection() as conn:
        m_row = conn.execute("SELECT movement_date FROM cash_movements WHERE id = ?", (movement_id,)).fetchone()
    if m_row and m_row["movement_date"] and cloud_db.is_cash_day_closed(business_date=str(m_row["movement_date"])[:10], user_id=user_id):
        return jsonify({"ok": False, "day_closed": True, "message": "🔒 Kapanan güne ait kasa hareketi iptal edilemez!"}), 409

    data = request.get_json(silent=True) or {}
    cancel_reason = (data.get("cancel_reason") or data.get("reason") or request.args.get("cancel_reason") or "").strip()
    cancel_note = (data.get("cancel_note") or data.get("note") or request.args.get("cancel_note") or "").strip()
    actor = get_current_actor()

    ok, msg = cloud_db.cancel_cash_movement(
        movement_id,
        cancel_reason=cancel_reason,
        cancel_note=cancel_note,
        user_id=user_id,
        actor=actor,
    )
    if ok:
        notify_data_change(user_id)
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)


@app.route("/api/reports/cancelled", methods=["GET"])
def get_cancelled_report_route():
    """Patron için iptal edilen satış/gider/tahsilat/kasa/çiftlik kayıtları raporu."""
    denied = enforce_permission("cancelled_report:view", "CANCELLED_REPORT_VIEW")
    if denied:
        return denied
    user_id = get_current_user_id()
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    entity_type = request.args.get("entity_type")
    report = cloud_db.get_cancelled_report(
        user_id=user_id,
        start_date=start_date,
        end_date=end_date,
        entity_type=entity_type,
    )
    return jsonify({"ok": True, "report": report})


@app.route("/api/reports/discrepancies", methods=["GET"])
def get_discrepancy_report_route():
    """Sadece Patron için gün sonu fark geçmişi, çalışan bazlı toplam/aylık rapor ve 3+ gün tekrar uyarıları."""
    denied = enforce_permission("report:view", "DISCREPANCY_REPORT_VIEW")
    if denied:
        return denied
    user_id = get_current_user_id()
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    cashier_name = request.args.get("cashier_name")
    report = cloud_db.get_discrepancy_report(
        user_id=user_id,
        start_date=start_date,
        end_date=end_date,
        cashier_name=cashier_name,
    )
    return jsonify({"ok": True, "report": report})


@app.route("/api/audit-logs", methods=["GET"])
def get_audit_logs_route():
    """Patron için salt okunur denetim kayıtları (audit log) listesi."""
    denied = enforce_permission("audit:view", "AUDIT_LOGS_VIEW")
    if denied:
        return denied
    user_id = get_current_user_id()
    entity_type = request.args.get("entity_type")
    action = request.args.get("action")
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    limit = request.args.get("limit", default=200, type=int)
    logs = get_audit_logs(
        user_id=user_id,
        entity_type=entity_type,
        action=action,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        db=cloud_db.db,
    )
    return jsonify({"ok": True, "logs": logs})


@app.route("/api/cash-day/reconciliation", methods=["GET", "POST"])
def cash_day_reconciliation_route():
    """Gün sonu mutabakat önizlemesi: Sistem | Girilen | Fark karşılaştırması."""
    user_id = get_current_user_id()
    actor = get_current_actor()
    today_str = get_turkey_now_str("%Y-%m-%d")

    if request.method == "POST":
        data = request.json or {}
        req_date = str(data.get("business_date") or data.get("date") or today_str).strip()[:10]
        opening_amount_raw = data.get("opening_amount")
        counted_cash = data.get("counted_cash")
        entered_pos_total = data.get("entered_pos_total")
        entered_expenses_total = data.get("entered_expenses_total")
        pending_expenses = data.get("new_expenses") or data.get("pending_expenses") or []
    else:
        req_date = (request.args.get("date") or today_str).strip()[:10]
        opening_amount_raw = request.args.get("opening_amount", type=float)
        counted_cash = request.args.get("counted_cash", type=float)
        entered_pos_total = request.args.get("entered_pos_total", type=float)
        entered_expenses_total = request.args.get("entered_expenses_total", type=float)
        pending_expenses = []

    if req_date != today_str and not has_permission(actor["role"], "cash:view"):
        log_unauthorized_attempt(
            action="PAST_DAY_RECONCILIATION_ACCESS",
            resource=f"{request.method} /api/cash-day/reconciliation",
            actor=actor,
            user_id=user_id,
            ip_address=actor.get("ip_address"),
            details={"requested_date": req_date},
            db=cloud_db.db,
        )
        return jsonify({"ok": False, "forbidden": True, "message": "🚫 Geçmiş gün mutabakatını sadece Patron görüntüleyebilir."}), 403

    try:
        o_amt = float(opening_amount_raw) if opening_amount_raw is not None and str(opening_amount_raw).strip() != "" else None
        c_cash = float(counted_cash) if counted_cash is not None and str(counted_cash).strip() != "" else None
        e_pos = float(entered_pos_total) if entered_pos_total is not None and str(entered_pos_total).strip() != "" else None
        e_exp = float(entered_expenses_total) if entered_expenses_total is not None and str(entered_expenses_total).strip() != "" else None
    except (ValueError, TypeError):
        return jsonify({"ok": False, "message": "Geçersiz sayısal mutabakat girişi!"}), 400

    summary = cloud_db.get_cash_summary(
        business_date=req_date,
        user_id=user_id,
        counted_cash=c_cash,
        entered_pos_total=e_pos,
        entered_expenses_total=e_exp,
        pending_expenses=pending_expenses,
        opening_amount_override=o_amt,
    )
    return jsonify({"ok": True, "reconciliation": summary, "role": actor["role"]})


@app.route("/api/cash-day/close", methods=["POST"])
def close_cash_day_route():
    """Gün sonu kapanışını ve mutabakatı onaylar, günü kilitler."""
    denied = enforce_permission("cash_day:close", "CASH_DAY_CLOSE")
    if denied:
        return denied
    user_id = get_current_user_id()
    actor = get_current_actor()
    data = request.json or {}
    today_str = get_turkey_now_str("%Y-%m-%d")
    b_date = str(data.get("business_date") or data.get("date") or today_str).strip()[:10]

    if b_date != today_str and not has_permission(actor["role"], "day:reopen"):
        log_unauthorized_attempt(
            action="PAST_CASH_DAY_CLOSE",
            resource="POST /api/cash-day/close",
            actor=actor,
            user_id=user_id,
            ip_address=actor.get("ip_address"),
            details={"requested_date": b_date},
            db=cloud_db.db,
        )
        return jsonify({"ok": False, "forbidden": True, "message": "🚫 Çalışan sadece bugünün kapanışını yapabilir."}), 403

    confirm_high = bool(data.get("confirm_high_amount", False))
    cash_val = validate_monetary_amount(
        data.get("counted_cash"),
        field_label="Sayılan nakit",
        allow_zero=True,
        confirm_high_amount=confirm_high,
    )
    if not cash_val["ok"]:
        return jsonify({
            "ok": False,
            "message": cash_val["message"],
            "requires_confirmation": cash_val["requires_confirmation"],
        }), (409 if cash_val["requires_confirmation"] else 400)

    pos_val = validate_monetary_amount(
        data.get("entered_pos_total", 0),
        field_label="POS toplamı",
        allow_zero=True,
        confirm_high_amount=confirm_high,
    )
    if not pos_val["ok"]:
        return jsonify({
            "ok": False,
            "message": pos_val["message"],
            "requires_confirmation": pos_val["requires_confirmation"],
        }), (409 if pos_val["requires_confirmation"] else 400)

    ent_exp = data.get("entered_expenses_total")
    ent_exp_val = None
    if ent_exp is not None and str(ent_exp).strip() != "":
        exp_chk = validate_monetary_amount(
            ent_exp,
            field_label="Girilen gider toplamı",
            allow_zero=True,
            confirm_high_amount=confirm_high,
        )
        if not exp_chk["ok"]:
            return jsonify({
                "ok": False,
                "message": exp_chk["message"],
                "requires_confirmation": exp_chk["requires_confirmation"],
            }), (409 if exp_chk["requires_confirmation"] else 400)
        ent_exp_val = exp_chk["value"]

    # Kapanışta sabah kasa da düzenlendiyse önce saatli değişiklik kaydını işle
    raw_open = data.get("opening_amount")
    if raw_open is not None and str(raw_open).strip() != "":
        open_chk = validate_monetary_amount(raw_open, field_label="Sabah kasa", allow_zero=True, confirm_high_amount=confirm_high)
        if not open_chk["ok"]:
            return jsonify({
                "ok": False,
                "message": open_chk["message"],
                "requires_confirmation": open_chk["requires_confirmation"],
            }), (409 if open_chk["requires_confirmation"] else 400)
        cloud_db.update_cash_day_values(
            business_date=b_date,
            opening_amount=open_chk["value"],
            reason=str(data.get("closing_note") or "Gün sonu sabah kasa düzeltmesi").strip(),
            user_id=user_id,
            actor=actor,
        )

    ok, msg, summary = cloud_db.close_cash_day(
        counted_cash=cash_val["value"],
        entered_pos_total=pos_val["value"],
        business_date=b_date,
        closing_note=str(data.get("closing_note") or "").strip(),
        new_expenses=data.get("new_expenses") or [],
        entered_expenses_total=ent_exp_val,
        user_id=user_id,
        actor=actor,
    )
    if ok:
        notify_data_change(user_id)
    return jsonify({"ok": ok, "message": msg, "reconciliation": summary}), (200 if ok else 400)


@app.route("/api/day/reopen", methods=["POST"])
def reopen_day_route():
    """Kapanan günü sadece Patron yetkisiyle yeniden açar ve denetim kaydına işler."""
    denied = enforce_permission("day:reopen", "DAY_REOPEN")
    if denied:
        return denied
    user_id = get_current_user_id()
    actor = get_current_actor()
    data = request.json or {}
    target_date = str(data.get("date") or data.get("business_date") or get_turkey_now_str("%Y-%m-%d")).strip()[:10]
    reason = str(data.get("reason") or "").strip()

    ok, msg, day_rec = cloud_db.reopen_cash_day(
        business_date=target_date,
        reason=reason,
        user_id=user_id,
        actor=actor,
    )
    if ok:
        notify_data_change(user_id)
    return jsonify({"ok": ok, "message": msg, "day": day_rec}), (200 if ok else 400)


# ════════════════════════════════════════════════════════════════════
# VERESİYE / CARİ HESAP & TAHSİLAT UÇ NOKTALARI
# ════════════════════════════════════════════════════════════════════
@app.route("/api/credit/customers", methods=["GET", "POST"])
def handle_credit_customers():
    denied = enforce_permission("credit:view", "CREDIT_CUSTOMERS_ACCESS")
    if denied:
        return denied
    user_id = get_current_user_id()

    if request.method == "POST":
        data = request.json or {}
        name = str(data.get("name") or "").strip()
        phone = str(data.get("phone") or "").strip()
        note = str(data.get("note") or "").strip()
        actor = get_current_actor()
        ok, msg, rec = cloud_db.get_or_create_credit_customer(
            name=name, phone=phone, note=note, user_id=user_id, actor=actor
        )
        if ok:
            notify_data_change(user_id)
        return jsonify({"ok": ok, "message": msg, "customer": rec}), (200 if ok else 400)

    search = request.args.get("search", "")
    res = cloud_db.get_credit_customers(user_id=user_id, search=search)
    return jsonify({"ok": True, **res})


@app.route("/api/credit/customers/<int:customer_id>", methods=["GET"])
def get_credit_customer_detail_route(customer_id: int):
    denied = enforce_permission("credit:view", "CREDIT_CUSTOMER_DETAIL")
    if denied:
        return denied
    user_id = get_current_user_id()
    detail = cloud_db.get_credit_customer_detail(customer_id=customer_id, user_id=user_id)
    if not detail:
        return jsonify({"ok": False, "message": "Cari müşteri bulunamadı!"}), 404
    return jsonify({"ok": True, **detail})


@app.route("/api/credit/collections", methods=["POST"])
def add_credit_collection_route():
    """Sadece borç ödemesi (Tahsilat - Nakit veya POS) alır."""
    denied = enforce_permission("credit:collect", "CREDIT_COLLECTION_CREATE")
    if denied:
        return denied
    user_id = get_current_user_id()
    actor = get_current_actor()
    data = request.json or {}

    customer_id = data.get("customer_id")
    if not customer_id:
        return jsonify({"ok": False, "message": "Tahsilat için müşteri seçilmesi zorunludur!"}), 400

    tx_date = data.get("tx_date")
    b_date = str(tx_date).strip()[:10] if (tx_date and str(tx_date).strip()) else get_turkey_now_str("%Y-%m-%d")
    day = cloud_db.get_cash_day(business_date=b_date, user_id=user_id)
    if day and str(day.get("status") or "").lower() == "closed":
        return jsonify({
            "ok": False,
            "day_closed": True,
            "message": f"🔒 {b_date} günü kapatılmıştır! Kapanan güne tahsilat girilemez.",
        }), 409
    if not day or day.get("status") != "open":
        return jsonify({
            "ok": False,
            "day_not_open": True,
            "message": "⚠️ Gün açılışı yapılmadı! Tahsilat alabilmek için önce açılış kasasını girerek günü açınız.",
        }), 409

    confirm_high = bool(data.get("confirm_high_amount", False))
    val_check = validate_monetary_amount(
        data.get("amount"),
        field_label="Tahsilat tutarı",
        allow_zero=False,
        confirm_high_amount=confirm_high,
    )
    if not val_check["ok"]:
        return jsonify({
            "ok": False,
            "message": val_check["message"],
            "requires_confirmation": val_check["requires_confirmation"],
            "amount": val_check["value"],
        }), (409 if val_check["requires_confirmation"] else 400)

    ok, msg, rec = cloud_db.add_credit_collection(
        customer_id=int(customer_id),
        amount=val_check["value"],
        payment_method=data.get("payment_method", "nakit"),
        note=str(data.get("note") or "").strip(),
        tx_date=tx_date,
        user_id=user_id,
        actor=actor,
    )
    if ok:
        notify_data_change(user_id)
    return jsonify({"ok": ok, "message": msg, "collection": rec}), (200 if ok else 400)


@app.route("/api/credit/collections/<int:tx_id>", methods=["DELETE"])
@app.route("/api/credit/collections/<int:tx_id>/cancel", methods=["POST"])
def cancel_credit_collection_route(tx_id: int):
    denied = enforce_permission("credit:cancel", "CREDIT_COLLECTION_CANCEL")
    if denied:
        return denied
    user_id = get_current_user_id()
    with cloud_db.db.get_connection() as conn:
        t_row = conn.execute("SELECT tx_date FROM credit_transactions WHERE id = ?", (tx_id,)).fetchone()
    if t_row and t_row["tx_date"] and cloud_db.is_cash_day_closed(business_date=str(t_row["tx_date"])[:10], user_id=user_id):
        return jsonify({"ok": False, "day_closed": True, "message": "🔒 Kapanan güne ait tahsilat iptal edilemez!"}), 409

    data = request.get_json(silent=True) or {}
    cancel_reason = (data.get("cancel_reason") or data.get("reason") or request.args.get("cancel_reason") or "").strip()
    cancel_note = (data.get("cancel_note") or data.get("note") or request.args.get("cancel_note") or "").strip()
    actor = get_current_actor()

    ok, msg = cloud_db.cancel_credit_collection(
        tx_id=tx_id,
        cancel_reason=cancel_reason,
        cancel_note=cancel_note,
        user_id=user_id,
        actor=actor,
    )
    if ok:
        notify_data_change(user_id)
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)


@app.route("/api/ai/analyze-sales", methods=["POST"])
def api_analyze_sales():
    denied = enforce_permission("report:view", "AI_ANALYZE_SALES")
    if denied:
        return denied
    user_id = get_current_user_id()
    if user_id is None:
        return jsonify({"ok": False, "authenticated": False, "message": "Lütfen önce giriş yapınız!"}), 401
    start_date = request.json.get("start_date") if request.json else None
    end_date = request.json.get("end_date") if request.json else None
    analytics = cloud_db.get_sales_analytics(user_id=user_id, start_date=start_date, end_date=end_date)
    hours = cloud_db.get_store_hours(user_id=user_id)
    res = ai_service.analyze_sales_and_business(analytics, hours)
    return jsonify(res)


@app.route("/api/settings/store-hours", methods=["GET", "POST"])
def handle_store_hours():
    if request.method == "POST":
        denied = enforce_permission("settings:manage", "STORE_HOURS_UPDATE")
        if denied:
            return denied
    user_id = get_current_user_id()
    if user_id is None:
        return jsonify({"ok": False, "authenticated": False, "message": "Lütfen önce giriş yapınız!"}), 401
    if request.method == "POST":
        data = request.json or {}
        weekday = data.get("weekday", "08:00 - 22:00")
        weekend = data.get("weekend", "09:00 - 23:00")
        cloud_db.save_store_hours(weekday, weekend, user_id=user_id)
        notify_data_change(user_id)
        return jsonify({"ok": True, "message": "İşletme çalışma saatleri başarıyla kaydedildi."})

    hours = cloud_db.get_store_hours(user_id=user_id)
    return jsonify({"ok": True, "store_hours": hours})




@app.route("/api/barcode/decode", methods=["POST"])
def api_decode_barcode():
    """Yüklenen fotoğraftan EXIF oryantasyonunu düzelterek ve çok aşamalı motorla barkod çözer."""
    if "file" not in request.files:
        return jsonify({"ok": False, "message": "Resim bulunamadı!"}), 400

    file = request.files["file"]
    image_bytes = file.read()
    if not image_bytes:
        return jsonify({"ok": False, "message": "Geçersiz veya boş resim dosyası!"}), 400

    try:
        import io
        from PIL import Image, ImageOps
        pil_img = Image.open(io.BytesIO(image_bytes))
        # Telefon kamerası dikey/yatay EXIF oryantasyonunu düzelt
        pil_img = ImageOps.exif_transpose(pil_img)

        # 1. PIL görseliyle doğrudan C++ zxingcpp taraması
        results = decode_barcode_from_frame(pil_img)
        if results and results[0][0] and is_valid_barcode(results[0][0]):
            return jsonify({"ok": True, "barcode": results[0][0]})

        # 2. NumPy / OpenCV çok katmanlı filtreleme ve tarama
        try:
            rgb_arr = np.array(pil_img.convert("RGB"))
            bgr_frame = cv2.cvtColor(rgb_arr, cv2.COLOR_RGB2BGR)
            results = decode_barcode_from_frame(bgr_frame)
            if results and results[0][0] and is_valid_barcode(results[0][0]):
                return jsonify({"ok": True, "barcode": results[0][0]})
        except Exception:
            pass

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


# ════════════════════════════════════════════════════════════════════
# ÇİFTLİK YÖNETİMİ REST API ENDPOINTS
# ════════════════════════════════════════════════════════════════════
@app.route("/api/farm/auth-check", methods=["POST"])
def farm_auth_check():
    data = request.json or {}
    pin = str(data.get("password") or data.get("pin") or "").strip()
    if pin in ("0528", "05282024"):
        return jsonify({
            "ok": True,
            "is_master": True,
            "message": "👑 Yönetici yetkilendirmesi başarılı!"
        })
    elif pin in ("2805", "28052024"):
        return jsonify({
            "ok": True,
            "is_master": False,
            "message": "Personel doğrulaması (Kısıtlı yetki, Çiftlik/Yönetici yetkisi yoktur)."
        })
    return jsonify({"ok": False, "is_master": False, "message": "Hatalı şifre!"}), 401


@app.route("/api/farm/customers", methods=["GET"])
def api_farm_customers():
    denied = enforce_permission("farm:manage", "FARM_CUSTOMERS_VIEW")
    if denied:
        return denied
    uid = get_current_user_id()
    customers = cloud_db.get_farm_customers(user_id=uid)
    return jsonify({"ok": True, "customers": customers})


@app.route("/api/farm/egg-sales", methods=["GET", "POST"])
def api_farm_egg_sales():
    denied = enforce_permission("farm:manage", "FARM_EGG_SALES_ACCESS")
    if denied:
        return denied
    uid = get_current_user_id()
    if request.method == "POST":
        data = request.json or {}
        cust = data.get("customer_name", "").strip()
        boxes = data.get("box_count")
        price = data.get("unit_price")
        s_date = data.get("sale_date")
        source = data.get("source", "Ciftlik")
        note = data.get("note")
        unit_type = data.get("unit_type", "koli")
        piece_count = data.get("piece_count")
        tot_amount = data.get("total_amount")
        idempotency_key = (data.get("idempotency_key") or "").strip()

        raw_fp = f"farm_egg|{uid}|{cust.lower()}|{boxes}|{unit_type}|{piece_count}|{price}|{tot_amount}|{s_date}|{(note or '').strip()}"
        payload_hash = hashlib.md5(raw_fp.encode("utf-8")).hexdigest()
        dedup_keys = [f"fegg_hash:{payload_hash}"]
        if idempotency_key:
            dedup_keys.append(f"fegg_key:{uid}:{idempotency_key}")

        with _sale_idempotency_lock:
            now_ts = time.time()
            expired = [k for k, v in _sale_idempotency_cache.items() if now_ts - v.get("timestamp", 0) > 8.0]
            for k in expired:
                _sale_idempotency_cache.pop(k, None)

            for dk in dedup_keys:
                if dk in _sale_idempotency_cache:
                    entry = _sale_idempotency_cache[dk]
                    if now_ts - entry.get("timestamp", 0) <= 8.0:
                        if entry.get("in_progress"):
                            return jsonify({"ok": True, "message": "İşlem kaydediliyor...", "deduplicated": True})
                        if entry.get("response"):
                            resp = dict(entry["response"])
                            resp["deduplicated"] = True
                            return jsonify(resp)

            for dk in dedup_keys:
                _sale_idempotency_cache[dk] = {"timestamp": now_ts, "in_progress": True, "response": None}

        ok, msg, sale_id = cloud_db.add_farm_egg_sale(
            customer_name=cust,
            box_count=boxes,
            unit_price=price,
            sale_date=s_date,
            source=source,
            note=note,
            user_id=uid,
            unit_type=unit_type,
            piece_count=piece_count,
            total_amount=tot_amount,
        )
        with _sale_idempotency_lock:
            if ok:
                finish_ts = time.time()
                resp_payload = {"ok": True, "message": msg, "sale_id": sale_id}
                for dk in dedup_keys:
                    _sale_idempotency_cache[dk] = {"timestamp": finish_ts, "in_progress": False, "response": resp_payload}
            else:
                for dk in dedup_keys:
                    _sale_idempotency_cache.pop(dk, None)

        if not ok:
            return jsonify({"ok": False, "message": msg}), 400
        notify_data_change(uid)
        return jsonify({"ok": True, "message": msg, "sale_id": sale_id})

    # GET
    cust = request.args.get("customer_name")
    start = request.args.get("start_date")
    end = request.args.get("end_date")
    source = request.args.get("source")
    limit = request.args.get("limit", type=int)
    include_cancelled = request.args.get("include_cancelled", "false").lower() == "true"
    sales = cloud_db.get_farm_egg_sales(
        user_id=uid,
        customer_name=cust,
        start_date=start,
        end_date=end,
        source=source,
        limit=limit,
        include_cancelled=include_cancelled,
    )
    return jsonify({"ok": True, "sales": sales})


@app.route("/api/farm/egg-sales/<int:sale_id>", methods=["DELETE"])
@app.route("/api/farm/egg-sales/<int:sale_id>/cancel", methods=["POST"])
def api_farm_delete_egg_sale(sale_id: int):
    denied = enforce_permission("farm:manage", "FARM_EGG_SALE_CANCEL")
    if denied:
        return denied
    uid = get_current_user_id()
    data = request.get_json(silent=True) or {}
    cancel_reason = (data.get("cancel_reason") or data.get("reason") or request.args.get("cancel_reason") or "").strip()
    cancel_note = (data.get("cancel_note") or data.get("note") or request.args.get("cancel_note") or "").strip()
    actor = get_current_actor()
    ok, msg = cloud_db.delete_farm_egg_sale(
        sale_id,
        user_id=uid,
        cancel_reason=cancel_reason,
        cancel_note=cancel_note,
        actor=actor,
        require_reason=True,
    )
    if ok:
        notify_data_change(uid)
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)


@app.route("/api/farm/feed-purchases", methods=["GET", "POST"])
def api_farm_feed_purchases():
    denied = enforce_permission("farm:manage", "FARM_FEED_PURCHASES_ACCESS")
    if denied:
        return denied
    uid = get_current_user_id()
    if request.method == "POST":
        data = request.json or {}
        bags = data.get("bag_count")
        price = data.get("unit_price", 0.0)
        total = data.get("total_amount", 0.0)
        p_date = data.get("purchase_date")
        supplier = data.get("supplier")
        note = data.get("note")
        idempotency_key = (data.get("idempotency_key") or "").strip()

        raw_fp = f"farm_feed|{uid}|{bags}|{price}|{total}|{p_date}|{(supplier or '').strip()}|{(note or '').strip()}"
        payload_hash = hashlib.md5(raw_fp.encode("utf-8")).hexdigest()
        dedup_keys = [f"ffeed_hash:{payload_hash}"]
        if idempotency_key:
            dedup_keys.append(f"ffeed_key:{uid}:{idempotency_key}")

        with _sale_idempotency_lock:
            now_ts = time.time()
            for dk in dedup_keys:
                if dk in _sale_idempotency_cache:
                    entry = _sale_idempotency_cache[dk]
                    if now_ts - entry.get("timestamp", 0) <= 8.0:
                        if entry.get("in_progress"):
                            return jsonify({"ok": True, "message": "İşlem kaydediliyor...", "deduplicated": True})
                        if entry.get("response"):
                            resp = dict(entry["response"])
                            resp["deduplicated"] = True
                            return jsonify(resp)

            for dk in dedup_keys:
                _sale_idempotency_cache[dk] = {"timestamp": now_ts, "in_progress": True, "response": None}

        ok, msg, pid = cloud_db.add_farm_feed_purchase(
            bag_count=bags,
            unit_price=price,
            total_amount=total,
            purchase_date=p_date,
            supplier=supplier,
            note=note,
            user_id=uid,
        )
        with _sale_idempotency_lock:
            if ok:
                finish_ts = time.time()
                resp_payload = {"ok": True, "message": msg, "purchase_id": pid}
                for dk in dedup_keys:
                    _sale_idempotency_cache[dk] = {"timestamp": finish_ts, "in_progress": False, "response": resp_payload}
            else:
                for dk in dedup_keys:
                    _sale_idempotency_cache.pop(dk, None)

        if not ok:
            return jsonify({"ok": False, "message": msg}), 400
        notify_data_change(uid)
        return jsonify({"ok": True, "message": msg, "purchase_id": pid})

    # GET
    start = request.args.get("start_date")
    end = request.args.get("end_date")
    limit = request.args.get("limit", type=int)
    include_cancelled = request.args.get("include_cancelled", "false").lower() == "true"
    purchases = cloud_db.get_farm_feed_purchases(
        user_id=uid,
        start_date=start,
        end_date=end,
        limit=limit,
        include_cancelled=include_cancelled,
    )
    summary = cloud_db.get_farm_feed_summary(user_id=uid)
    return jsonify({"ok": True, "purchases": purchases, "summary": summary})


@app.route("/api/farm/feed-purchases/<int:purchase_id>", methods=["DELETE"])
@app.route("/api/farm/feed-purchases/<int:purchase_id>/cancel", methods=["POST"])
def api_farm_delete_feed_purchase(purchase_id: int):
    denied = enforce_permission("farm:manage", "FARM_FEED_PURCHASE_CANCEL")
    if denied:
        return denied
    uid = get_current_user_id()
    data = request.get_json(silent=True) or {}
    cancel_reason = (data.get("cancel_reason") or data.get("reason") or request.args.get("cancel_reason") or "").strip()
    cancel_note = (data.get("cancel_note") or data.get("note") or request.args.get("cancel_note") or "").strip()
    actor = get_current_actor()
    ok, msg = cloud_db.delete_farm_feed_purchase(
        purchase_id,
        user_id=uid,
        cancel_reason=cancel_reason,
        cancel_note=cancel_note,
        actor=actor,
        require_reason=True,
    )
    if ok:
        notify_data_change(uid)
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)


@app.route("/api/farm/analytics", methods=["GET"])
def api_farm_analytics():
    denied = enforce_permission("farm:manage", "FARM_ANALYTICS_VIEW")
    if denied:
        return denied
    uid = get_current_user_id()
    from app.services.farm_service import FarmService
    f_svc = FarmService(cloud_db)
    cust = request.args.get("customer_name")
    period = request.args.get("period", "weekly")
    start = request.args.get("start_date")
    end = request.args.get("end_date")
    source = request.args.get("source")

    analysis = f_svc.get_egg_sales_analysis(
        customer_name=cust,
        period=period,
        start_date=start,
        end_date=end,
        source=source,
        user_id=uid,
    )
    return jsonify({"ok": True, "data": analysis})


def _background_sync_loop():
    import time
    import urllib.request
    logger.info("Firebase 7/24 Arka Plan Otomatik Senkronizasyon ve Keep-Alive Servisi başlatıldı.")
    # Sunucu ilk açılışında tek seferlik initial hydration (verileri buluttan SQLite'a çekme)
    try:
        cloud_db.ensure_initial_hydration()
    except Exception as exc:
        logger.warning(f"Sunucu açılışı initial hydration uyarısı: {exc}")

    last_self_ping = time.time() - 150
    while True:
        try:
            time.sleep(25)
            # Kopmuş veya ardışık hata veren Firebase gRPC bağlantısını otomatik iyileştir
            cloud_db.check_firebase_health()
            cloud_db.push_offline_data_to_firebase()

            # Render ücretsiz sürümde sunucunun uykuya dalmasını önlemek için kendi dış URL'sine 3 dakikada bir ping at
            now_ts = time.time()
            target_url = os.environ.get("KEEP_ALIVE_URL") or os.environ.get("RENDER_EXTERNAL_URL") or _detected_external_url
            if target_url and (now_ts - last_self_ping >= 180):
                last_self_ping = now_ts
                try:
                    ping_url = f"{target_url.rstrip('/')}/ping"
                    req = urllib.request.Request(
                        ping_url,
                        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) KeepAlive/1.0"},
                    )
                    with urllib.request.urlopen(req, timeout=10) as _:
                        pass
                except Exception as ping_err:
                    logger.debug(f"Self keep-alive ping uyarısı: {ping_err}")
        except Exception as exc:
            logger.debug(f"Arka plan senkronizasyon uyarısı: {exc}")


@app.route("/api/web-orders", methods=["GET"])
def api_get_web_orders():
    """Gallus Mama web sitesinden gelen siparişleri, puan durumlarını ve bülten maillerini getirir."""
    try:
        data = cloud_db.sync_and_get_web_orders_and_subscribers()
        return jsonify({
            "success": True,
            "orders": data.get("orders", []),
            "subscribers": data.get("subscribers", []),
            "subscriberDetails": data.get("subscriberDetails", []),
            "members": data.get("members", [])
        })
    except Exception as exc:
        logger.error(f"Web siparişleri alınırken hata: {exc}")
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/web-orders/approve", methods=["POST"])
def api_approve_web_order():
    """Web siparişini onaylar ve müşterinin (%1 * 100) puanını hesabında aktif hale getirir."""
    try:
        body = request.get_json(silent=True) or {}
        order_id = str(body.get("orderId") or body.get("id") or "").strip()
        ok, msg = cloud_db.approve_web_order_points(order_id)
        if ok:
            return jsonify({"success": True, "message": msg})
        return jsonify({"success": False, "error": msg}), 400
    except Exception as exc:
        logger.error(f"Web siparişi onaylama hatası: {exc}")
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/newsletter-subscribers", methods=["GET"])
def api_get_newsletter_subscribers():
    """Bültene abone olan ve üye olan tüm e-postaları toplu kopyalama için döndürür."""
    try:
        data = cloud_db.sync_and_get_web_orders_and_subscribers()
        return jsonify({
            "success": True,
            "subscribers": data.get("subscribers", []),
            "subscriberDetails": data.get("subscriberDetails", []),
            "count": len(data.get("subscribers", []))
        })
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/push/vapid-public-key", methods=["GET"])
def api_push_vapid_public_key():
    keys = get_vapid_keys()
    return jsonify({"ok": True, "public_key": keys["public_key"]})


@app.route("/api/push/subscribe", methods=["POST"])
def api_push_subscribe():
    user_id = get_current_user_id() or 1
    data = request.get_json(silent=True) or {}
    subscription = data.get("subscription") or data
    device_label = str(data.get("device_label") or request.headers.get("User-Agent") or "")
    ok, msg = save_subscription(subscription, user_id=user_id, device_label=device_label)
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)


@app.route("/api/push/unsubscribe", methods=["POST"])
def api_push_unsubscribe():
    data = request.get_json(silent=True) or {}
    endpoint = str(data.get("endpoint") or "").strip()
    removed = remove_subscription(endpoint)
    return jsonify({"ok": True, "removed": removed})


@app.route("/api/push/test", methods=["POST"])
def api_push_test():
    user_id = get_current_user_id()
    result = send_push_to_all(
        title="🔔 Bildirim Testi Başarılı!",
        body="Artık POS'tan her satış yapıldığında telefonunuza anında bildirim gelecek.",
        url="/",
        tag="pos-test",
        user_id=user_id,
    )
    return jsonify({"ok": result["sent"] > 0, **result})


_bg_sync_started = False
_bg_sync_lock = threading.Lock()

def start_background_sync_if_needed():
    global _bg_sync_started
    with _bg_sync_lock:
        if not _bg_sync_started:
            _bg_sync_started = True
            t_sync = threading.Thread(target=_background_sync_loop, daemon=True)
            t_sync.start()
            logger.info("Firebase 7/24 Arka Plan Otomatik Senkronizasyon Servisi başlatıldı.")

start_background_sync_if_needed()

def run_web_server_in_thread(host: str = "0.0.0.0", port: Optional[int] = None) -> list:
    """Flask Web sunucusunu HTTP (Port 5000) ve HTTPS (Port 5001) modlarında çalıştırır."""
    server_port = port or int(os.environ.get("PORT", 5000))
    cert_path = DATA_DIR / "cert.pem"
    key_path = DATA_DIR / "key.pem"

    threads = []

    # 0. Arka Plan Firebase Otomatik Senkronizasyon Servisi
    start_background_sync_if_needed()

    # 1. Standart HTTP Sunucusu (Port 5000 - PC ve Mobil için Kolay Erişim)
    t_http = threading.Thread(
        target=lambda: app.run(host=host, port=server_port, debug=False, use_reloader=False, threaded=True),
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
                target=lambda: app.run(host=host, port=ssl_port, ssl_context=ssl_ctx, debug=False, use_reloader=False, threaded=True),
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
