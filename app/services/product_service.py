from typing import List, Optional, Tuple

from app.config import DEFAULT_CRITICAL_STOCK
from app.models.product import Product
from app.repositories.category_repository import CategoryRepository
from app.repositories.product_repository import ProductRepository
from app.utils.validators import validate_product_fields


class ProductService:
    def __init__(
        self,
        product_repo: ProductRepository | None = None,
        category_repo: CategoryRepository | None = None,
    ) -> None:
        self.product_repo = product_repo or ProductRepository()
        self.category_repo = category_repo or CategoryRepository()

    def list_products(
        self,
        search: str = "",
        order_by: str = "name",
        ascending: bool = True,
    ) -> List[Product]:
        return self.product_repo.get_all(search=search, order_by=order_by, ascending=ascending)

    def get_categories(self):
        return self.category_repo.get_all()

    def find_for_scan(self, term: str) -> Optional[Product]:
        term = term.strip()
        if not term:
            return None
        product = self.product_repo.get_by_barcode(term)
        if product:
            return product
        results = self.product_repo.search(term)
        return results[0] if len(results) == 1 else None

    def save_product(
        self,
        name: str,
        category_name: str,
        purchase_price: float,
        sale_price: float,
        stock_quantity: int,
        barcode: str,
        critical_stock_level: int = DEFAULT_CRITICAL_STOCK,
        product_id: Optional[int] = None,
    ) -> Tuple[bool, str, Optional[Product]]:
        res = validate_product_fields(
            name, category_name, purchase_price, sale_price, stock_quantity, barcode
        )
        if not res.is_valid:
            return False, res.message or "Geçersiz veri", None


        category = self.category_repo.get_or_create(category_name)

        if product_id:
            existing = self.product_repo.get_by_id(product_id)
            if not existing:
                return False, "Ürün bulunamadı", None
            duplicate = self.product_repo.get_by_barcode(barcode)
            if duplicate and duplicate.id != product_id:
                return False, "Bu barkod numarası zaten kayıtlı", None
            product = Product(
                id=product_id,
                category_id=category.id,
                name=name,
                barcode=barcode,
                purchase_price=purchase_price,
                sale_price=sale_price,
                stock_quantity=stock_quantity,
                critical_stock_level=critical_stock_level,
            )
            saved = self.product_repo.update(product)
            return True, "Ürün güncellendi", saved

        if self.product_repo.get_by_barcode(barcode):
            return False, "Bu barkod numarası zaten kayıtlı", None

        product = Product(
            id=None,
            category_id=category.id,
            name=name,
            barcode=barcode,
            purchase_price=purchase_price,
            sale_price=sale_price,
            stock_quantity=stock_quantity,
            critical_stock_level=critical_stock_level,
        )
        saved = self.product_repo.create(product)
        return True, "Ürün eklendi", saved

    def delete_product(self, product_id: int) -> Tuple[bool, str]:
        existing = self.product_repo.get_by_id(product_id)
        if not existing:
            return False, "Ürün bulunamadı"
        self.product_repo.delete(product_id)
        return True, "Ürün silindi"

    def seed_demo_data(self, force: bool = True) -> Tuple[bool, str]:
        from app.database.connection import Database
        from app.database.seed_data import seed_demo_products

        db = Database()
        with db.get_connection() as conn:
            added = seed_demo_products(conn, force=force)

        if added > 0:
            return True, f"{added} adet demo ürün eklendi"
        return False, "Örnek ürünler zaten eklenmiş veya yeni ürün eklenemedi"

