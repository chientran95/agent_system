from pathlib import Path


class FilesystemBackend:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def write(self, path: str, content: str) -> Path:
        target = self.base_dir / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def read(self, path: str) -> str:
        target = self.base_dir / path
        return target.read_text(encoding="utf-8")

    def exists(self, path: str) -> bool:
        return (self.base_dir / path).exists()

    def list(self, prefix: str = "") -> list[str]:
        root = self.base_dir / prefix
        if not root.exists():
            return []
        return [str(p.relative_to(self.base_dir)) for p in root.rglob("*") if p.is_file()]
