from typing import Any, Dict, List, Optional


def _compare_metric(system_val: float, entered_val: Optional[float]) -> Dict[str, Any]:
    """Tek bir mutabakat kalemi için Sistem | Girilen | Fark ve durum üretir."""
    sys_r = round(float(system_val or 0.0), 2)
    if entered_val is None:
        return {
            "system": sys_r,
            "entered": None,
            "diff": None,
            "status": "pending",
            "label": "Giriş bekleniyor",
        }
    ent_r = round(float(entered_val), 2)
    diff = round(ent_r - sys_r, 2)
    if abs(diff) < 0.005:
        return {
            "system": sys_r,
            "entered": ent_r,
            "diff": 0.0,
            "status": "match",
            "label": "Tutuyor",
        }
    if diff > 0:
        return {
            "system": sys_r,
            "entered": ent_r,
            "diff": diff,
            "status": "surplus",
            "label": f"+{diff:.2f} ₺ Fazla",
        }
    return {
        "system": sys_r,
        "entered": ent_r,
        "diff": diff,
        "status": "shortage",
        "label": f"{diff:.2f} ₺ Eksik",
    }


def calculate_day_reconciliation(
    *,
    opening_amount: float = 0.0,
    cash_sales: float = 0.0,
    pos_sales: float = 0.0,
    internet_sales: float = 0.0,
    cash_expenses: float = 0.0,
    card_expenses: float = 0.0,
    patron_withdrawals: float = 0.0,
    other_cash_in: float = 0.0,
    other_cash_out: float = 0.0,
    # Gelecek faz (Veresiye Tahsilatı) için genişletilebilir kalemler:
    credit_collections_cash: float = 0.0,
    credit_collections_pos: float = 0.0,
    # Gün sonu kullanıcı girişleri (opsiyonel önizleme / kapanış):
    counted_cash: Optional[float] = None,
    entered_pos_total: Optional[float] = None,
    entered_expenses_total: Optional[float] = None,
) -> Dict[str, Any]:
    """Genişletilebilir gün sonu kasa & POS mutabakat hesaplama motoru.

    Formüller:
      - Beklenen nakit = Açılış kasası + Nakit satışlar − Nakit giderler − Patron çekimleri
                         + Diğer nakit girişler − Diğer nakit çıkışlar (+ Veresiye nakit tahsilat)
      - Beklenen POS   = Sistemdeki POS satışları (+ Veresiye POS tahsilat)
    """
    opening = round(float(opening_amount or 0.0), 2)
    c_sales = round(float(cash_sales or 0.0), 2)
    p_sales = round(float(pos_sales or 0.0), 2)
    net_sales = round(float(internet_sales or 0.0), 2)
    c_exp = round(float(cash_expenses or 0.0), 2)
    card_exp = round(float(card_expenses or 0.0), 2)
    p_withdraw = round(float(patron_withdrawals or 0.0), 2)
    o_in = round(float(other_cash_in or 0.0), 2)
    o_out = round(float(other_cash_out or 0.0), 2)
    cc_cash = round(float(credit_collections_cash or 0.0), 2)
    cc_pos = round(float(credit_collections_pos or 0.0), 2)

    # Genişletilebilir nakit kalem listesi (ileride yeni kalem eklemek için tek nokta)
    cash_formula_items: List[Dict[str, Any]] = [
        {"key": "opening_amount", "label": "Açılış Kasası", "sign": "+", "amount": opening},
        {"key": "cash_sales", "label": "Nakit Satışlar", "sign": "+", "amount": c_sales},
        {"key": "cash_expenses", "label": "Nakit Giderler", "sign": "-", "amount": c_exp},
        {"key": "patron_withdrawals", "label": "Patron Çekimleri", "sign": "-", "amount": p_withdraw},
        {"key": "other_cash_in", "label": "Diğer Kasa Girişleri", "sign": "+", "amount": o_in},
        {"key": "other_cash_out", "label": "Diğer Kasa Çıkışları", "sign": "-", "amount": o_out},
    ]
    if cc_cash > 0:
        cash_formula_items.append(
            {"key": "credit_collections_cash", "label": "Veresiye Nakit Tahsilat", "sign": "+", "amount": cc_cash}
        )

    expected_cash = 0.0
    for item in cash_formula_items:
        if item["sign"] == "+":
            expected_cash += item["amount"]
        else:
            expected_cash -= item["amount"]
    expected_cash = round(expected_cash, 2)

    pos_formula_items: List[Dict[str, Any]] = [
        {"key": "pos_sales", "label": "POS (Kart) Satışları", "sign": "+", "amount": p_sales},
    ]
    if cc_pos > 0:
        pos_formula_items.append(
            {"key": "credit_collections_pos", "label": "Veresiye POS Tahsilat", "sign": "+", "amount": cc_pos}
        )
    expected_pos = round(sum(i["amount"] for i in pos_formula_items), 2)

    total_expenses = round(c_exp + card_exp, 2)
    eff_entered_expenses = (
        round(float(entered_expenses_total), 2)
        if entered_expenses_total is not None
        else (total_expenses if (counted_cash is not None or entered_pos_total is not None) else None)
    )

    comparison = {
        "cash": _compare_metric(expected_cash, counted_cash),
        "pos": _compare_metric(expected_pos, entered_pos_total),
        "expenses": _compare_metric(total_expenses, eff_entered_expenses),
    }

    has_difference = any(
        c["diff"] is not None and abs(float(c["diff"])) >= 0.005
        for c in comparison.values()
    )

    return {
        "opening_amount": opening,
        "cash_sales": c_sales,
        "pos_sales": p_sales,
        "internet_sales": net_sales,
        "cash_expenses": c_exp,
        "card_expenses": card_exp,
        "total_expenses": total_expenses,
        "patron_withdrawals": p_withdraw,
        "other_cash_in": o_in,
        "other_cash_out": o_out,
        "credit_collections_cash": cc_cash,
        "credit_collections_pos": cc_pos,
        "expected_cash": expected_cash,
        "expected_pos": expected_pos,
        "cash_formula_items": cash_formula_items,
        "pos_formula_items": pos_formula_items,
        "comparison": comparison,
        "has_difference": has_difference,
    }


def resolve_day_cashier(day: Dict[str, Any]) -> str:
    """O gün kasada olan sorumlu çalışanı/kullanıcıyı belirler."""
    opened_role = str(day.get("opened_by_role") or "").strip().lower()
    closed_role = str(day.get("closed_by_role") or "").strip().lower()
    opened_by = str(day.get("opened_by") or "").strip()
    closed_by = str(day.get("closed_by") or "").strip()

    if closed_role == "calisan" and closed_by:
        return closed_by
    if opened_role == "calisan" and opened_by:
        return opened_by
    return closed_by or opened_by or "Patron"


def has_day_discrepancy(day: Dict[str, Any]) -> bool:
    for key in ("cash_diff", "pos_diff", "expense_diff"):
        val = day.get(key)
        if val is not None and abs(float(val)) >= 0.005:
            return True
    return False


def analyze_discrepancy_history(
    closed_days: List[Dict[str, Any]],
    *,
    repeat_threshold: int = 3,
) -> Dict[str, Any]:
    """Kapanmış günlerden fark geçmişi, çalışan bazında toplam/aylık rapor ve
    aynı çalışanda üst üste 3+ gün tekrar uyarılarını üretir.
    """
    # Kronolojik sıralama (streak hesabı için ASC)
    ordered = sorted(closed_days, key=lambda d: str(d.get("business_date") or ""))

    streaks: Dict[str, int] = {}
    max_streaks: Dict[str, int] = {}
    last_diff_date: Dict[str, str] = {}
    by_emp: Dict[str, Dict[str, Any]] = {}
    monthly_map: Dict[tuple[str, str], Dict[str, Any]] = {}
    records: List[Dict[str, Any]] = []

    for d in ordered:
        b_date = str(d.get("business_date") or "")[:10]
        cashier = resolve_day_cashier(d)
        c_diff = round(float(d.get("cash_diff") or 0.0), 2)
        p_diff = round(float(d.get("pos_diff") or 0.0), 2)
        e_diff = round(float(d.get("expense_diff") or 0.0), 2)
        is_diff = has_day_discrepancy(d)

        if is_diff:
            streaks[cashier] = streaks.get(cashier, 0) + 1
            if streaks[cashier] > max_streaks.get(cashier, 0):
                max_streaks[cashier] = streaks[cashier]
            last_diff_date[cashier] = b_date

            net_diff = round(c_diff + p_diff + e_diff, 2)
            abs_diff = round(abs(c_diff) + abs(p_diff) + abs(e_diff), 2)

            records.append({
                "id": d.get("id"),
                "business_date": b_date,
                "cashier_name": cashier,
                "opened_by": d.get("opened_by") or "",
                "closed_by": d.get("closed_by") or "",
                "cash_diff": c_diff,
                "pos_diff": p_diff,
                "expense_diff": e_diff,
                "net_diff": net_diff,
                "abs_total_diff": abs_diff,
                "closing_note": d.get("closing_note") or "",
                "closed_at": d.get("closed_at") or "",
                "streak_at_day": streaks[cashier],
            })

            emp_acc = by_emp.setdefault(cashier, {
                "cashier_name": cashier,
                "discrepancy_days": 0,
                "total_cash_diff": 0.0,
                "total_pos_diff": 0.0,
                "total_expense_diff": 0.0,
                "net_total_diff": 0.0,
                "abs_total_diff": 0.0,
                "current_streak": 0,
                "max_streak": 0,
                "has_repeat_alert": False,
            })
            emp_acc["discrepancy_days"] += 1
            emp_acc["total_cash_diff"] = round(emp_acc["total_cash_diff"] + c_diff, 2)
            emp_acc["total_pos_diff"] = round(emp_acc["total_pos_diff"] + p_diff, 2)
            emp_acc["total_expense_diff"] = round(emp_acc["total_expense_diff"] + e_diff, 2)
            emp_acc["net_total_diff"] = round(emp_acc["net_total_diff"] + net_diff, 2)
            emp_acc["abs_total_diff"] = round(emp_acc["abs_total_diff"] + abs_diff, 2)

            m_key = b_date[:7] if len(b_date) >= 7 else b_date
            m_acc = monthly_map.setdefault((m_key, cashier), {
                "month_key": m_key,
                "cashier_name": cashier,
                "discrepancy_days": 0,
                "total_cash_diff": 0.0,
                "total_pos_diff": 0.0,
                "total_expense_diff": 0.0,
                "net_total_diff": 0.0,
                "abs_total_diff": 0.0,
            })
            m_acc["discrepancy_days"] += 1
            m_acc["total_cash_diff"] = round(m_acc["total_cash_diff"] + c_diff, 2)
            m_acc["total_pos_diff"] = round(m_acc["total_pos_diff"] + p_diff, 2)
            m_acc["total_expense_diff"] = round(m_acc["total_expense_diff"] + e_diff, 2)
            m_acc["net_total_diff"] = round(m_acc["net_total_diff"] + net_diff, 2)
            m_acc["abs_total_diff"] = round(m_acc["abs_total_diff"] + abs_diff, 2)
        else:
            # Fark olmayan kapanış seriyi sıfırlar
            streaks[cashier] = 0

    repeat_alerts: List[Dict[str, Any]] = []
    for cashier, acc in by_emp.items():
        cur_s = streaks.get(cashier, 0)
        max_s = max_streaks.get(cashier, 0)
        acc["current_streak"] = cur_s
        acc["max_streak"] = max_s
        acc["has_repeat_alert"] = (cur_s >= repeat_threshold)
        if cur_s >= repeat_threshold:
            repeat_alerts.append({
                "cashier_name": cashier,
                "consecutive_days": cur_s,
                "max_streak": max_s,
                "last_date": last_diff_date.get(cashier, ""),
                "total_abs_diff": acc["abs_total_diff"],
                "message": (
                    f"🚨 TEKRAR UYARISI: '{cashier}' adlı çalışanda üst üste "
                    f"{cur_s} gün sonu farkı tespit edildi!"
                ),
            })

    records.reverse()  # En yeni tarih üstte
    by_employee = sorted(by_emp.values(), key=lambda x: (-x["current_streak"], -x["abs_total_diff"], x["cashier_name"]))
    monthly_by_employee = sorted(monthly_map.values(), key=lambda x: (x["month_key"], x["cashier_name"]), reverse=True)

    return {
        "records": records,
        "total_discrepancy_days": len(records),
        "total_abs_diff": round(sum(r["abs_total_diff"] for r in records), 2),
        "total_net_diff": round(sum(r["net_diff"] for r in records), 2),
        "by_employee": by_employee,
        "monthly_by_employee": monthly_by_employee,
        "repeat_alerts": repeat_alerts,
        "has_alerts": bool(records or repeat_alerts),
    }
