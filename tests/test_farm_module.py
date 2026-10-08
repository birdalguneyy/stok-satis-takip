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
            conn.execute("DELETE FROM products WHERE barcode LIKE 'EGGTEST%'")

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

        # Bugün ve Dün periyot testleri
        t_start, t_end = FarmService.get_date_range_for_period("today")
        self.assertEqual(t_start, today.strftime("%Y-%m-%d"))
        self.assertEqual(t_end, today.strftime("%Y-%m-%d"))

        y_date = today - timedelta(days=1)
        y_start, y_end = FarmService.get_date_range_for_period("yesterday")
        self.assertEqual(y_start, y_date.strftime("%Y-%m-%d"))
        self.assertEqual(y_end, y_date.strftime("%Y-%m-%d"))

        # Dün bir satış ekle
        self.farm_service.create_egg_sale(
            customer_name="Dünkü Müşteri",
            box_count=3.0,
            unit_price=190.0,
            sale_date=y_date.strftime("%Y-%m-%d 14:00:00"),
        )
        yesterday_data = self.farm_service.get_egg_sales_analysis(period="yesterday")
        self.assertEqual(yesterday_data["total_boxes"], 3.0)

        # Özel tarih aralığı testi (custom)
        custom_data = self.farm_service.get_egg_sales_analysis(
            period="custom",
            start_date=y_date.strftime("%Y-%m-%d"),
            end_date=y_date.strftime("%Y-%m-%d"),
        )
        self.assertEqual(custom_data["total_boxes"], 3.0)

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
    def test_parse_egg_product_koli_vs_piece(self):
        from app.services.farm_service import parse_egg_product

        # 30'lu koli -> koli
        u_type, boxes, pieces = parse_egg_product("Gezen Tavuk Yumurta 30'lu Koli", 2)
        self.assertEqual(u_type, "koli")
        self.assertEqual(boxes, 2.0)
        self.assertEqual(pieces, 60.0)

        # 20'li koli -> adet (20 adet)
        u_type, boxes, pieces = parse_egg_product("Doğal Yumurta 20'li Koli", 1)
        self.assertEqual(u_type, "adet")
        self.assertEqual(pieces, 20.0)
        self.assertAlmostEqual(boxes, 20.0 / 30.0, places=2)

        # 15'li paket -> adet (30 adet for 2 qty)
        u_type, boxes, pieces = parse_egg_product("Köy Yumurtası 15'li", 2)
        self.assertEqual(u_type, "adet")
        self.assertEqual(pieces, 30.0)
        self.assertEqual(boxes, 1.0)

        # 10'lu -> adet
        u_type, boxes, pieces = parse_egg_product("Organik Yumurta 10'lu", 3)
        self.assertEqual(u_type, "adet")
        self.assertEqual(pieces, 30.0)
        self.assertEqual(boxes, 1.0)

        # 1 Adet / 3 adet
        u_type, boxes, pieces = parse_egg_product("Yumurta 1 Adet", 3)
        self.assertEqual(u_type, "adet")
        self.assertEqual(pieces, 3.0)
        self.assertAlmostEqual(boxes, 0.1, places=2)

    def test_shop_egg_piece_transfer_and_farm_conversion_analytics(self):
        # 1. Shop'ta 20'li koli ve 1 adet yumurta satışı yapalım
        with self.db.get_connection() as conn:
            conn.execute("INSERT OR IGNORE INTO products (id, category_id, name, barcode, purchase_price, sale_price, stock_quantity) VALUES (951, 1, 'Yumurta 20li Koli', 'YUM20', 80, 120, 50)")
            conn.execute("INSERT OR IGNORE INTO products (id, category_id, name, barcode, purchase_price, sale_price, stock_quantity) VALUES (952, 1, 'Köy Yumurtası 1 Adet', 'YUM01', 3, 6, 100)")

        items = [
            CartItem(product_id=951, product_name='Yumurta 20li Koli', barcode='YUM20', unit_price=120.0, quantity=1, stock_quantity=50),
            CartItem(product_id=952, product_name='Köy Yumurtası 1 Adet', barcode='YUM01', unit_price=6.0, quantity=10, stock_quantity=100),
        ]
        sale = self.sale_repo.create_sale(cart_items=items, channel='magaza', customer_name='Müşteri Hasan')
        self.assertIsNotNone(sale)

        # Farm kayıtlarını kontrol et: 20 adet ve 10 adet olarak geçmiş olmalı
        farm_sales = self.cloud_db.get_farm_egg_sales(customer_name='Müşteri Hasan')
        self.assertEqual(len(farm_sales), 2)
        piece_sales = [s for s in farm_sales if s["unit_type"] == "adet"]
        self.assertEqual(len(piece_sales), 2)
        total_p = sum(s["piece_count"] for s in piece_sales)
        self.assertEqual(total_p, 30.0) # 20 + 10 = 30 adet!

        # 2. Çiftlikten doğrudan 2 koli (30'lu) satış ekleyelim
        self.farm_service.create_egg_sale(
            customer_name="Müşteri Hasan",
            box_count=2.0,
            unit_price=180.0,
            unit_type="koli",
        )

        # 3. Analitik verisini test et: 2 koli + 30 adet = 3.0 Koli olmalı! (Her 30 adet = 1 koli)
        analysis = self.farm_service.get_egg_sales_analysis(customer_name="Müşteri Hasan", period="all")
        self.assertEqual(analysis["pure_koli_boxes"], 2.0)
        self.assertEqual(analysis["total_pieces"], 30.0)
        self.assertEqual(analysis["converted_koli"], 1.0)
        self.assertEqual(analysis["total_boxes"], 3.0)
        self.assertEqual(analysis["total_revenue"], 120.0 + 60.0 + 360.0) # 540 TL
        self.assertEqual(analysis["avg_box_price"], 180.0)

    def test_shop_egg_sales_transferred_to_farm_and_farm_isolated_from_shop(self):
        # 1. Çiftlikten yumurta satışı yapalım
        ok, msg, f_sale_id = self.cloud_db.add_farm_egg_sale(
            customer_name="Özel Çiftlik Müşterisi",
            box_count=3.0,
            unit_price=160.0,
            sale_date="2026-10-08 10:15:00",
            source="Ciftlik",
            note="Organik Çiftlik Satışı",
            unit_type="koli",
        )
        self.assertTrue(ok)
        self.assertIsNotNone(f_sale_id)

        # 2. Çiftlik satışı DÜKKAN satış geçmişinde YER ALMAMALIDIR (Çiftlik dükkana dahil edilmez)
        cloud_history = self.cloud_db.get_sales_history(customer_name="Özel Çiftlik Müşterisi")
        self.assertFalse(any(s.get("customer_name") == "Özel Çiftlik Müşterisi" for s in cloud_history))

        repo_history = self.sale_repo.get_sales_history(customer_name="Özel Çiftlik Müşterisi")
        self.assertFalse(any(s.get("customer_name") == "Özel Çiftlik Müşterisi" for s in repo_history))

        # 3. Dükkandan yumurta satışı yapalım
        shop_items = [
            {
                "product_id": 999,
                "product_name": "Gezen Tavuk Yumurtası 30'lu Koli",
                "barcode": "YUM30",
                "unit_price": 150.0,
                "quantity": 2.0,
                "subtotal": 300.0,
                "unit": "koli",
            }
        ]
        ok_shop, msg_shop = self.cloud_db.add_sale(
            cart_items=shop_items,
            note="Dükkan Kasa Satışı",
            customer_name="Dükkan Yumurta Alıcısı",
            sold_at="2026-10-08 11:00:00",
        )
        self.assertTrue(ok_shop)

        # 4. Dükkan satışı DÜKKAN satış geçmişinde bulunmalıdır
        shop_hist = self.cloud_db.get_sales_history(customer_name="Dükkan Yumurta Alıcısı")
        self.assertEqual(len(shop_hist), 1)
        shop_sale = shop_hist[0]
        shop_sale_id = shop_sale["id"]
        self.assertEqual(shop_sale["total_amount"], 300.0)

        # 5. Dükkandan satılan bu yumurta ÇİFTLİK modülüne dahil edilmiş olmalıdır
        farm_records = self.cloud_db.get_farm_egg_sales(customer_name="Dükkan Yumurta Alıcısı")
        self.assertEqual(len(farm_records), 1)
        egg_mirror = farm_records[0]
        self.assertEqual(egg_mirror["source"], "Dükkan")
        self.assertEqual(egg_mirror["box_count"], 2.0)
        self.assertEqual(egg_mirror["total_amount"], 300.0)
        self.assertIn(f"Fiş #{shop_sale_id}", egg_mirror["note"])

        # 6. Dükkan satış tarihi güncellendiğinde çiftlikteki kaydın da tarihi güncellenmelidir
        ok_upd, _ = self.cloud_db.update_sale_datetime(shop_sale_id, "2026-10-08 14:45:00")
        self.assertTrue(ok_upd)
        upd_farm_records = self.cloud_db.get_farm_egg_sales(customer_name="Dükkan Yumurta Alıcısı")
        self.assertEqual(upd_farm_records[0]["sale_date"], "2026-10-08 14:45:00")

        # 7. Dükkan satışı silindiğinde çiftlikteki kaydı da silinmelidir
        ok_del, _ = self.cloud_db.delete_sale(shop_sale_id)
        self.assertTrue(ok_del)
        del_farm_records = self.cloud_db.get_farm_egg_sales(customer_name="Dükkan Yumurta Alıcısı")
        self.assertEqual(len(del_farm_records), 0)

    def test_sync_shop_egg_sales_to_farm(self):
        # Dükkan veritabanına doğrudan bir yumurta satışı ekleyelim (farm_egg_sales olmadan)
        ok_p, _, prod_dict = self.cloud_db.save_product(
            name="Organik Yumurta 30lu",
            barcode="EGGTEST123",
            category_name="Gıda",
            purchase_price=100.0,
            sale_price=150.0,
            stock_quantity=10.0,
            unit="koli",
            user_id=1,
        )
        self.assertTrue(ok_p)
        pid = prod_dict["id"]

        with self.cloud_db.db.get_connection() as conn:
            cur = conn.execute(
                "INSERT INTO sales (user_id, total_amount, item_count, sold_at, note, channel, customer_name) VALUES (1, 150.0, 1, '2026-10-08 12:00:00', 'Geçmiş Satış', 'magaza', 'Eski Müşteri')"
            )
            sid = cur.lastrowid
            conn.execute(
                "INSERT INTO sale_items (sale_id, product_id, product_name, barcode, unit_price, quantity, subtotal, unit) VALUES (?, ?, 'Organik Yumurta 30lu', 'EGGTEST123', 150.0, 1.0, 150.0, 'koli')",
                (sid, pid)
            )

        # Senkronizasyonu çalıştır
        count = self.cloud_db.sync_shop_egg_sales_to_farm()
        self.assertGreaterEqual(count, 1)

        # Çiftlik kayıtlarında görünmeli
        records = self.cloud_db.get_farm_egg_sales(customer_name="Eski Müşteri")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["source"], "Dükkan")
        self.assertEqual(records[0]["total_amount"], 150.0)


if __name__ == '__main__':
    unittest.main()

