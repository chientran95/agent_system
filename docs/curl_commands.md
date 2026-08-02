# Test Scenario curl / test commands

Commands for exercising [TEST_SCENARIOS.md](TEST_SCENARIOS.md), scenarios 2 through 6. Not every row has a real curl form — where a scenario has no HTTP trigger path (mesh depth limit, registry lookups, malformed-peer-response handling, storage/DB checks), the matching Python or shell snippet is given instead and called out explicitly, same as the rest of this file.

Make sure `code_agent` (port 8001), `research_agent` (port 8002), `weather_agent` (port 8003), and `orchestrator` (port 8000) are running before you start. Scenario 5 additionally needs Jaeger up (`make jaeger`, UI/API at port 16686) and, optionally, Langfuse keys set in `.env` for the Langfuse-only checks.

---

## Scenario 2 — Mesh mechanics (peer-to-peer, bubbling, registry, depth limit)

Only 2.1, 2.2, 2.6, and 2.7 are reachable via curl — 2.3, 2.4, and 2.5 have no real HTTP trigger path and are direct Python tests instead.

---

## 2.1 — code_agent → research_agent delegation

```bash
curl -N -s -X POST http://localhost:8001/ -H "Content-Type: application/json" -d '{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "message/stream",
  "params": {
    "message": {
      "role": "user",
      "parts": [{"kind": "text", "text": "Write a Python script using the httpx library'\''s newest streaming API to fetch a URL - I need to know the current recommended way to do this in the latest httpx release before you write it."}],
      "messageId": "msg-2-1"
    }
  }
}' --max-time 400
```

Watch for a `[call_research_agent]`-style progress event before the final code artifact. This can take 3+ minutes since it triggers research_agent's full pipeline — the `--max-time 400` headroom is deliberate.

---

## 2.2 — research_agent → code_agent delegation

```bash
curl -N -s -X POST http://localhost:8002/ -H "Content-Type: application/json" -d '{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "message/stream",
  "params": {
    "message": {
      "role": "user",
      "parts": [{"kind": "text", "text": "Research how Python'\''s built-in csv module handles quoting, then call the call_code_agent tool to have it write a small Python function that reads a CSV file using the recommended approach you found. Do not write the code yourself - delegate that step."}],
      "messageId": "msg-2-2"
    }
  }
}' --max-time 400
```

Per the known model-unreliability note in [TEST_SCENARIOS.md](TEST_SCENARIOS.md), this may skip delegation on some runs — retry if `call_code_agent` never fires.

---

## 2.3 — Bubbling (code_agent asks a question mid research_agent delegation)

Not reliably reachable via curl (depends on the deep agent's own tool-choice judgment on top of code_agent's own clarify judgment). Direct tool-level test instead — isolates `call_code_agent` + `PeerInputRequired` + `interrupt()` from LLM tool-choice:

```python
import asyncio
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, MessagesState
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from agent_system.research_agent import call_code_agent

async def node(state, config: RunnableConfig):
    call_config = {**config, "configurable": {**config.get("configurable", {}), "call_depth": 0}}
    result = await call_code_agent.ainvoke(
        {"request": "Write a validation function."},  # ambiguous enough that code_agent should ask_user
        config=call_config,
    )
    return {"messages": [result]}

graph = StateGraph(MessagesState).add_node("n", node)
graph.set_entry_point("n")
app = graph.compile(checkpointer=InMemorySaver())
config = {"configurable": {"thread_id": "bubble-test"}}

async def main():
    result = await app.ainvoke({"messages": []}, config=config)
    print(result)  # should show an __interrupt__ with code_agent's question
    resumed = await app.ainvoke(Command(resume="Validate that a string is a well-formed email address."), config=config)
    print(resumed)

asyncio.run(main())
```

> **Note:** the `node` function must accept LangGraph's injected `config` and merge into it (as above) rather than replacing it with a fresh dict — `interrupt()` reads pregel-internal keys (e.g. `__pregel_scratchpad`) off that ambient config, and a fresh `config={"configurable": {...}}` silently drops them, causing `KeyError: '__pregel_scratchpad'`.

---

## 2.4 — Depth limit enforcement

Not reachable via curl — no real cycle exists to trigger it naturally:

```python
import asyncio
from agent_system.a2a_peer_client import call_peer_agent, MeshDepthExceeded

async def main():
    try:
        await call_peer_agent("http://localhost:8001", "test", call_depth=10)
        print("FAIL: no exception raised")
    except MeshDepthExceeded:
        print("PASS: MeshDepthExceeded raised, no HTTP call made")

asyncio.run(main())
```

---

## 2.5 — Registry resolves before Direct Config

Pure Python assertion, no server needed:

```python
from agent_system.agent_registry import lookup_agent_url
from agent_system.settings import CODE_AGENT_URL
assert lookup_agent_url("coding_agent") == CODE_AGENT_URL
print("PASS")
```

---

## 2.6 — Fallback when agent isn't registered

```bash
# 1. back up and remove the research_agent entry
cp src/agent_system/agent_registry.json /tmp/agent_registry.json.bak
python3 -c "
import json
p = 'src/agent_system/agent_registry.json'
d = json.load(open(p))
del d['research_agent']
json.dump(d, open(p, 'w'), indent=2)
"
```

```bash
# 2. same call as 2.1 - should still succeed via settings.py's RESEARCH_AGENT_URL fallback
curl -N -s -X POST http://localhost:8001/ -H "Content-Type: application/json" -d '{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "message/stream",
  "params": {
    "message": {
      "role": "user",
      "parts": [{"kind": "text", "text": "Write a Python script using the httpx library'\''s newest streaming API to fetch a URL - I need to know the current recommended way to do this in the latest httpx release before you write it."}],
      "messageId": "msg-2-1"
    }
  }
}' --max-time 400
```

```bash
# 3. restore
cp /tmp/agent_registry.json.bak src/agent_system/agent_registry.json
```

Check the code_agent server's console log for the line:

```
[agent discovery] 'research_agent' -> not in curated registry, falling back to direct config
```

That's the actual proof, not just the curl succeeding.

---

## 2.7 — Registry override actually changes routing

```bash
# 1. back up and point coding_agent at a wrong port
cp src/agent_system/agent_registry.json /tmp/agent_registry.json.bak
python3 -c "
import json
p = 'src/agent_system/agent_registry.json'
d = json.load(open(p))
d['coding_agent']['url'] = 'http://localhost:9999'
json.dump(d, open(p, 'w'), indent=2)
"
```

```bash
# 2. same call as 2.2 - the call_code_agent delegation should now fail/misroute
#    (connection refused on 9999), not reach the real code_agent on 8001
curl -N -s -X POST http://localhost:8002/ -H "Content-Type: application/json" -d '{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "message/stream",
  "params": {
    "message": {
      "role": "user",
      "parts": [{"kind": "text", "text": "Research how Python'\''s csv module handles quoting, then call call_code_agent to write a function using that approach."}],
      "messageId": "msg-2-7"
    }
  }
}' --max-time 120
```

```bash
# 3. restore
cp /tmp/agent_registry.json.bak src/agent_system/agent_registry.json
```

Confirm via the console log line:

```
[agent discovery] 'coding_agent' -> curated registry -> http://localhost:9999
```

That's what proves the registry (not the fallback) drove the routing.

---

## Scenario 3 — Orchestrator routing + resume-correlation

The orchestrator is a different shape entirely from the other three servers: it's ADK's own session-based REST API (`get_fast_api_app`), not raw A2A JSON-RPC. Every request needs a session created first, and `appName` is always `orchestrator` (the sub-folder name under `orchestrator_agents/`).

### 3.1 — Route to each of the 3 sub-agents

```bash
# health / discovery
curl -s http://localhost:8000/health
curl -s http://localhost:8000/list-apps
```

```bash
# coding request -> coding_agent
curl -s -X POST "http://localhost:8000/apps/orchestrator/users/tester/sessions/s1" \
  -H "Content-Type: application/json" -d '{}'

curl -N -s -X POST "http://localhost:8000/run_sse" -H "Content-Type: application/json" -d '{
  "appName": "orchestrator",
  "userId": "tester",
  "sessionId": "s1",
  "newMessage": {
    "role": "user",
    "parts": [{"text": "Write a Python function that reverses a string."}]
  }
}' --max-time 90
```

Look for `"functionCall": {"name": "transfer_to_agent", "args": {"agent_name": "coding_agent"}}` confirming the routing decision, followed by an event authored by `coding_agent` with the generated code.

```bash
# research request -> research_agent (fresh session - don't reuse s1)
curl -s -X POST "http://localhost:8000/apps/orchestrator/users/tester/sessions/s2" \
  -H "Content-Type: application/json" -d '{}'

curl -N -s -X POST "http://localhost:8000/run_sse" -H "Content-Type: application/json" -d '{
  "appName": "orchestrator",
  "userId": "tester",
  "sessionId": "s2",
  "newMessage": {
    "role": "user",
    "parts": [{"text": "Research the current state of protected bike lane funding in US cities and write a blog post about it."}]
  }
}' --max-time 240
```

Look for `transfer_to_agent` routing to `research_agent`, followed by the finished blog post (internally went through `content_writer`).

```bash
# weather request -> weather_agent (fresh session)
curl -s -X POST "http://localhost:8000/apps/orchestrator/users/tester/sessions/s3" \
  -H "Content-Type: application/json" -d '{}'

curl -N -s -X POST "http://localhost:8000/run_sse" -H "Content-Type: application/json" -d '{
  "appName": "orchestrator",
  "userId": "tester",
  "sessionId": "s3",
  "newMessage": {
    "role": "user",
    "parts": [{"text": "What is the current weather in Tokyo, Japan?"}]
  }
}' --max-time 90
```

Look for `transfer_to_agent` routing to `weather_agent`.

> **Tip:** `/run_sse` emits one `data: {...}` line per event, which reads much better piped through `jq`:
> ```bash
> curl -N -s -X POST "http://localhost:8000/run_sse" ... | sed -n 's/^data: //p' | \
>   jq -r '"--- \(.author) ---\n\(.content.parts[]?.text // .content.parts[]?.functionCall // empty)"'
> ```
> For `/run` (blocking) instead of `/run_sse`, the response is one JSON array: `curl -s ... | jq -r '.[] | "--- \(.author) ---\n\(.content.parts[]?.text // .content.parts[]?.functionCall // empty)"'`.

### 3.2 — Weather sub-agent pauses via orchestrator

Reuse the weather route above but with a query missing a location, so `weather_agent` has to ask:

```bash
curl -s -X POST "http://localhost:8000/apps/orchestrator/users/tester/sessions/s4" \
  -H "Content-Type: application/json" -d '{}'

curl -N -s -X POST "http://localhost:8000/run_sse" -H "Content-Type: application/json" -d '{
  "appName": "orchestrator",
  "userId": "tester",
  "sessionId": "s4",
  "newMessage": {
    "role": "user",
    "parts": [{"text": "What is the weather like today?"}]
  }
}' --max-time 90
```

Confirm ADK represents the pause as a paused long-running tool rather than a normal completion: look for `"a2a:response".status.state: "input-required"` and a `longRunningToolIds` entry on the event, with a synthetic `functionCall` named `mock_function_call_for_required_user_input` carrying the clarifying question.

### 3.3 — Plain-text follow-up resumes the same paused task

Send a plain-text follow-up in the **same session** (`s4`) — `ResumePendingInputMiddleware` should detect the pending mock call from 3.2 and auto-wrap this into the `functionResponse` shape ADK expects, rather than the orchestrator's LLM just re-deciding to `transfer_to_agent` again and orphaning the original paused task:

```bash
curl -N -s -X POST "http://localhost:8000/run_sse" -H "Content-Type: application/json" -d '{
  "appName": "orchestrator",
  "userId": "tester",
  "sessionId": "s4",
  "newMessage": {
    "role": "user",
    "parts": [{"text": "Berlin, Germany"}]
  }
}' --max-time 90
```

Check the *middleware's* correlation worked by confirming the outgoing request it rewrites contains a `functionResponse` (not a second `transfer_to_agent` call) — easiest via the orchestrator's own console log or by inspecting `GET /apps/orchestrator/users/tester/sessions/s4` for the event history:

```bash
curl -s "http://localhost:8000/apps/orchestrator/users/tester/sessions/s4" | jq '.events[-3:]'
```

**Known limitation:** the middleware fixes correlation at the ADK session layer, but the deeper resume through `RemoteA2aAgent` into the actual paused `weather_agent` task is still broken — ADK's flow layer re-executes `transfer_to_agent` from scratch on resume instead of reconstructing the paused branch, so the original A2A task's events are never available to continue it. This is a real, currently-unresolved gap in `google-adk` itself, not something wrong with the middleware. Full write-up: [BUG_ORCHESTRATOR_RESUME.md](BUG_ORCHESTRATOR_RESUME.md). Direct agent-to-agent resume (bypassing the orchestrator, per scenario 1.5) works fine — this is specific to the orchestrator hop.

### 3.4 — Orchestrator model sanity check

Cheap smoke test — one trivial routing request, assert the response has a non-empty `functionCall` part rather than empty `parts` (see the LiteLLM ↔ Ollama flakiness known issue):

```bash
curl -s -X POST "http://localhost:8000/apps/orchestrator/users/tester/sessions/s5" \
  -H "Content-Type: application/json" -d '{}'

curl -s -X POST "http://localhost:8000/run" -H "Content-Type: application/json" -d '{
  "appName": "orchestrator",
  "userId": "tester",
  "sessionId": "s5",
  "newMessage": {
    "role": "user",
    "parts": [{"text": "Write a Python function that adds two numbers."}]
  }
}' --max-time 60 | jq '[.[] | .content.parts[]? | select(.functionCall)] | length > 0'
```

`true` means at least one event had a real `functionCall`; `false` means the model returned empty `parts` on every event (retry).

---

## Scenario 4 — Infrastructure / failure-mode tests

Only 4.1 and 4.2 are curl-reachable. 4.3 (malformed peer response) and 4.4 (`mechanical_verify`) need to stub/drive internals directly, so they're Python snippets instead.

### 4.1 — Resume after "Queue is closed" scenario

This exercises `ResilientQueueManager` ([BUG_A2A_QUEUE_LIFECYCLE.md](BUG_A2A_QUEUE_LIFECYCLE.md)) — a2a-sdk unconditionally closes a paused task's `EventQueue`, and without the workaround, resuming it raises "Queue is closed" instead of continuing. No artificial wait is actually needed to trigger it (the bug isn't timing-dependent, it happens on every single pause), but pausing, waiting a beat, then resuming matches how a real user would interact with it:

```bash
# 1. pause: underspecified weather query
curl -s -X POST http://localhost:8003/ -H "Content-Type: application/json" -d '{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "message/send",
  "params": {
    "message": {
      "role": "user",
      "parts": [{"kind": "text", "text": "What is the weather like today?"}],
      "messageId": "msg-4-1a"
    }
  }
}' --max-time 90
```

Note the `taskId` from the response (`status.state` should be `"input-required"`), wait a few seconds, then resume against the **same** `taskId`:

```bash
# 2. resume - replace <TASK_ID> with the id from step 1
curl -s -X POST http://localhost:8003/ -H "Content-Type: application/json" -d '{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "message/send",
  "params": {
    "message": {
      "role": "user",
      "parts": [{"kind": "text", "text": "Berlin, Germany"}],
      "messageId": "msg-4-1b",
      "taskId": "<TASK_ID>"
    }
  }
}' --max-time 90
```

Confirm `status.state` moves to `"completed"` with a real (non-empty) answer, and check weather_agent's console for the absence of `Queue is closed. Event will not be dequeued.`.

### 4.2 — Two concurrent sessions to the same agent

Fire two independent weather queries at the same agent in parallel (no shared `taskId`, so each should get its own task) and confirm no cross-talk between the responses:

```bash
curl -s -X POST http://localhost:8003/ -H "Content-Type: application/json" -d '{
  "jsonrpc": "2.0", "id": "1", "method": "message/send",
  "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": "Current weather in Tokyo, Japan?"}], "messageId": "msg-4-2a"}}
}' --max-time 90 > /tmp/session_a.json &

curl -s -X POST http://localhost:8003/ -H "Content-Type: application/json" -d '{
  "jsonrpc": "2.0", "id": "2", "method": "message/send",
  "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": "Current weather in Sydney, Australia?"}], "messageId": "msg-4-2b"}}
}' --max-time 90 > /tmp/session_b.json &

wait

jq -r '.result.status.message.parts[0].text' /tmp/session_a.json
jq -r '.result.status.message.parts[0].text' /tmp/session_b.json
jq -r '.result.id' /tmp/session_a.json /tmp/session_b.json  # confirm two distinct taskIds
```

Confirm the two `taskId`s differ and each answer actually matches its own city (Tokyo in `session_a`, Sydney in `session_b`) — not swapped or blended.

### 4.3 — Empty/malformed peer response

Not curl-reachable — this tests `call_peer_agent`'s own error handling, which needs a stubbed HTTP response rather than a real server:

```python
import asyncio
from unittest.mock import AsyncMock, patch
import httpx
from agent_system.a2a_peer_client import call_peer_agent

async def main():
    fake_response = httpx.Response(
        200, json={"jsonrpc": "2.0", "id": "1", "error": {"code": -32000, "message": "boom"}},
        request=httpx.Request("POST", "http://localhost:8001"),
    )
    with patch("httpx.AsyncClient.post", AsyncMock(return_value=fake_response)):
        try:
            await call_peer_agent("http://localhost:8001", "test", call_depth=0)
            print("FAIL: no exception raised")
        except RuntimeError as e:
            print("PASS: RuntimeError raised cleanly:", e)

asyncio.run(main())
```

### 4.4 — code_agent's mechanical_verify

Not curl-reachable — drive `CodeAgent.mechanical_verify` directly against a scratch workspace with deliberately bad code, confirm it fails closed:

```python
import tempfile
from pathlib import Path
from agent_system.code_agent import CodeAgent

with tempfile.TemporaryDirectory() as tmp:
    workspace = Path(tmp)
    (workspace / "bad.py").write_text("def broken(:\n    pass\n")  # syntax error
    agent = CodeAgent()
    result = agent.mechanical_verify(workspace)
    print("PASS: verify correctly failed on bad code" if result is False else "FAIL: verify should have returned False")
```

---

## Scenario 5 — Observability spot-checks

These are UI/API lookups rather than requests that drive agent behavior. Run any scenario 1-4 request first (through the orchestrator, for 5.1) so there's a trace to find.

### 5.1 — One trace spans orchestrator → sub-agent

Jaeger's query API backs its UI at the same port:

```bash
curl -s "http://localhost:16686/api/services" | jq '.data'
curl -s "http://localhost:16686/api/traces?service=orchestrator&limit=5" | \
  jq '.data[] | {traceID, spans: [.spans[].operationName]}'
```

Confirm a trace's `spans` list includes both `orchestrator`-authored spans and a child span from e.g. `research_agent_a2a` — that's the cross-service span confirming one trace covers the whole routed request. Or just open `http://localhost:16686` in a browser and search by service `orchestrator`.

### 5.2 — Claude Code's own spans appear

```bash
curl -s "http://localhost:16686/api/traces?service=claude_code_cli&limit=5" | jq '.data[] | .traceID'
```

One trace per `code_agent` request that went through Claude Code's own native OTEL export.

### 5.3 — Langfuse prompt/completion detail

No curl — Langfuse is a dashboard check, not an API this project wires curl against. Open your project dashboard (`LANGFUSE_BASE_URL` in `.env`, default `https://cloud.langfuse.com`) and look for traces from recent `research_agent`, `content_writer`, and `weather_agent` runs, with full prompt/completion text visible per generation.

### 5.4 — Orchestrator's dual OTLP export

Same Langfuse dashboard as 5.3, filtered to the `orchestrator` service — confirm routing spans show up with structure and token counts. Known limitation: raw prompt/completion text does *not* show for these spans (ADK's span attributes don't match the naming Langfuse's OTLP mapper expects) — structure/tokens only is expected, not a bug to chase.

---

## Scenario 6 — Storage

Filesystem checks, not curl. Run after a request that goes through `content_writer` (e.g. scenario 1.3 or 3.1's research route).

### 6.1 — Draft persistence

```bash
ls -la storage/draft-*.md
tail -20 "$(ls -t storage/draft-*.md | head -1)"
```

Confirm a new `draft-*.md` file exists with a timestamp matching your test run, and its content matches the blog post the agent returned.

### 6.2 — Checkpoint DB

```bash
ls -la state/checkpoint.sqlite
sqlite3 state/checkpoint.sqlite "SELECT namespace, updated_at FROM checkpoints ORDER BY updated_at DESC LIMIT 5;"
```

Confirm the file's mtime updated after your run, and the most recent row's `namespace` matches the session/thread you just used. (This is `content_agent`'s own lightweight `DurableState` table — see [state.py](../src/agent_system/state.py) — not LangGraph's `SqliteSaver`.)
