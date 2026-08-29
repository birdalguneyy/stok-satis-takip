from dataclasses import dataclass, field
from typing import List, Tuple

from app.config import DEFAULT_CRITICAL_STOCK
from app.models.product import Product
from app.repositories.product_repository import ProductRepository
from app.repositories.sale_repository import SaleRepository


@dataclass
class DashboardStats:
    total_products: int
    critical_stock_count: int
    today_sales_count: int
    top_products: List[Tuple[str, int]]
    critical_products: List[Product] = field(default_factory=list)


class DashboardService:
    def __init__(
        self,
        product_repo: ProductRepository | None = None,
        sale_repo: SaleRepository | None = None,
    ) -> None:
        self.product_repo = product_repo or ProductRepository()
        self.sale_repo = sale_repo or SaleRepository()

    def get_stats(self) -> DashboardStats:
        return DashboardStats(
            total_products=self.product_repo.count_all(),
            critical_stock_count=self.product_repo.count_critical(DEFAULT_CRITICAL_STOCK),
            today_sales_count=self.sale_repo.count_today_sales(),
            top_products=self.sale_repo.get_top_selling_products(limit=5),
            critical_products=self.product_repo.get_critical_products(limit=8),
        )
