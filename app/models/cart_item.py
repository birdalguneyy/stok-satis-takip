from dataclasses import dataclass


@dataclass
class CartItem:
    product_id: int
    product_name: str
    barcode: str
    unit_price: float
    quantity: int = 1
    stock_quantity: int = 0

    @property
    def subtotal(self) -> float:
        return self.unit_price * self.quantity

    def can_increase(self) -> bool:
        return self.quantity < self.stock_quantity
