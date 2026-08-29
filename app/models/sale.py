from dataclasses import dataclass
from typing import Optional


@dataclass
class Sale:
    id: Optional[int]
    total_amount: float
    item_count: int
    sold_at: Optional[str] = None
    note: Optional[str] = None

    @classmethod
    def from_row(cls, row) -> "Sale":
        return cls(
            id=row["id"],
            total_amount=row["total_amount"],
            item_count=row["item_count"],
            sold_at=row["sold_at"],
            note=row["note"],
        )


@dataclass
class SaleItem:
    id: Optional[int]
    sale_id: int
    product_id: int
    product_name: str
    barcode: str
    unit_price: float
    quantity: int
    subtotal: float

    @classmethod
    def from_row(cls, row) -> "SaleItem":
        return cls(
            id=row["id"],
            sale_id=row["sale_id"],
            product_id=row["product_id"],
            product_name=row["product_name"],
            barcode=row["barcode"],
            unit_price=row["unit_price"],
            quantity=row["quantity"],
            subtotal=row["subtotal"],
        )
