"""Web Push (VAPID) notification service for sending background phone alerts on POS sales."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.config import DATA_DIR, get_turkey_now_str

logger = logging.getLogger(__name__)

_VAPID_FILE = DATA_DIR / "vapid_keys.json"
_SUBS_FILE = DATA_DIR / "push_subscriptions.json"
_VAPID_LOCK = threading.Lock()
_SUBS_LOCK = threading.Lock()

_VAPID_CLAIMS_SUB = os.getenv("VAPID_CLAIMS_EMAIL", "mailto:bildirim@gallusmama.com")


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _sub_doc_id(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:32]


def _generate_vapid_keypair() -> Dict[str, str]:
    """Generates a new EC P-256 VAPID keypair (PEM private key + base64url uncompressed public key)."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")

    public_numbers = private_key.public_key().public_numbers()
    x_bytes = public_numbers.x.to_bytes(32, "big")
    y_bytes = public_numbers.y.to_bytes(32, "big")
    public_raw = b"\x04" + x_bytes + y_bytes
    public_b64 = _b64url_encode(public_raw)
    return {"public_key": public_b64, "private_key": private_pem}


def _get_cloud_db():
    try:
        from app.database.cloud_db import CloudDatabase

        cdb = CloudDatabase()
        cdb.ensure_firebase()
        return cdb
    except Exception:
        return None


def get_vapid_keys() -> Dict[str, str]:
    """Returns persistent VAPID keys from env, Firestore, or local disk (auto-generating if missing)."""
    env_pub = os.getenv("VAPID_PUBLIC_KEY", "").strip()
    env_priv = os.getenv("VAPID_PRIVATE_KEY", "").strip()
    if env_pub and env_priv:
        return {"public_key": env_pub, "private_key": env_priv}

    with _VAPID_LOCK:
        # 1. Check local file
        if _VAPID_FILE.exists():
            try:
                data = json.loads(_VAPID_FILE.read_text(encoding="utf-8"))
                if data.get("public_key") and data.get("private_key"):
                    return data
            except Exception as exc:
                logger.warning("VAPID local read error: %s", exc)

        # 2. Check Firestore so keys survive Render restarts
        cdb = _get_cloud_db()
        if cdb and cdb.firestore_db:
            try:
                doc = cdb.firestore_db.collection("system_settings").document("web_push_vapid").get()
                if doc.exists:
                    data = doc.to_dict() or {}
                    if data.get("public_key") and data.get("private_key"):
                        _save_vapid_local(data)
                        return {"public_key": data["public_key"], "private_key": data["private_key"]}
            except Exception as exc:
                logger.warning("VAPID Firestore read error: %s", exc)

        # 3. Generate new keypair and persist everywhere
        keys = _generate_vapid_keypair()
        _save_vapid_local(keys)
        if cdb and cdb.firestore_db:
            try:
                cdb.firestore_db.collection("system_settings").document("web_push_vapid").set({
                    "public_key": keys["public_key"],
                    "private_key": keys["private_key"],
                    "updated_at": get_turkey_now_str(),
                })
            except Exception as exc:
                logger.warning("VAPID Firestore write error: %s", exc)
        return keys


def _save_vapid_local(keys: Dict[str, str]) -> None:
    try:
        _VAPID_FILE.parent.mkdir(parents=True, exist_ok=True)
        _VAPID_FILE.write_text(json.dumps(keys, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("VAPID local write error: %s", exc)


def _load_local_subs() -> Dict[str, Dict[str, Any]]:
    if not _SUBS_FILE.exists():
        return {}
    try:
        data = json.loads(_SUBS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_local_subs(subs: Dict[str, Dict[str, Any]]) -> None:
    try:
        _SUBS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SUBS_FILE.write_text(json.dumps(subs, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("Push subscriptions local write error: %s", exc)


def save_subscription(subscription: Dict[str, Any], user_id: int = 1, device_label: str = "") -> tuple[bool, str]:
    """Saves or updates a Web Push subscription in local disk and Firestore."""
    endpoint = (subscription or {}).get("endpoint", "").strip()
    keys = (subscription or {}).get("keys") or {}
    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        return False, "Geçersiz bildirim aboneliği verisi."

    doc_id = _sub_doc_id(endpoint)
    record = {
        "id": doc_id,
        "user_id": int(user_id),
        "endpoint": endpoint,
        "keys": {
            "p256dh": str(keys["p256dh"]),
            "auth": str(keys["auth"]),
        },
        "device_label": device_label[:120] if device_label else "",
        "updated_at": get_turkey_now_str(),
    }

    with _SUBS_LOCK:
        subs = _load_local_subs()
        subs[doc_id] = record
        _save_local_subs(subs)

    cdb = _get_cloud_db()
    if cdb and cdb.firestore_db:
        try:
            cdb.firestore_db.collection("push_subscriptions").document(doc_id).set(record)
        except Exception as exc:
            logger.warning("Push subscription Firestore save error: %s", exc)

    return True, "Bildirim aboneliği kaydedildi."


def remove_subscription(endpoint: str) -> bool:
    """Removes a Web Push subscription by endpoint."""
    endpoint = (endpoint or "").strip()
    if not endpoint:
        return False
    doc_id = _sub_doc_id(endpoint)

    with _SUBS_LOCK:
        subs = _load_local_subs()
        subs.pop(doc_id, None)
        _save_local_subs(subs)

    cdb = _get_cloud_db()
    if cdb and cdb.firestore_db:
        try:
            cdb.firestore_db.collection("push_subscriptions").document(doc_id).delete()
        except Exception as exc:
            logger.warning("Push subscription Firestore delete error: %s", exc)
    return True


def list_subscriptions(user_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Lists active push subscriptions from Firestore (merged with local cache)."""
    merged: Dict[str, Dict[str, Any]] = {}
    with _SUBS_LOCK:
        merged.update(_load_local_subs())

    cdb = _get_cloud_db()
    if cdb and cdb.firestore_db:
        try:
            docs = cdb.firestore_db.collection("push_subscriptions").stream()
            for doc in docs:
                d = doc.to_dict() or {}
                if d.get("endpoint") and d.get("keys"):
                    merged[doc.id] = d
            with _SUBS_LOCK:
                _save_local_subs(merged)
        except Exception as exc:
            logger.warning("Push subscription Firestore list error: %s", exc)

    result = list(merged.values())
    if user_id is not None:
        result = [s for s in result if int(s.get("user_id", 1)) == int(user_id)]
    return result


def send_push_to_all(
    title: str,
    body: str,
    url: str = "/",
    tag: str = "pos-sale",
    user_id: Optional[int] = None,
) -> Dict[str, int]:
    """Sends a Web Push notification to all subscribed devices."""
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        logger.warning("pywebpush yüklü değil; telefon bildirimi gönderilemedi.")
        return {"sent": 0, "failed": 0, "removed": 0}

    subs = list_subscriptions(user_id=user_id)
    if not subs:
        return {"sent": 0, "failed": 0, "removed": 0}

    vapid = get_vapid_keys()
    payload = json.dumps({
        "title": title,
        "body": body,
        "url": url,
        "tag": tag,
        "timestamp": get_turkey_now_str("%H:%M"),
    }, ensure_ascii=False)

    sent = 0
    failed = 0
    removed = 0

    for sub in subs:
        sub_info = {
            "endpoint": sub["endpoint"],
            "keys": sub["keys"],
        }
        try:
            webpush(
                subscription_info=sub_info,
                data=payload,
                vapid_private_key=vapid["private_key"],
                vapid_claims={"sub": _VAPID_CLAIMS_SUB},
                ttl=3600,
            )
            sent += 1
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, 410):
                remove_subscription(sub["endpoint"])
                removed += 1
            else:
                logger.warning("WebPush gönderim hatası (status=%s): %s", status, exc)
                failed += 1
        except Exception as exc:
            logger.warning("WebPush beklenmeyen hata: %s", exc)
            failed += 1

    return {"sent": sent, "failed": failed, "removed": removed}


def format_sale_notification(
    items: List[Dict[str, Any]],
    total_amount: Optional[float] = None,
    channel: str = "magaza",
    customer_name: Optional[str] = None,
) -> tuple[str, str]:
    """Builds a concise Turkish title and body for a POS sale push notification."""
    calc_total = 0.0
    parts: List[str] = []
    total_qty = 0

    for it in items or []:
        name = str(it.get("product_name") or it.get("name") or f"Ürün #{it.get('product_id', '')}").strip()
        qty = float(it.get("quantity", 1) or 1)
        price = float(it.get("unit_price", 0) or 0)
        calc_total += qty * price
        total_qty += int(qty) if qty == int(qty) else 1
        qty_str = str(int(qty)) if qty == int(qty) else f"{qty:.2f}"
        if len(parts) < 3:
            parts.append(f"{name} x{qty_str}")

    final_total = float(total_amount) if total_amount is not None else calc_total
    title = f"💰 Yeni POS Satışı: {final_total:,.2f} ₺"

    items_summary = ", ".join(parts)
    if len(items or []) > 3:
        items_summary += f" (+{len(items) - 3} ürün)"

    channel_label = "İnternet" if str(channel).lower() in ("internet", "online", "web") else "Mağaza POS"
    time_str = get_turkey_now_str("%H:%M")
    body_parts = [p for p in [items_summary, customer_name, f"{channel_label} • {time_str}"] if p]
    return title, " | ".join(body_parts)


def send_sale_notification_async(
    items: List[Dict[str, Any]],
    total_amount: Optional[float] = None,
    channel: str = "magaza",
    customer_name: Optional[str] = None,
    user_id: Optional[int] = None,
) -> None:
    """Dispatches a POS sale push notification in a background daemon thread."""
    title, body = format_sale_notification(
        items=items,
        total_amount=total_amount,
        channel=channel,
        customer_name=customer_name,
    )

    def _worker():
        try:
            send_push_to_all(title=title, body=body, url="/", tag="pos-sale", user_id=user_id)
        except Exception as exc:
            logger.warning("Arka plan satış bildirimi hatası: %s", exc)

    threading.Thread(target=_worker, daemon=True).start()
