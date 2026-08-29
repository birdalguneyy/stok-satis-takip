from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class ValidationResult:
    is_valid: bool
    message: Optional[str] = None
    invalid_fields: List[str] = field(default_factory=list)


def validate_product_fields(
    name: str,
    category_name: str,
    purchase_price: float,
    sale_price: float,
    stock_quantity: int,
    barcode: str,
) -> ValidationResult:
    invalid_fields = []
    messages = []

    if not name.strip():
        invalid_fields.append("name")
        messages.append("Ürün adı zorunludur")
    if not category_name.strip():
        invalid_fields.append("category")
        messages.append("Kategori zorunludur")
    if not barcode.strip():
        invalid_fields.append("barcode")
        messages.append("Barkod numarası zorunludur")
    if purchase_price < 0:
        invalid_fields.append("purchase_price")
        messages.append("Alış fiyatı negatif olamaz")
    if sale_price < 0:
        invalid_fields.append("sale_price")
        messages.append("Satış fiyatı negatif olamaz")
    if stock_quantity < 0:
        invalid_fields.append("stock_quantity")
        messages.append("Stok miktarı negatif olamaz")

    if invalid_fields:
        return ValidationResult(
            is_valid=False,
            message=messages[0],
            invalid_fields=invalid_fields,
        )

    return ValidationResult(is_valid=True)

