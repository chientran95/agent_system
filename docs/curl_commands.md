# Test Scenario curl / test commands

Commands for exercising [TEST_SCENARIOS.md](TEST_SCENARIOS.md), scenarios 1 through 6. Not every row has a real curl form — where a scenario has no HTTP trigger path (mesh depth limit, registry lookups, malformed-peer-response handling, storage/DB checks), the matching Python or shell snippet is given instead and called out explicitly, same as the rest of this file.

Make sure `code_agent` (port 8001), `research_agent` (port 8002), `weather_agent` (port 8003), and `orchestrator` (port 8000) are running before you start. Scenario 5 additionally needs Jaeger up (`make jaeger`, UI/API at port 16686) and, optionally, Langfuse keys set in `.env` for the Langfuse-only checks.

The orchestrator has two interchangeable implementations, switched via `ORCHESTRATOR_BACKEND` in `.env` (`adk` or `langgraph`) — only one runs at a time, on the same port, so check which one you've got running before using scenario 3's commands (they're split into an "ADK backend" and "LangGraph backend" section, each with a different request shape).

Every request below uses streaming (`message/stream` for the three A2A agents and the LangGraph orchestrator, `/run_sse` for the ADK orchestrator) rather than a blocking call — all agent servers declare `AgentCapabilities(streaming=True)` (`code_agent`, `research_agent`, `weather_agent`, and the LangGraph orchestrator alternative), and ADK's own `/run_sse` is its streaming route. `-N` disables curl's output buffering so events print as they arrive.

### Reading streaming responses

A streaming response is a sequence of `data: {...}` SSE lines (plus blank lines and `: ping ...` comments to keep the connection alive) rather than one JSON object. To pull a specific field out programmatically — e.g. when a curl command redirects to a file for a later step — strip the `data: ` prefix and slurp all lines into `jq`:

```bash
sed -n 's/^data: //p' response.json | jq -s '...'
```

Two patterns come up repeatedly for the three A2A agents (`code_agent`/`research_agent`/`weather_agent`):

```bash
# task ID - from the first event, which is always a raw Task snapshot (kind:"task")
sed -n 's/^data: //p' response.json | jq -s '[.[] | select(.result.kind == "task")] | first | .result.id'

# final status text - from the last status-update event that reached a terminal state
sed -n 's/^data: //p' response.json | jq -s '
  [.[] | select(.result.kind == "status-update" and (.result.status.state == "completed" or .result.status.state == "input-required"))]
  | last | {state: .result.status.state, text: .result.status.message.parts[0].text}'
```

The second one only applies to agents that attach a `message` to their final status update (`weather_agent` always does; `code_agent`'s normal completion doesn't - it puts the answer in a `generated_code` artifact-update event instead, called out inline below where that matters).

---

## Scenario 1 — Single-agent core behavior (direct A2A, bypass orchestrator)

All curl-reachable — this is the most direct layer, one JSON-RPC call straight to the agent being tested.

### 1.1 — Clean code generation

```bash
curl -N -s -X POST http://localhost:8001/ -H "Content-Type: application/json" -d '{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "message/stream",
  "params": {
    "message": {
      "role": "user",
      "parts": [{"kind": "text", "text": "Write a Python function that reverses a string."}],
      "messageId": "msg-1-1"
    }
  }
}' --max-time 90
```

You'll see `status-update` events stream in as Claude generates the response (token-level chunks), then a final `artifact-update` event named `generated_code`, then a `completed` status-update with no message attached (the answer lives in the artifact, not `status.message`). **If this errors** with a billing/credit message, that's your `CLAUDE_API_KEY`'s Anthropic account, not the wiring — check [console.anthropic.com/settings/billing](https://console.anthropic.com/settings/billing).

### 1.2 — Ambiguous code request → clarify → resume

```bash
# 1. ambiguous request - code_agent should ask rather than guess
curl -N -s -X POST http://localhost:8001/ -H "Content-Type: application/json" -d '{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "message/stream",
  "params": {
    "message": {
      "role": "user",
      "parts": [{"kind": "text", "text": "Write a validation function."}],
      "messageId": "msg-1-2a"
    }
  }
}' --max-time 90 > /tmp/scenario_1_2.json
```

The final `status-update` event's `status.state` should be `"input-required"` with a real clarifying question (e.g. asking what should be validated) in `status.message`. Pull the task ID out to resume:

```bash
sed -n 's/^data: //p' /tmp/scenario_1_2.json | jq -s '[.[] | select(.result.kind == "task")] | first | .result.id'
```

```bash
# 2. resume - replace <TASK_ID> with the id from step 1
curl -N -s -X POST http://localhost:8001/ -H "Content-Type: application/json" -d '{
  "jsonrpc": "2.0",
  "id": "2",
  "method": "message/stream",
  "params": {
    "message": {
      "role": "user",
      "taskId": "<TASK_ID>",
      "parts": [{"kind": "text", "text": "Validate that a string is a well-formed email address."}],
      "messageId": "msg-1-2b"
    }
  }
}' --max-time 90
```

Expect the same shape as 1.1's completion (final answer in a `generated_code` artifact-update, not `status.message`) — same session resumed. Internally `code_agent_server.py` maps the task ID to a Claude Agent SDK session ID to continue the same session — you only ever need to pass the `taskId` back, same shape as every other agent's resume.

### 1.3 — Research happy path

```bash
curl -N -s -X POST http://localhost:8002/ -H "Content-Type: application/json" -d '{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "message/stream",
  "params": {
    "message": {
      "role": "user",
      "parts": [{"kind": "text", "text": "Research the current state of protected bike lane funding in US cities and write a blog post about it."}],
      "messageId": "msg-1-3"
    }
  }
}' --max-time 240
```

`--max-time 240` — real DuckDuckGo search plus two model calls (research_agent's own reasoning, then the `content_writer` subagent drafting + verifying). Expect `completed` with a non-empty `blog_post` artifact; check `storage/draft-*.md` for the saved file.

**Reliability note:** with a smaller local model this doesn't always follow the intended research → delegate → return sequence — on an off run it may answer directly instead of calling `content_writer`. Retry if the result looks like a bullet-point summary instead of a formatted post.

### 1.4 — Well-specified weather query

```bash
curl -N -s -X POST http://localhost:8003/ -H "Content-Type: application/json" -d '{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "message/stream",
  "params": {
    "message": {
      "role": "user",
      "parts": [{"kind": "text", "text": "What is the current weather in Tokyo, Japan?"}],
      "messageId": "msg-1-4"
    }
  }
}' --max-time 90
```

Each `agent` node turn and `tools` node turn (the geocoding/forecast tool results) streams as its own `working` status-update event before the final `completed` one with the answer in `status.message`. Watch which tool it picks (`weather_forecast` vs a national-model tool like `jma_forecast`), and whether it calls `geocoding` first to resolve the place name into coordinates — both are real, observed points of variability with local models, not guaranteed to go the same way every run.

### 1.5 — Underspecified weather query → clarify → resume

```bash
# 1. underspecified - weather_agent should ask for a location
curl -N -s -X POST http://localhost:8003/ -H "Content-Type: application/json" -d '{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "message/stream",
  "params": {
    "message": {
      "role": "user",
      "parts": [{"kind": "text", "text": "What is the weather like today?"}],
      "messageId": "msg-1-5a"
    }
  }
}' --max-time 60 > /tmp/scenario_1_5.json
```

Check the final status text and pull the task ID using the two patterns from the top of this doc:

```bash
sed -n 's/^data: //p' /tmp/scenario_1_5.json | jq -s '
  [.[] | select(.result.kind == "status-update" and (.result.status.state == "completed" or .result.status.state == "input-required"))]
  | last | {state: .result.status.state, text: .result.status.message.parts[0].text}'

sed -n 's/^data: //p' /tmp/scenario_1_5.json | jq -s '[.[] | select(.result.kind == "task")] | first | .result.id'
```

`state` should be `"input-required"` with a real clarifying question as `text`. Then resume against that task ID:

```bash
# 2. resume - replace <TASK_ID> with the id from step 1
curl -N -s -X POST http://localhost:8003/ -H "Content-Type: application/json" -d '{
  "jsonrpc": "2.0",
  "id": "2",
  "method": "message/stream",
  "params": {
    "message": {
      "role": "user",
      "taskId": "<TASK_ID>",
      "parts": [{"kind": "text", "text": "Berlin, Germany"}],
      "messageId": "msg-1-5b"
    }
  }
}' --max-time 90
```

Confirm `status.state` moves to `"completed"` with the same `taskId`/thread resumed (the task ID doubles as the LangGraph thread ID).

**Reliability note:** the pause/resume mechanism itself is solid across different local models. What's inconsistent is whether the model *acts correctly* on the resumed answer (e.g. it may re-ask for information you just gave it) — a model-capability limitation, not a mechanism bug.

### 1.6 — Streaming variant of 1.1 / 1.4

Since every command in this doc now uses `message/stream`, 1.1 and 1.4 above already **are** this scenario — there's no separate blocking form left to compare against. Re-run them and confirm multiple `working` progress events (not just one) before the final `completed` event — for `weather_agent` (1.4) specifically, confirm you see both a `[agent]`-labeled chunk (the model's tool-call decision) and a `[tools]`-labeled chunk (the geocoding/forecast tool result) as separate events, not just the final answer.

---

## Scenario 2 — Mesh mechanics (peer-to-peer, bubbling, registry, depth limit)

Only 2.1, 2.2, 2.6, and 2.7 are reachable via curl — 2.3, 2.4, and 2.5 have no real HTTP trigger path and are direct Python tests instead.

---

### 2.1 — code_agent → research_agent delegation

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

### 2.2 — research_agent → code_agent delegation

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

### 2.3 — Bubbling (code_agent asks a question mid research_agent delegation)

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

### 2.4 — Depth limit enforcement

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

### 2.5 — Registry resolves before Direct Config

Pure Python assertion, no server needed:

```python
from agent_system.agent_registry import lookup_agent_url
from agent_system.settings import CODE_AGENT_URL
assert lookup_agent_url("coding_agent") == CODE_AGENT_URL
print("PASS")
```

---

### 2.6 — Fallback when agent isn't registered

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

### 2.7 — Registry override actually changes routing

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

There are two orchestrator implementations, switched via `ORCHESTRATOR_BACKEND` in `.env` - only one runs at a time, on the same port. **3.1-3.4 and the cleanup section below are the "adk" backend** (the default). **See "LangGraph backend" at the end of this scenario for the "langgraph" backend**, which speaks plain A2A JSON-RPC (same shape as the other three agents) instead of ADK's session-REST API, and plans/dispatches waves of steps instead of routing to exactly one agent.

### ADK backend

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

curl -N -s -X POST "http://localhost:8000/run_sse" -H "Content-Type: application/json" -d '{
  "appName": "orchestrator",
  "userId": "tester",
  "sessionId": "s5",
  "newMessage": {
    "role": "user",
    "parts": [{"text": "Write a Python function that adds two numbers."}]
  }
}' --max-time 60 | sed -n 's/^data: //p' | jq -s '[.[] | .content.parts[]? | select(.functionCall)] | length > 0'
```

`true` means at least one event had a real `functionCall`; `false` means the model returned empty `parts` on every event (retry).

### Cleanup — delete a session

Same URL shape as creating a session, just `DELETE`, no body needed:

```bash
curl -s -X DELETE "http://localhost:8000/apps/orchestrator/users/tester/sessions/s1"
```

Goes straight to `session_service.delete_session()`. Useful for freeing a `sessionId` mid-run (e.g. between retries of 3.2/3.3) without restarting the orchestrator. Confirm it's gone:

```bash
curl -s -o /dev/null -w "%{http_code}\n" "http://localhost:8000/apps/orchestrator/users/tester/sessions/s1"
```

`404` means deleted; `200` means it's still there. Note: with the default `InMemorySessionService`, restarting the orchestrator process clears all sessions anyway — this is mainly for cleanup within a single run.

### LangGraph backend

Plain A2A JSON-RPC — same `message/stream` shape, same task-ID/resume mechanics, and the same "Reading streaming responses" patterns from the top of this doc apply directly. No session creation step needed.

#### Multi-step plan (parallel + sequential waves)

```bash
curl -N -s -X POST http://localhost:8000/ -H "Content-Type: application/json" -d '{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "message/stream",
  "params": {
    "message": {
      "role": "user",
      "parts": [{"kind": "text", "text": "Research current best practices for rate limiting in Python web APIs, write example code implementing it, and check the current weather forecast for Berlin so we can plan a maintenance window."}],
      "messageId": "msg-3-lg-1"
    }
  }
}' --max-time 400
```

Watch for a `[plan]`-labeled progress event describing the generated waves (e.g. `wave 0: [research_agent, weather_agent]; wave 1: [coding_agent]`), then one `[dispatch_step]` event per completed step (`research_agent`/`weather_agent` interleaved since they're in the same wave, `coding_agent` only after both finish), then a final `[summarize]`/`completed` event combining all three results.

#### Single-agent request (plan degrades to one wave, one step)

```bash
curl -N -s -X POST http://localhost:8000/ -H "Content-Type: application/json" -d '{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "message/stream",
  "params": {
    "message": {
      "role": "user",
      "parts": [{"kind": "text", "text": "What is the current weather in Tokyo, Japan?"}],
      "messageId": "msg-3-lg-2"
    }
  }
}' --max-time 90
```

Same coverage as 3.1's per-agent routing checks, just through the plan/dispatch/summarize path instead of a dedicated route step — `[plan]` should show a single wave with a single `weather_agent` step, and since there's only one step result, `summarize` returns it unchanged rather than paraphrasing (see `summarize_node`'s single-step shortcut).

#### A step pauses → resume (this actually completes, unlike the ADK backend)

```bash
# 1. underspecified - weather_agent should ask for a location
curl -N -s -X POST http://localhost:8000/ -H "Content-Type: application/json" -d '{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "message/stream",
  "params": {
    "message": {
      "role": "user",
      "parts": [{"kind": "text", "text": "What is the weather like today?"}],
      "messageId": "msg-3-lg-3a"
    }
  }
}' --max-time 60 > /tmp/scenario_3_lg.json
```

Extract the task ID, same pattern as scenario 1.5:

```bash
sed -n 's/^data: //p' /tmp/scenario_3_lg.json | jq -s '[.[] | select(.result.kind == "task")] | first | .result.id'
```

```bash
# 2. resume - replace <TASK_ID> with the id from step 1
curl -N -s -X POST http://localhost:8000/ -H "Content-Type: application/json" -d '{
  "jsonrpc": "2.0",
  "id": "2",
  "method": "message/stream",
  "params": {
    "message": {
      "role": "user",
      "taskId": "<TASK_ID>",
      "parts": [{"kind": "text", "text": "Berlin, Germany"}],
      "messageId": "msg-3-lg-3b"
    }
  }
}' --max-time 90
```

Confirm `status.state` moves all the way to `"completed"` — **this is the exact case that's structurally broken on the ADK backend** (3.3's known limitation, [BUG_ORCHESTRATOR_RESUME.md](BUG_ORCHESTRATOR_RESUME.md)). The LangGraph backend sidesteps that bug entirely by using the same `interrupt()`/checkpointer mechanism already proven across the rest of the mesh, rather than depending on ADK's `RemoteA2aAgent` to reconstruct a paused branch.

> **Note:** if a wave happens to have two steps that both pause simultaneously, resuming answers one at a time — a fresh `input-required` after your resume means a second question is still pending, not that the resume failed silently.

#### Sanity check

```bash
curl -s http://localhost:8000/.well-known/agent-card.json
```

No `/health`/`/list-apps`/session-creation equivalent needed — if this returns a card, the orchestrator is up. (Same role as 3.4's smoke test, but a plan/structured-output failure surfaces as a real error in the response rather than empty `parts`, so there's less need for a dedicated non-empty-response check here.)

---

## Scenario 4 — Infrastructure / failure-mode tests

Only 4.1 and 4.2 are curl-reachable. 4.3 (malformed peer response) and 4.4 (`mechanical_verify`) need to stub/drive internals directly, so they're Python snippets instead.

### 4.1 — Resume after "Queue is closed" scenario

This exercises `ResilientQueueManager` ([BUG_A2A_QUEUE_LIFECYCLE.md](BUG_A2A_QUEUE_LIFECYCLE.md)) — a2a-sdk unconditionally closes a paused task's `EventQueue`, and without the workaround, resuming it raises "Queue is closed" instead of continuing. No artificial wait is actually needed to trigger it (the bug isn't timing-dependent, it happens on every single pause), but pausing, waiting a beat, then resuming matches how a real user would interact with it:

```bash
# 1. pause: underspecified weather query
curl -N -s -X POST http://localhost:8003/ -H "Content-Type: application/json" -d '{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "message/stream",
  "params": {
    "message": {
      "role": "user",
      "parts": [{"kind": "text", "text": "What is the weather like today?"}],
      "messageId": "msg-4-1a"
    }
  }
}' --max-time 90 > /tmp/scenario_4_1.json
```

Pull the task ID (should be `input-required`, per the status-text check from the top of this doc), wait a few seconds, then resume against the **same** `taskId`:

```bash
sed -n 's/^data: //p' /tmp/scenario_4_1.json | jq -s '[.[] | select(.result.kind == "task")] | first | .result.id'
```

```bash
# 2. resume - replace <TASK_ID> with the id from step 1
curl -N -s -X POST http://localhost:8003/ -H "Content-Type: application/json" -d '{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "message/stream",
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

Confirm the final status-update's `state` moves to `"completed"` with a real (non-empty) answer, and check weather_agent's console for the absence of `Queue is closed. Event will not be dequeued.`.

### 4.2 — Two concurrent sessions to the same agent

Fire two independent weather queries at the same agent in parallel (no shared `taskId`, so each should get its own task) and confirm no cross-talk between the responses:

```bash
curl -N -s -X POST http://localhost:8003/ -H "Content-Type: application/json" -d '{
  "jsonrpc": "2.0", "id": "1", "method": "message/stream",
  "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": "Current weather in Tokyo, Japan?"}], "messageId": "msg-4-2a"}}
}' --max-time 90 > /tmp/session_a.json &

sleep 1   # stagger slightly - see note below

curl -N -s -X POST http://localhost:8003/ -H "Content-Type: application/json" -d '{
  "jsonrpc": "2.0", "id": "2", "method": "message/stream",
  "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": "Current weather in Sydney, Australia?"}], "messageId": "msg-4-2b"}}
}' --max-time 90 > /tmp/session_b.json &

wait

for f in /tmp/session_a.json /tmp/session_b.json; do
  echo "--- $f ---"
  sed -n 's/^data: //p' "$f" | jq -s '
    [.[] | select(.result.kind == "status-update" and .result.status.state == "completed")]
    | last | .result.status.message.parts[0].text'
  sed -n 's/^data: //p' "$f" | jq -s '[.[] | select(.result.kind == "task")] | first | .result.id'  # confirm two distinct taskIds
done
```

> **Note:** the `sleep 1` stagger works around `weather_agent`'s NVIDIA free-tier backend (`ChatNVIDIA`, `stepfun-ai/step-3.7-flash`), which rejects two truly-simultaneous requests with `429 Too Many Requests` before either reaches the model — a hosted-API rate limit, not a session-isolation bug in our own code. A 1-second head start is enough to dodge the burst limit while the two requests still overlap for most of their duration (the model call itself takes much longer than 1s), so this still meaningfully exercises concurrent task handling. If you still see `.error` in either file instead of `.result`, check for `429` specifically before assuming a real bug — inspect with `jq . /tmp/session_a.json /tmp/session_b.json` and increase the stagger if needed.

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
