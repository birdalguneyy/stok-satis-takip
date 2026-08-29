from typing import List, Optional

from app.database.connection import Database
from app.models.category import Category


class CategoryRepository:
    def __init__(self, db: Optional[Database] = None) -> None:
        self.db = db or Database()

    def get_all(self) -> List[Category]:
        with self.db.get_connection() as conn:
            rows = conn.execute(
                "SELECT id, name, created_at FROM categories ORDER BY name"
            ).fetchall()
        return [Category.from_row(row) for row in rows]

    def get_by_name(self, name: str) -> Optional[Category]:
        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT id, name, created_at FROM categories WHERE name = ?",
                (name.strip(),),
            ).fetchone()
        return Category.from_row(row) if row else None

    def get_or_create(self, name: str) -> Category:
        existing = self.get_by_name(name)
        if existing:
            return existing
        with self.db.get_connection() as conn:
            cursor = conn.execute(
                "INSERT INTO categories (name) VALUES (?)",
                (name.strip(),),
            )
            category_id = cursor.lastrowid
            row = conn.execute(
                "SELECT id, name, created_at FROM categories WHERE id = ?",
                (category_id,),
            ).fetchone()
        return Category.from_row(row)
