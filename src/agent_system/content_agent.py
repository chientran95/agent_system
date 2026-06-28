import os
import sqlite3
from pathlib import Path
from typing import Any

from .storage import FilesystemBackend
from .state import DurableState
from .settings import STORAGE_DIR, CHECKPOINT_DB, CLAUDE_MODEL


class SQLiteConversationMemory:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()

    def add_message(self, role: str, content: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO conversation(role, content) VALUES (?, ?)",
                (role, content),
            )
            conn.commit()

    def history(self) -> list[tuple[str, str]]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT role, content FROM conversation ORDER BY id"
            ).fetchall()
            return [(role, content) for role, content in rows]


class ContentAgent:
    def __init__(self, storage_dir: Path = STORAGE_DIR, checkpoint_db: Path = CHECKPOINT_DB):
        self.storage = FilesystemBackend(storage_dir)
        self.state = DurableState(checkpoint_db)
        self.memory = SQLiteConversationMemory(checkpoint_db.with_name("content_memory.sqlite"))
        self.model_name = os.getenv("CLAUDE_MODEL", CLAUDE_MODEL)
        self.client = self._build_agent_client()

    def _build_agent_client(self) -> Any:
        import importlib

        deepagents = importlib.import_module("deepagents")
        if hasattr(deepagents, "DeepAgent"):
            DeepAgent = getattr(deepagents, "DeepAgent")
            return DeepAgent(model=self.model_name)

        raise ImportError("deepagents is required for ContentAgent. Install deepagents before running.")

    def write_draft(self, brief: str, draft_filename: str) -> Path:
        draft = self._run_content_pipeline(brief)
        path = self.storage.write(draft_filename, draft)
        self.state.checkpoint("latest_brief", brief)
        self.state.checkpoint("latest_draft", draft)
        self.memory.add_message("user", brief)
        self.memory.add_message("assistant", draft)
        return path

    def _run_content_pipeline(self, brief: str) -> str:
        prompt = f"Write a news-style article from this brief:\n\n{brief}\n\nUse a clear headline, cite sources if present, and keep the draft between 450 and 650 words."
        return self.client.run(prompt)

    def verify_draft(self, brief: str, draft: str) -> dict[str, Any]:
        rubric = [
            "Headline matches the body.",
            "Sources are cited when available.",
            "Word count is between 450 and 650 words.",
        ]
        prompt = (
            f"Please evaluate the draft against the original brief and checklist below.\n\n"
            f"Brief:\n{brief}\n\nDraft:\n{draft}\n\n"
            f"Checklist:\n- {rubric[0]}\n- {rubric[1]}\n- {rubric[2]}\n\n"
            "Answer in a compact JSON object with keys: verdict, issues, recommendations."
        )
        return self.client.run(prompt)

    def archive_note(self, filename: str, note: str) -> Path:
        return self.storage.write(filename, note)

    def get_draft(self, filename: str) -> str:
        return self.storage.read(filename)
