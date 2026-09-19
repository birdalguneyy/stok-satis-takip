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
        self._custom_total: Optional[float] = None

    @property
    def cart_items(self) -> List[CartItem]:
        return list(self._cart.values())

    @property
    def calculated_total(self) -> float:
        return round(sum(item.subtotal for item in self._cart.values()), 2)

    @property
    def total_amount(self) -> float:
        if self._custom_total is not None:
            return round(self._custom_total, 2)
        return self.calculated_total

    @property
    def is_custom_total(self) -> bool:
        return self._custom_total is not None

    def set_custom_total(self, amount: Optional[float]) -> None:
        if amount is None or amount < 0:
            self._custom_total = None
        else:
            self._custom_total = round(float(amount), 2)

    def clear_cart(self) -> None:
        self._cart.clear()
        self._custom_total = None

    def search_candidates(self, term: str) -> List[Product]:
        return self.product_service.search_candidates(term)

    def add_by_scan(self, term: str) -> Tuple[bool, str]:
        candidates = self.search_candidates(term)
        if not candidates:
            return False, "Ürün bulunamadı"
        if len(candidates) == 1:
            return self.add_product(candidates[0])
        # Exact barcode match
        exact_barcode = next((p for p in candidates if p.barcode == term.strip()), None)
        if exact_barcode:
            return self.add_product(exact_barcode)
        return False, f"Birden fazla ürün bulundu ({len(candidates)} ürün)"

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
            original_unit_price=product.sale_price,
        )
        return True, f"{product.name} sepete eklendi"

    def update_item_price(self, product_id: int, new_unit_price: float) -> Tuple[bool, str]:
        """Yalnızca bu satışa özel geçici birim fiyat belirler (ana ürün kataloğu değişmez)."""
        if product_id not in self._cart:
            return False, "Ürün sepette değil"
        if new_unit_price < 0:
            return False, "Birim fiyat negatif olamaz"

        item = self._cart[product_id]
        item.unit_price = round(new_unit_price, 2)
        return True, f"'{item.product_name}' için geçici birim fiyat {item.unit_price:.2f} ₺ yapıldı"

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

    def complete_sale(
        self,
        custom_total: Optional[float] = None,
        note: Optional[str] = None,
        channel: str = "magaza",
        customer_name: Optional[str] = None,
    ) -> Tuple[bool, str]:
        if not self._cart:
            return False, "Sepet boş"

        final_total = custom_total if custom_total is not None else self._custom_total

        try:
            self.sale_repo.create_sale(
                self.cart_items,
                note=note,
                channel=channel,
                total_amount_override=final_total,
                customer_name=customer_name,
            )
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

    def get_sales_history(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        customer_name: Optional[str] = None,
    ) -> List[dict]:
        return self.sale_repo.get_sales_history(start_date, end_date, customer_name)

    def delete_sale(self, sale_id: int, restore_stock: bool = True) -> Tuple[bool, str]:
        return self.sale_repo.delete_sale(sale_id, restore_stock=restore_stock)

    def delete_sales_bulk(self, sale_ids: List[int], restore_stock: bool = True) -> Tuple[bool, str, int]:
        return self.sale_repo.delete_sales_bulk(sale_ids, restore_stock=restore_stock)
