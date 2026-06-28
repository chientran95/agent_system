import os
import subprocess
from pathlib import Path
from typing import Any


class InMemorySaver:
    def __init__(self) -> None:
        self.memory: list[dict[str, Any]] = []

    def save(self, item: dict[str, Any]) -> None:
        self.memory.append(item)

    def get_all(self) -> list[dict[str, Any]]:
        return list(self.memory)


class CodeAgent:
    def __init__(self, api_key: str | None = None, model: str = "claude-3.5" ) -> None:
        self.api_key = api_key or os.getenv("CLAUDE_API_KEY")
        self.model = model
        self.memory = InMemorySaver()
        self.client = self._build_client()

    def _build_client(self) -> Any:
        import importlib

        module = importlib.import_module("claude_agent_sdk")
        ClaudeAgentClient = getattr(module, "ClaudeAgentClient")
        return ClaudeAgentClient(api_key=self.api_key, model=self.model)

    def save_interaction(self, input_text: str, output_text: str) -> None:
        self.memory.save({"input": input_text, "output": output_text})

    def generate_code(self, prompt: str) -> str:
        response = self.client.complete(prompt)
        self.save_interaction(prompt, response.text)
        return response.text

    def mechanical_verify(self, workspace_dir: Path, test_command: list[str] | None = None) -> bool:
        test_command = test_command or ["pytest"]
        print("Running deterministic verification: tac --noEmit")
        tac_process = subprocess.run(
            ["tac", "--noEmit"], cwd=workspace_dir, capture_output=True, text=True
        )
        if tac_process.returncode != 0:
            print("tac failed:\n", tac_process.stdout, tac_process.stderr)
            return False

        print("Running actual tests: %s" % " ".join(test_command))
        test_process = subprocess.run(test_command, cwd=workspace_dir, capture_output=True, text=True)
        if test_process.returncode != 0:
            print("Tests failed:\n", test_process.stdout, test_process.stderr)
            return False

        return True
