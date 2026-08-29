from typing import Dict, List, Optional, Tuple

from app.models.cart_item import CartItem
from app.models.product import Product
from app.repositories.sale_repository import SaleRepository
from app.services.product_service import ProductService


class SaleService:
    def __init__(
        self,
        product_service: ProductService | None = None,
        sale_repo: SaleRepository | None = None,
    ) -> None:
        self.product_service = product_service or ProductService()
        self.sale_repo = sale_repo or SaleRepository()
        self._cart: Dict[int, CartItem] = {}

    @property
    def cart_items(self) -> List[CartItem]:
        return list(self._cart.values())

    @property
    def total_amount(self) -> float:
        return sum(item.subtotal for item in self._cart.values())

    def clear_cart(self) -> None:
        self._cart.clear()

    def add_by_scan(self, term: str) -> Tuple[bool, str]:
        product = self.product_service.find_for_scan(term)
        if not product:
            return False, "Ürün bulunamadı"
        return self.add_product(product)

    def add_product(self, product: Product) -> Tuple[bool, str]:
        if product.stock_quantity <= 0:
            return False, "Stok yetersiz"

        if product.id in self._cart:
            item = self._cart[product.id]
            if not item.can_increase():
                return False, "Stok yetersiz"
            item.quantity += 1
            return True, f"{product.name} miktarı artırıldı"

        self._cart[product.id] = CartItem(
            product_id=product.id,
            product_name=product.name,
            barcode=product.barcode,
            unit_price=product.sale_price,
            quantity=1,
            stock_quantity=product.stock_quantity,
        )
        return True, f"{product.name} sepete eklendi"

    def update_quantity(self, product_id: int, quantity: int) -> Tuple[bool, str]:
        if product_id not in self._cart:
            return False, "Ürün sepette değil"
        if quantity <= 0:
            del self._cart[product_id]
            return True, "Ürün sepetten çıkarıldı"

        item = self._cart[product_id]
        if quantity > item.stock_quantity:
            return False, "Stok yetersiz"
        item.quantity = quantity
        return True, "Miktar güncellendi"

    def complete_sale(self) -> Tuple[bool, str]:
        if not self._cart:
            return False, "Sepet boş"

        try:
            self.sale_repo.create_sale(self.cart_items)
        except ValueError as exc:
            return False, str(exc)

        self.clear_cart()
        return True, "Satış tamamlandı"

    def remove_from_cart(self, product_id: int) -> Tuple[bool, str]:
        if product_id not in self._cart:
            return False, "Ürün sepette değil"
        name = self._cart[product_id].product_name
        del self._cart[product_id]
        return True, f"{name} sepetten çıkarıldı"
