import unittest
import os
import sqlite3
from datetime import datetime, timedelta, date

from app.database.migrations import run_migrations
from app.database.connection import Database
from app.database.cloud_db import CloudDatabase
from app.services.farm_service import FarmService
from app.repositories.sale_repository import SaleRepository
from app.models.cart_item import CartItem


class TestFarmModule(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        run_migrations()
        cls.db = Database()
        cls.cloud_db = CloudDatabase()
        cls.farm_service = FarmService(cls.cloud_db)
        cls.sale_repo = SaleRepository(cls.db)

    def setUp(self):
        # Temiz test ortamı
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM farm_egg_sales")
            conn.execute("DELETE FROM farm_feed_purchases")
            conn.execute("DELETE FROM farm_customers")
            conn.execute("DELETE FROM sale_items")
            conn.execute("DELETE FROM sales")

    def test_egg_sale_creation_and_customer_remembering(self):
        # 1. Yeni yumurta satışı kaydet
        ok, msg, sale_id = self.farm_service.create_egg_sale(
            customer_name="Mehmet Bey",
            box_count=3.0,
            unit_price=180.0,
            source="Ciftlik",
            note="Organik yumurta",
        )
        self.assertTrue(ok)
        self.assertIsNotNone(sale_id)

        # 2. Müşteri adının otomatik hatırlandığını doğrula
        customers = self.farm_service.get_customers()
        self.assertIn("Mehmet Bey", customers)

        # 3. İkinci bir satış yap
        self.farm_service.create_egg_sale(
            customer_name="Ayşe Hanım",
            box_count=2.0,
            unit_price=175.0,
        )
        customers = self.farm_service.get_customers()
        self.assertIn("Ayşe Hanım", customers)
        self.assertIn("Mehmet Bey", customers)

    def test_feed_purchase_tonnage_calculation(self):
        # 1 çuval = 50 kg -> 40 çuval = 2.000 kg = 2.00 Ton
        ok, msg, pid = self.farm_service.create_feed_purchase(
            bag_count=40.0,
            unit_price=450.0,
            total_amount=18000.0,
            supplier="Özlem Yem",
        )
        self.assertTrue(ok)

        feed_data = self.farm_service.get_feed_analysis()
        self.assertEqual(feed_data["total_bags"], 40.0)
        self.assertEqual(feed_data["total_kg"], 2000.0)
        self.assertEqual(feed_data["total_tons"], 2.0)
        self.assertEqual(feed_data["total_cost"], 18000.0)

    def test_date_filters_and_monday_to_monday(self):
        today = datetime.now().date()
        monday = today - timedelta(days=today.weekday())
        next_monday = monday + timedelta(days=7)

        # Haftalık periyot hesaplaması
        w_start, w_end = FarmService.get_date_range_for_period("weekly")
        self.assertEqual(w_start, monday.strftime("%Y-%m-%d"))
        self.assertEqual(w_end, next_monday.strftime("%Y-%m-%d"))

        # Bu haftanın Pazartesi gününe bir satış ekle
        self.farm_service.create_egg_sale(
            customer_name="Haftalık Müşteri",
            box_count=5.0,
            unit_price=180.0,
            sale_date=monday.strftime("%Y-%m-%d 10:00:00"),
        )

        # 30 gün öncesine (farklı haftaya) bir satış ekle
        past_date = today - timedelta(days=30)
        self.farm_service.create_egg_sale(
            customer_name="Geçmiş Müşteri",
            box_count=10.0,
            unit_price=170.0,
            sale_date=past_date.strftime("%Y-%m-%d 10:00:00"),
        )

        # Haftalık analizi sorgula
        weekly_data = self.farm_service.get_egg_sales_analysis(period="weekly")
        self.assertEqual(weekly_data["total_boxes"], 5.0)

        # Tüm zamanlar analizini sorgula
        all_data = self.farm_service.get_egg_sales_analysis(period="all")
        self.assertEqual(all_data["total_boxes"], 15.0)

    def test_customer_specific_filtering_and_all_customers_summary(self):
        self.farm_service.create_egg_sale(customer_name="Ali", box_count=4.0, unit_price=180.0)
        self.farm_service.create_egg_sale(customer_name="Ali", box_count=6.0, unit_price=180.0)
        self.farm_service.create_egg_sale(customer_name="Veli", box_count=5.0, unit_price=170.0)

        # Sadece Ali sorgusu
        ali_data = self.farm_service.get_egg_sales_analysis(customer_name="Ali", period="all")
        self.assertEqual(ali_data["total_boxes"], 10.0)
        self.assertEqual(ali_data["total_revenue"], 1800.0)
        self.assertEqual(ali_data["customer_count"], 1)

        # Tüm müşteriler sorgusu
        all_data = self.farm_service.get_egg_sales_analysis(period="all")
        self.assertEqual(all_data["total_boxes"], 15.0)
        self.assertEqual(len(all_data["customer_summary"]), 2)
        # Ali ilk sırada olmalı çünkü 10 koli aldı
        self.assertEqual(all_data["customer_summary"][0]["customer_name"], "Ali")
        self.assertEqual(all_data["customer_summary"][0]["total_boxes"], 10.0)

    def test_shop_pos_egg_transfer_with_discount(self):
        # Sepette: 2 Ekmek (12 TL), 1 Koli Yumurta (Normalde 180 TL ama indirimle 150 TL yapılmış)
        # Önce test ürünleri var mı kontrol et veya ekle
        with self.db.get_connection() as conn:
            conn.execute("INSERT OR IGNORE INTO products (id, category_id, name, barcode, purchase_price, sale_price, stock_quantity) VALUES (901, 1, 'Köy Ekmeği', 'EKM01', 8, 12, 50)")
            conn.execute("INSERT OR IGNORE INTO products (id, category_id, name, barcode, purchase_price, sale_price, stock_quantity) VALUES (902, 1, 'Gezen Tavuk Yumurta 30lu', 'YUM01', 120, 180, 50)")

        items = [
            CartItem(product_id=901, product_name='Köy Ekmeği', barcode='EKM01', unit_price=12.0, quantity=2, stock_quantity=50),
            CartItem(product_id=902, product_name='Gezen Tavuk Yumurta 30lu', barcode='YUM01', unit_price=150.0, quantity=1, stock_quantity=50), # 180 yerine 150 TL indirimli!
        ]

        sale = self.sale_repo.create_sale(
            cart_items=items,
            channel='magaza',
            customer_name='Dükkan Müdavimi Salih',
            total_amount_override=174.0, # 2*12 + 150 = 174 TL
        )
        self.assertIsNotNone(sale)

        # Çiftlik kayıtlarına bak: Sadece yumurta geçmiş olmalı, ekmek geçmemeli!
        farm_sales = self.cloud_db.get_farm_egg_sales()
        self.assertEqual(len(farm_sales), 1)
        egg_entry = farm_sales[0]
        self.assertEqual(egg_entry["customer_name"], "Dükkan Müdavimi Salih")
        self.assertEqual(egg_entry["box_count"], 1.0)
        self.assertEqual(egg_entry["unit_price"], 150.0)
        self.assertEqual(egg_entry["total_amount"], 150.0)
        self.assertEqual(egg_entry["source"], "Dükkan")
        self.assertIn("Dükkan Satışı", egg_entry["note"])

    def test_flask_api_farm_endpoints(self):
        from app.web.web_server import app
        client = app.test_client()

        # 1. Şifre kontrolü (2805)
        res_fail = client.post("/api/farm/auth-check", json={"password": "1234"})
        self.assertEqual(res_fail.status_code, 401)
        res_ok = client.post("/api/farm/auth-check", json={"password": "2805"})
        self.assertEqual(res_ok.status_code, 200)

        # 2. Yumurta satışı POST & GET
        res_egg = client.post("/api/farm/egg-sales", json={
            "customer_name": "Web Müşterisi",
            "box_count": 3,
            "unit_price": 185.0,
            "note": "Web Siparişi",
        })
        self.assertEqual(res_egg.status_code, 200)

        res_egg_get = client.get("/api/farm/egg-sales")
        self.assertEqual(res_egg_get.status_code, 200)
        self.assertTrue(len(res_egg_get.json["sales"]) > 0)

        # 3. Yem alımı POST & GET
        res_feed = client.post("/api/farm/feed-purchases", json={
            "bag_count": 50,
            "unit_price": 400.0,
            "total_amount": 20000.0,
            "supplier": "Birlik Yem",
        })
        self.assertEqual(res_feed.status_code, 200)

        res_feed_get = client.get("/api/farm/feed-purchases")
        self.assertEqual(res_feed_get.status_code, 200)
        self.assertTrue(len(res_feed_get.json["purchases"]) > 0)

        # 4. Analitik Endpoint
        res_analytics = client.get("/api/farm/analytics?period=all")
        self.assertEqual(res_analytics.status_code, 200)
        self.assertEqual(res_analytics.json["data"]["total_boxes"], 3.0)


if __name__ == '__main__':
    unittest.main()

