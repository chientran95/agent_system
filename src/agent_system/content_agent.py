import json
import sqlite3
import uuid
from pathlib import Path
from typing import Annotated, Any, TypedDict

import litellm
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_ollama import ChatOllama
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages

from .langfuse_tracing import configure_litellm, get_langchain_callbacks
from .storage import FilesystemBackend
from .state import DurableState
from .settings import OLLAMA_MODEL, LITELLM_OLLAMA_MODEL, STORAGE_DIR, CHECKPOINT_DB

DRAFT_SYSTEM_PROMPT = (
    "You are a blog-post writer. Using the research brief and notes provided "
    "in this conversation, write a clear, publish-ready blog post. Use a "
    "strong headline, cite sources if present, and keep the draft between "
    "200 and 500 words. Respond with only the post itself, no preamble."
)


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


class ContentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    draft_path: str
    verification: dict[str, Any]


class ContentAgent:
    def __init__(self, storage_dir: Path = STORAGE_DIR, checkpoint_db: Path = CHECKPOINT_DB):
        self.storage = FilesystemBackend(storage_dir)
        self.state = DurableState(checkpoint_db)
        self.memory = SQLiteConversationMemory(checkpoint_db.with_name("content_memory.sqlite"))
        self.model = ChatOllama(model=OLLAMA_MODEL)
        self.graph = self._build_graph()
        configure_litellm()
        print(f"ContentAgent initialized with LLM=ollama:{OLLAMA_MODEL}")

    def _build_graph(self):
        graph = StateGraph(ContentState)
        graph.add_node("draft", self._draft_node)
        graph.add_node("persist_and_verify", self._persist_and_verify_node)
        graph.set_entry_point("draft")
        graph.add_edge("draft", "persist_and_verify")
        graph.add_edge("persist_and_verify", END)
        return graph.compile()

    def _draft_node(self, state: ContentState) -> dict[str, Any]:
        print("ContentAgent: drafting post from research brief")
        response = self.model.invoke([SystemMessage(content=DRAFT_SYSTEM_PROMPT), *state["messages"]])
        return {"messages": [response]}

    def _persist_and_verify_node(self, state: ContentState) -> dict[str, Any]:
        draft = state["messages"][-1].content
        brief = _first_user_text(state["messages"])
        filename = f"draft-{uuid.uuid4().hex[:8]}.md"
        path = self.persist_draft(brief, filename, draft)
        verification = self.verify_draft(brief, draft)
        footer = f"\n\n---\nSaved to {path} | Verification: {json.dumps(verification)}"
        return {
            "messages": [AIMessage(content=draft + footer)],
            "draft_path": str(path),
            "verification": verification,
        }

    def as_subagent(self) -> dict[str, Any]:
        """Returns a deepagents CompiledSubAgent spec so a research_agent can
        delegate blog-post writing to this graph via the task() tool."""
        return {
            "name": "content_writer",
            "description": (
                "Writes a publish-ready blog post from a research brief or notes. "
                "Call this once research is complete, passing the findings as the task."
            ),
            "runnable": self.graph,
        }

    def write_draft(self, brief: str) -> Path:
        """Synchronous convenience entrypoint for direct/manual use, running
        the same graph a research_agent would invoke as a subagent."""
        result = self.graph.invoke(
            {"messages": [HumanMessage(content=brief)], "draft_path": "", "verification": {}},
            config={"callbacks": get_langchain_callbacks()},
        )
        return Path(result["draft_path"])

    def persist_draft(self, brief: str, draft_filename: str, draft: str) -> Path:
        path = self.storage.write(draft_filename, draft)
        self.state.checkpoint("latest_brief", brief)
        self.state.checkpoint("latest_draft", draft)
        self.memory.add_message("user", brief)
        self.memory.add_message("assistant", draft)
        return path

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


def _first_user_text(messages: list[BaseMessage]) -> str:
    for message in messages:
        if getattr(message, "type", None) == "human":
            return message.content
    return messages[0].content if messages else ""
