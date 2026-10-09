import sqlite3
from contextlib import contextmanager
from typing import Any, Generator, Optional

from app.config import DATA_DIR, DB_PATH


class Database:
    _instance: Optional["Database"] = None

    def __new__(cls) -> "Database":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, db_path: Optional[Any] = None) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if db_path is not None:
            from pathlib import Path
            self.db_path = Path(db_path)
            self._initialized = True
            return
        if self._initialized:
            return
        from app.config import DB_PATH as CONFIG_DB_PATH
        self.db_path = CONFIG_DB_PATH
        self._initialized = True

    @classmethod
    def reset_instance(cls, db_path: Optional[Any] = None) -> "Database":
        """Testlerde izole veritabanı kullanabilmek için singleton'ı sıfırlar."""
        cls._instance = None
        inst = cls()
        if db_path is not None:
            from pathlib import Path
            inst.db_path = Path(db_path)
        return inst

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("PRAGMA cache_size = -64000")
            conn.execute("PRAGMA temp_store = MEMORY")
            conn.execute("PRAGMA mmap_size = 268435456")
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
