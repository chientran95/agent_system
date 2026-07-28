import json

import httpx
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

_RESUMABLE_PATHS = {"/run", "/run_sse"}
_MOCK_FUNCTION_CALL_NAMES = {
    "mock_function_call_for_required_user_input",
    "mock_function_call_for_required_user_auth",
}


class ResumePendingInputMiddleware(BaseHTTPMiddleware):
    """When a sub-agent pauses (input-required), ADK represents it as a
    synthetic functionCall named mock_function_call_for_required_user_input,
    and only resumes the same paused task if the next message answers it with
    a matching functionResponse (response={"result": <answer>}) - a plain
    text follow-up gets routed as a brand-new request instead, orphaning the
    paused task. See google/adk/a2a/converters/to_adk_event.py.

    This middleware lets callers (including the ADK Dev UI) keep sending
    plain text: it checks whether the session's last stored event is an
    unanswered mock function call, and if so, auto-wraps the incoming plain
    text into the functionResponse shape ADK expects before the real /run or
    /run_sse handler ever sees it.
    """

    async def dispatch(self, request: Request, call_next):
        if request.method != "POST" or request.url.path not in _RESUMABLE_PATHS:
            return await call_next(request)

        body = await request.body()
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            request._body = body
            return await call_next(request)

        rewritten = await self._maybe_rewrite(request, payload)
        if rewritten is not None:
            body = json.dumps(rewritten).encode("utf-8")

        request._body = body
        return await call_next(request)

    async def _maybe_rewrite(self, request: Request, payload: dict) -> dict | None:
        new_message = payload.get("newMessage") or {}
        parts = new_message.get("parts") or []
        if not parts or any("functionResponse" in p for p in parts):
            return None  # nothing to translate, or caller already did it themselves
        text_parts = [p["text"] for p in parts if "text" in p]
        if not text_parts:
            return None

        pending_call = await self._find_pending_mock_call(request, payload)
        if not pending_call:
            return None

        new_message["parts"] = [
            {
                "functionResponse": {
                    "id": pending_call["id"],
                    "name": pending_call["name"],
                    "response": {"result": " ".join(text_parts)},
                }
            }
        ]
        payload["newMessage"] = new_message
        return payload

    async def _find_pending_mock_call(self, request: Request, payload: dict) -> dict | None:
        app_name = payload.get("appName")
        user_id = payload.get("userId")
        session_id = payload.get("sessionId")
        if not (app_name and user_id and session_id):
            return None

        base_url = str(request.base_url).rstrip("/")
        url = f"{base_url}/apps/{app_name}/users/{user_id}/sessions/{session_id}"
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, timeout=10.0)
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None

        events = response.json().get("events") or []
        if not events:
            return None
        last_parts = (events[-1].get("content") or {}).get("parts", [])
        for part in last_parts:
            function_call = part.get("functionCall")
            if function_call and function_call.get("name") in _MOCK_FUNCTION_CALL_NAMES:
                return function_call
        return None
