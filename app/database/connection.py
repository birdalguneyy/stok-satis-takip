import sqlite3
from contextlib import contextmanager
from typing import Generator, Optional

from app.config import DATA_DIR, DB_PATH


class Database:
    _instance: Optional["Database"] = None

    def __new__(cls) -> "Database":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.db_path = DB_PATH
        self._initialized = True

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA busy_timeout = 30000")
        except Exception:
            pass
        conn.execute("PRAGMA foreign_keys = ON")
        return conn


    @contextmanager
    def get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
