import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from app.config import (
    HIGH_AMOUNT_WARNING_THRESHOLD,
    MAX_ALLOWED_SINGLE_AMOUNT,
    get_turkey_now_str,
)
from app.database.connection import Database

logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════════
# KULLANICI ROLLERİ VE YETKİ MATRİSİ
# ════════════════════════════════════════════════════════════════════
ROLE_PATRON = "patron"
ROLE_CALISAN = "calisan"

VALID_ROLES = (ROLE_PATRON, ROLE_CALISAN)

# İptal / İade için zorunlu seçmeli neden listesi
CANCEL_REASONS: List[str] = [
    "Müşteri iadesi",
    "Yanlış ürün / barkod girişi",
    "Yanlış tutar / miktar girişi",
    "Mükerrer (çift) kayıt",
    "Ödeme alınamadı / vazgeçildi",
    "Diğer",
]

# Kasa / Tahsilat hareket türleri
CASH_MOVEMENT_TYPES = ("tahsilat", "kasa_giris", "kasa_cikis")

ROLE_PERMISSIONS: Dict[str, set[str]] = {
    ROLE_CALISAN: {
        "sale:create",
        "sale:view_today",
        "product:view",
        "cash_day:open",
        "cash_day:close",
        "cash:view_today",
        "cash:in",
        "credit:view",
        "credit:collect",
    },
    ROLE_PATRON: {
        "sale:create",
        "sale:view_today",
        "sale:view_history",
        "sale:cancel",
        "sale:edit",
        "product:view",
        "product:manage",
        "expense:view",
        "expense:create",
        "expense:cancel",
        "cash_day:open",
        "cash_day:close",
        "cash:view_today",
        "cash:view",
        "cash:in",
        "cash:create",
        "cash:patron_withdraw",
        "cash:cancel",
        "credit:view",
        "credit:collect",
        "credit:cancel",
        "report:view",
        "profit:view",
        "cancelled_report:view",
        "audit:view",
        "farm:manage",
        "day:reopen",
        "settings:manage",
        "users:manage",
    },
}


def normalize_role(role: Optional[str]) -> str:
    r = str(role or "").strip().lower()
    if r in ("calisan", "çalışan", "staff", "employee", "kasiyer"):
        return ROLE_CALISAN
    return ROLE_PATRON


def has_permission(role: Optional[str], permission: str) -> bool:
    norm = normalize_role(role)
    return permission in ROLE_PERMISSIONS.get(norm, set())


def get_permissions_for_role(role: Optional[str]) -> List[str]:
    norm = normalize_role(role)
    return sorted(ROLE_PERMISSIONS.get(norm, set()))


def build_actor(
    name: Optional[str] = None,
    role: Optional[str] = None,
    user_id: Optional[int] = None,
    ip_address: Optional[str] = None,
) -> Dict[str, Any]:
    norm_role = normalize_role(role)
    default_name = "Patron" if norm_role == ROLE_PATRON else "Çalışan"
    return {
        "name": (name or "").strip() or default_name,
        "role": norm_role,
        "user_id": user_id,
        "ip_address": (ip_address or "").strip() or None,
    }


# ════════════════════════════════════════════════════════════════════
# NEGATİF / AŞIRI TUTAR DOĞRULAMA VE ONAY KONTROLÜ
# ════════════════════════════════════════════════════════════════════
def validate_monetary_amount(
    amount: Any,
    *,
    field_label: str = "Tutar",
    allow_zero: bool = False,
    confirm_high_amount: bool = False,
    warning_threshold: Optional[float] = None,
    max_allowed: Optional[float] = None,
) -> Dict[str, Any]:
    """Parasal tutarları doğrular.

    Dönüş sözlüğü:
      - ok (bool): İşleme devam edilebilir mi?
      - value (float | None): Temizlenmiş yuvarlanmış tutar
      - message (str): Hata veya uyarı mesajı
      - requires_confirmation (bool): Tutar yüksek eşiği aştıysa ve onay verilmediyse True
      - warning_threshold (float): Kullanılan uyarı eşiği
    """
    warn_limit = float(
        warning_threshold if warning_threshold is not None else HIGH_AMOUNT_WARNING_THRESHOLD
    )
    hard_limit = float(
        max_allowed if max_allowed is not None else MAX_ALLOWED_SINGLE_AMOUNT
    )

    try:
        val = float(amount)
    except (ValueError, TypeError):
        return {
            "ok": False,
            "value": None,
            "message": f"{field_label} geçerli bir sayı olmalıdır!",
            "requires_confirmation": False,
            "warning_threshold": warn_limit,
        }

    if val != val or val in (float("inf"), float("-inf")):
        return {
            "ok": False,
            "value": None,
            "message": f"{field_label} geçersiz bir sayısal değer içeriyor!",
            "requires_confirmation": False,
            "warning_threshold": warn_limit,
        }

    if val < 0:
        return {
            "ok": False,
            "value": None,
            "message": f"{field_label} negatif olamaz ({val:.2f} ₺)!",
            "requires_confirmation": False,
            "warning_threshold": warn_limit,
        }

    if not allow_zero and val == 0:
        return {
            "ok": False,
            "value": None,
            "message": f"{field_label} 0'dan büyük olmalıdır!",
            "requires_confirmation": False,
            "warning_threshold": warn_limit,
        }

    if val > hard_limit:
        return {
            "ok": False,
            "value": None,
            "message": f"{field_label} izin verilen üst sınırı ({hard_limit:,.2f} ₺) aşıyor!",
            "requires_confirmation": False,
            "warning_threshold": warn_limit,
        }

    rounded = round(val, 2)
    if rounded >= warn_limit and not confirm_high_amount:
        return {
            "ok": False,
            "value": rounded,
            "message": (
                f"⚠️ Yüksek tutar uyarısı: {field_label} {rounded:,.2f} ₺ "
                f"(eşik: {warn_limit:,.2f} ₺). İşlemi onaylamak istiyor musunuz?"
            ),
            "requires_confirmation": True,
            "warning_threshold": warn_limit,
        }

    return {
        "ok": True,
        "value": rounded,
        "message": "OK",
        "requires_confirmation": False,
        "warning_threshold": warn_limit,
    }


def validate_cancel_reason(
    reason: Optional[str],
    note: Optional[str] = None,
) -> Tuple[bool, str, str, str]:
    """İptal/iade için zorunlu neden ve isteğe bağlı serbest not doğrulaması."""
    clean_reason = str(reason or "").strip()
    clean_note = str(note or "").strip()

    if not clean_reason:
        return False, "İptal/iade nedeni seçmek zorunludur!", "", clean_note

    if clean_reason == "Diğer" and len(clean_note) < 3:
        return False, "'Diğer' seçildiğinde kısa bir açıklama notu girilmesi zorunludur!", clean_reason, clean_note

    return True, "OK", clean_reason, clean_note


MIN_CASH_OUT_DESCRIPTION_LEN = 5


def validate_cash_description(
    description: Optional[str],
    movement_type: str,
    *,
    confirm_short_description: bool = False,
) -> Dict[str, Any]:
    """Kasa hareketlerinde zorunlu açıklama ve kısa metin uyarısı doğrulaması."""
    clean = str(description or "").strip()
    m_type = str(movement_type or "").strip().lower()

    if not clean:
        return {
            "ok": False,
            "value": "",
            "message": "Açıklama boş bırakılamaz! Lütfen hareketin nedenini yazınız.",
            "requires_confirmation": False,
        }

    if m_type == "kasa_cikis" and len(clean) < MIN_CASH_OUT_DESCRIPTION_LEN and not confirm_short_description:
        return {
            "ok": False,
            "value": clean,
            "message": (
                f"⚠️ Açıklama çok kısa ('{clean}'). Kasadan çıkan nakit için en az "
                f"{MIN_CASH_OUT_DESCRIPTION_LEN} karakterlik açıklayıcı bir not yazmanız önerilir. "
                "Yine de bu açıklamayla devam etmek istiyor musunuz?"
            ),
            "requires_confirmation": True,
            "warning_type": "short_description",
        }

    return {
        "ok": True,
        "value": clean,
        "message": "OK",
        "requires_confirmation": False,
    }


# ════════════════════════════════════════════════════════════════════
# MERKEZİ DENETİM KAYDI (IMMUTABLE AUDIT LOG) SERVİSİ
# ════════════════════════════════════════════════════════════════════
def _to_json_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def log_audit(
    action: str,
    entity_type: str,
    entity_id: Any = None,
    *,
    old_value: Any = None,
    new_value: Any = None,
    reason: Optional[str] = None,
    actor: Optional[Dict[str, Any]] = None,
    user_id: Optional[int] = None,
    ip_address: Optional[str] = None,
    db: Optional[Database] = None,
    conn=None,
) -> Optional[int]:
    """Tüm modüllerin kullanabileceği tek merkezi denetim kaydı (audit log) fonksiyonu.

    Kayıtlar sadece eklenebilir (append-only); veritabanı trigger'ları UPDATE ve DELETE
    işlemlerini engeller.
    """
    act = actor or {}
    actor_name = str(act.get("name") or "Sistem").strip()
    actor_role = normalize_role(act.get("role")) if act.get("role") else ROLE_PATRON
    uid = user_id if user_id is not None else act.get("user_id")
    ip = (ip_address or act.get("ip_address") or "").strip() or None
    now = get_turkey_now_str()

    params = (
        uid,
        actor_name,
        actor_role,
        str(action).strip().upper(),
        str(entity_type).strip().lower(),
        str(entity_id) if entity_id is not None else None,
        _to_json_str(old_value),
        _to_json_str(new_value),
        (reason or "").strip() or None,
        ip,
        now,
    )

    sql = """
        INSERT INTO audit_logs (
            user_id, actor_name, actor_role, action, entity_type, entity_id,
            old_value, new_value, reason, ip_address, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    try:
        if conn is not None:
            cur = conn.execute(sql, params)
            return cur.lastrowid

        target_db = db or Database()
        with target_db.get_connection() as c:
            cur = c.execute(sql, params)
            return cur.lastrowid
    except Exception as exc:
        logger.warning("Audit log yazma uyarısı (%s/%s): %s", action, entity_type, exc)
        return None


def log_unauthorized_attempt(
    action: str,
    resource: str,
    *,
    actor: Optional[Dict[str, Any]] = None,
    user_id: Optional[int] = None,
    ip_address: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
    db: Optional[Database] = None,
) -> Optional[int]:
    """Yetkisiz işlem denemelerini engelleme anında denetim kaydına yazar."""
    payload = {
        "attempted_action": action,
        "resource": resource,
    }
    if details:
        payload.update(details)

    return log_audit(
        action="UNAUTHORIZED_ATTEMPT",
        entity_type="security",
        entity_id=resource,
        old_value=None,
        new_value=payload,
        reason=f"Yetkisiz işlem denemesi: {action}",
        actor=actor,
        user_id=user_id,
        ip_address=ip_address,
        db=db,
    )


def get_audit_logs(
    *,
    user_id: Optional[int] = None,
    entity_type: Optional[str] = None,
    action: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 200,
    db: Optional[Database] = None,
) -> List[Dict[str, Any]]:
    """Patron ekranı için denetim kayıtlarını listeler (salt okunur)."""
    target_db = db or Database()
    query = "SELECT * FROM audit_logs WHERE 1=1"
    params: List[Any] = []

    if user_id is not None:
        query += " AND (user_id = ? OR user_id IS NULL)"
        params.append(user_id)
    if entity_type and str(entity_type).strip():
        query += " AND entity_type = ?"
        params.append(str(entity_type).strip().lower())
    if action and str(action).strip():
        query += " AND action = ?"
        params.append(str(action).strip().upper())
    if start_date and str(start_date).strip():
        query += " AND created_at >= ?"
        params.append(f"{str(start_date).strip()} 00:00:00")
    if end_date and str(end_date).strip():
        query += " AND created_at <= ?"
        params.append(f"{str(end_date).strip()} 23:59:59")

    safe_limit = max(1, min(int(limit or 200), 1000))
    query += f" ORDER BY id DESC LIMIT {safe_limit}"

    with target_db.get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        results: List[Dict[str, Any]] = []
        for r in rows:
            item = dict(r)
            for field in ("old_value", "new_value"):
                raw = item.get(field)
                if isinstance(raw, str):
                    try:
                        item[field] = json.loads(raw)
                    except Exception:
                        pass
            results.append(item)
        return results
