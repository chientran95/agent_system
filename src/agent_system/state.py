import sqlite3
from pathlib import Path


class DurableState:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS checkpoints (
                    namespace TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()

    def checkpoint(self, namespace: str, payload: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO checkpoints(namespace, payload)
                VALUES (?, ?)
                ON CONFLICT(namespace) DO UPDATE SET payload=excluded.payload, updated_at=CURRENT_TIMESTAMP
                """,
                (namespace, payload),
            )
            conn.commit()

    def load(self, namespace: str) -> str | None:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT payload FROM checkpoints WHERE namespace = ?",
                (namespace,),
            ).fetchone()
            return row[0] if row else None

    def list_namespaces(self) -> list[str]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT namespace FROM checkpoints").fetchall()
            return [row[0] for row in rows]
