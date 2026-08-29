from dataclasses import dataclass
from typing import Optional


@dataclass
class Category:
    id: Optional[int]
    name: str
    created_at: Optional[str] = None

    @classmethod
    def from_row(cls, row) -> "Category":
        return cls(id=row["id"], name=row["name"], created_at=row["created_at"])
