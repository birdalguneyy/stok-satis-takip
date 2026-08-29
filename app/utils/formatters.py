from app.config import CURRENCY_SYMBOL


def format_currency(amount: float) -> str:
    formatted = f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{formatted} {CURRENCY_SYMBOL}"
