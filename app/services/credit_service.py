from decimal import Decimal, ROUND_HALF_UP
from typing import Any


def to_kurus(amount: Any) -> int:
    """TL tutarını kayıpsız tam sayı kuruşa (int) çevirir."""
    d = Decimal(str(amount if amount is not None else 0)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return int(d * 100)


def from_kurus(kurus: int) -> float:
    """Tam sayı kuruşu 2 ondalıklı TL float değerine çevirir."""
    return float((Decimal(int(kurus or 0)) / Decimal(100)).quantize(Decimal("0.01")))


def kurus_to_decimal(kurus: int) -> Decimal:
    return (Decimal(int(kurus or 0)) / Decimal(100)).quantize(Decimal("0.01"))
