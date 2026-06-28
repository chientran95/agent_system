import json
from typing import Any
import httpx
from .a2a_tracing import init_tracing


class A2AClient:
    def __init__(self, timeout: int = 30) -> None:
        init_tracing()
        self.client = httpx.Client(timeout=timeout)

    def post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.client.post(url, json=payload)
        response.raise_for_status()
        return response.json()

    def health_check(self, url: str) -> bool:
        try:
            response = self.client.get(url)
            return response.status_code == 200
        except httpx.HTTPError:
            return False
