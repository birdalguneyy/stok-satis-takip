from dataclasses import dataclass
from typing import Optional


@dataclass
class CartItem:
    product_id: int
    product_name: str
    barcode: str
    unit_price: float
    quantity: int = 1
    stock_quantity: int = 0
    original_unit_price: Optional[float] = None

    def __post_init__(self):
        if self.original_unit_price is None:
            self.original_unit_price = self.unit_price

    @property
    def subtotal(self) -> float:
        return round(self.unit_price * self.quantity, 2)

    @property
    def is_price_overridden(self) -> bool:
        return self.original_unit_price is not None and abs(self.unit_price - self.original_unit_price) > 0.001

    def can_increase(self) -> bool:
        return self.quantity < self.stock_quantity
