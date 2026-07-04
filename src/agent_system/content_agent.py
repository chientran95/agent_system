import json
import sqlite3
from pathlib import Path
from typing import Any

import litellm
from deepagents import create_deep_agent

from .storage import FilesystemBackend
from .state import DurableState
from .settings import LANGCHAIN_OLLAMA_MODEL, LITELLM_OLLAMA_MODEL, STORAGE_DIR, CHECKPOINT_DB


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
        self.client = create_deep_agent(model=LANGCHAIN_OLLAMA_MODEL)

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
        result = self.client.invoke({"messages": [{"role": "user", "content": prompt}]})
        return result["messages"][-1].content

    def verify_draft(self, brief: str, draft: str) -> dict[str, Any]:
        # Direct completion call, not the full drafting agent/tool loop -
        # a rubric check doesn't need filesystem access or subagents.
        rubric = [
            "Headline matches the body.",
            "Sources are cited when available.",
            "Word count is between 450 and 650 words.",
        ]
        prompt = (
            f"Please evaluate the draft against the original brief and checklist below.\n\n"
            f"Brief:\n{brief}\n\nDraft:\n{draft}\n\n"
            f"Checklist:\n- {rubric[0]}\n- {rubric[1]}\n- {rubric[2]}\n\n"
            "Answer with only a compact JSON object with keys: verdict, issues, recommendations."
        )
        response = litellm.completion(
            model=LITELLM_OLLAMA_MODEL,
            messages=[{"role": "user", "content": prompt}],
        )
        response_text = response.choices[0].message.content
        try:
            return json.loads(response_text)
        except (json.JSONDecodeError, TypeError):
            return {"verdict": "unknown", "issues": [response_text], "recommendations": []}

    def archive_note(self, filename: str, note: str) -> Path:
        return self.storage.write(filename, note)

    def get_draft(self, filename: str) -> str:
        return self.storage.read(filename)
