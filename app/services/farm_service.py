from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.database.cloud_db import CloudDatabase


class FarmService:
    """Çiftlik Yönetim Servisi:
    - Yumurta satışlarının kaydı, takibi ve müşteri bazlı dökümü
    - Pazartesiden Pazartesiye haftalık, aylık, 3 aylık gelişmiş tarih filtreleri
    - 1 çuval = 50 kg yem alımı ve tonaj hesabı
    - Firebase Firestore ile 2 yönlü offline-first senkronizasyon
    """

    def __init__(self, cloud_db: Optional[CloudDatabase] = None) -> None:
        self.cloud_db = cloud_db or CloudDatabase()

    @staticmethod
    def get_date_range_for_period(
        period: str = "all",
        custom_start: Optional[str] = None,
        custom_end: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Verilen periyot için [başlangıç_tarihi, bitiş_tarihi] (YYYY-MM-DD) döner.
        - 'weekly': Pazartesiden Pazartesiye (En son Pazartesi 00:00 ile sonraki Pazartesi 23:59)
        - 'monthly': İçinde bulunulan takvim ayının 1'inden ay sonuna
        - '3_months': Son 90 gün (3 takvim ayı geriye)
        - 'all': Tüm zamanlar (Filtresiz)
        - 'custom': Özel aralık
        """
        today = datetime.now().date()
        clean_period = (period or "all").lower().strip()

        if clean_period == "weekly":
            # Pazartesi = 0, Pazar = 6
            monday = today - timedelta(days=today.weekday())
            # Pazartesiden bir sonraki Pazartesiye kadar
            next_monday = monday + timedelta(days=7)
            return monday.strftime("%Y-%m-%d"), next_monday.strftime("%Y-%m-%d")

        elif clean_period == "monthly":
            month_start = date(today.year, today.month, 1)
            if today.month == 12:
                next_month = date(today.year + 1, 1, 1)
            else:
                next_month = date(today.year, today.month + 1, 1)
            month_end = next_month - timedelta(days=1)
            return month_start.strftime("%Y-%m-%d"), month_end.strftime("%Y-%m-%d")

        elif clean_period in ("3_months", "three_months", "quarterly"):
            start_date = today - timedelta(days=90)
            return start_date.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")

        elif clean_period == "custom":
            return custom_start, custom_end

        return None, None

    # ════════════════════════════════════════════════════════════════════
    # YUMURTA SATIŞ İŞLEMLERİ
    # ════════════════════════════════════════════════════════════════════

    def create_egg_sale(
        self,
        customer_name: str,
        box_count: float,
        unit_price: float,
        sale_date: Optional[str] = None,
        source: str = "Ciftlik",
        note: Optional[str] = None,
        user_id: Optional[int] = None,
    ) -> Tuple[bool, str, Optional[int]]:
        """Yeni yumurta satışı kaydeder."""
        return self.cloud_db.add_farm_egg_sale(
            customer_name=customer_name,
            box_count=box_count,
            unit_price=unit_price,
            sale_date=sale_date,
            source=source,
            note=note,
            user_id=user_id,
        )

    def delete_egg_sale(self, sale_id: int, user_id: Optional[int] = None) -> Tuple[bool, str]:
        """Yumurta satışını siler."""
        return self.cloud_db.delete_farm_egg_sale(sale_id, user_id=user_id)

    def get_customers(self, user_id: Optional[int] = None) -> List[str]:
        """Hatırlanacak müşteri isimleri listesini döner."""
        return self.cloud_db.get_farm_customers(user_id=user_id)

    def get_egg_sales_analysis(
        self,
        customer_name: Optional[str] = None,
        period: str = "all",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        source: Optional[str] = None,
        user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Gelişmiş filtreleme ve müşteri analiz verilerini üretir:
        - Müşteri adı belirtilmişse: O müşteriye satılan toplam koli, ciro, ortalama koli fiyatı ve hareketler
        - Müşteri adı belirtilmemişse: Tüm müşterilere yapılan genel toplam, ciro ve müşteri bazında döküm özeti
        """
        if period != "custom":
            calc_start, calc_end = self.get_date_range_for_period(period)
        else:
            calc_start, calc_end = start_date, end_date

        sales = self.cloud_db.get_farm_egg_sales(
            user_id=user_id,
            customer_name=customer_name,
            start_date=calc_start,
            end_date=calc_end,
            source=source,
        )

        total_boxes = round(sum(float(s.get("box_count", 0)) for s in sales), 2)
        total_revenue = round(sum(float(s.get("total_amount", 0)) for s in sales), 2)
        avg_box_price = round(total_revenue / total_boxes, 2) if total_boxes > 0 else 0.0

        # Müşteri bazında döküm (özet tablo)
        customers_map: Dict[str, Dict[str, Any]] = {}
        for s in sales:
            c_name = (s.get("customer_name") or "Bilinmeyen Müşteri").strip()
            if c_name not in customers_map:
                customers_map[c_name] = {
                    "customer_name": c_name,
                    "total_boxes": 0.0,
                    "total_revenue": 0.0,
                    "sale_count": 0,
                    "last_sale_date": s.get("sale_date", "")[:10],
                }
            customers_map[c_name]["total_boxes"] += float(s.get("box_count", 0))
            customers_map[c_name]["total_revenue"] += float(s.get("total_amount", 0))
            customers_map[c_name]["sale_count"] += 1
            curr_date = s.get("sale_date", "")[:10]
            if curr_date > customers_map[c_name]["last_sale_date"]:
                customers_map[c_name]["last_sale_date"] = curr_date

        customer_summary = []
        for c_dict in customers_map.values():
            c_dict["total_boxes"] = round(c_dict["total_boxes"], 2)
            c_dict["total_revenue"] = round(c_dict["total_revenue"], 2)
            c_dict["avg_price"] = (
                round(c_dict["total_revenue"] / c_dict["total_boxes"], 2)
                if c_dict["total_boxes"] > 0
                else 0.0
            )
            customer_summary.append(c_dict)

        customer_summary.sort(key=lambda x: x["total_boxes"], reverse=True)

        return {
            "period": period,
            "start_date": calc_start,
            "end_date": calc_end,
            "filter_customer": customer_name or "",
            "total_boxes": total_boxes,
            "total_revenue": total_revenue,
            "avg_box_price": avg_box_price,
            "total_sales_count": len(sales),
            "customer_count": len(customer_summary),
            "customer_summary": customer_summary,
            "sales": sales,
        }

    # ════════════════════════════════════════════════════════════════════
    # YEM ALIM İŞLEMLERİ
    # ════════════════════════════════════════════════════════════════════

    def create_feed_purchase(
        self,
        bag_count: float,
        unit_price: float = 0.0,
        total_amount: float = 0.0,
        purchase_date: Optional[str] = None,
        supplier: Optional[str] = None,
        note: Optional[str] = None,
        user_id: Optional[int] = None,
    ) -> Tuple[bool, str, Optional[int]]:
        """1 çuval = 50 kg esasına göre yem alımı kaydeder."""
        return self.cloud_db.add_farm_feed_purchase(
            bag_count=bag_count,
            unit_price=unit_price,
            total_amount=total_amount,
            purchase_date=purchase_date,
            supplier=supplier,
            note=note,
            user_id=user_id,
        )

    def delete_feed_purchase(self, purchase_id: int, user_id: Optional[int] = None) -> Tuple[bool, str]:
        """Yem alım kaydını siler."""
        return self.cloud_db.delete_farm_feed_purchase(purchase_id, user_id=user_id)

    def get_feed_analysis(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Yem alımları listesini ve özet tonaj/maliyet metriklerini döner."""
        purchases = self.cloud_db.get_farm_feed_purchases(
            user_id=user_id,
            start_date=start_date,
            end_date=end_date,
        )

        total_bags = round(sum(float(p.get("bag_count", 0)) for p in purchases), 2)
        total_kg = round(sum(float(p.get("total_weight_kg", 0)) for p in purchases), 2)
        total_tons = round(sum(float(p.get("total_weight_ton", 0)) for p in purchases), 3)
        total_cost = round(sum(float(p.get("total_amount", 0)) for p in purchases), 2)

        return {
            "total_bags": total_bags,
            "total_kg": total_kg,
            "total_tons": total_tons,
            "total_cost": total_cost,
            "purchase_count": len(purchases),
            "purchases": purchases,
        }
