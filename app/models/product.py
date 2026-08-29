from dataclasses import dataclass
from typing import Optional


@dataclass
class Product:
    id: Optional[int]
    category_id: int
    name: str
    barcode: str
    purchase_price: float
    sale_price: float
    stock_quantity: int
    critical_stock_level: int = 5
    image_path: Optional[str] = None
    is_active: int = 1
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    category_name: Optional[str] = None

    @classmethod
    def from_row(cls, row) -> "Product":
        keys = row.keys()
        return cls(
            id=row["id"],
            category_id=row["category_id"],
            name=row["name"],
            barcode=row["barcode"],
            purchase_price=row["purchase_price"],
            sale_price=row["sale_price"],
            stock_quantity=row["stock_quantity"],
            critical_stock_level=row["critical_stock_level"],
            image_path=row["image_path"] if "image_path" in keys else None,
            is_active=row["is_active"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            category_name=row["category_name"] if "category_name" in keys else None,
        )

    @property
    def is_critical(self) -> bool:
        return self.stock_quantity <= self.critical_stock_level
