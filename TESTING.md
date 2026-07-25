# Testing guide

Sample `curl` commands for each service. Start whichever services you need first (see [README.md](README.md)):

```bash
uv run code-agent        # http://localhost:8001
uv run research-agent    # http://localhost:8002
uv run weather-agent     # http://localhost:8003
uv run orchestrator      # http://localhost:8000
```

The three A2A servers (`code-agent`, `research-agent`, `weather-agent`) speak JSON-RPC 2.0 over `POST /`, with `message/send` for a single blocking response or `message/stream` (SSE) for live progress. `code-agent` and `research-agent` are streaming-capable; `weather-agent` is `message/send`-only for now (see its section below). The orchestrator is a different shape entirely: it's ADK's own session-based REST API (`get_fast_api_app`), not raw A2A.

---

## Code agent (port 8001)

### Agent card

```bash
curl -s http://localhost:8001/.well-known/agent-card.json
```

### Generate code (streaming)

```bash
curl -N -s -X POST http://localhost:8001/ -H "Content-Type: application/json" -d '{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "message/stream",
  "params": {
    "message": {
      "role": "user",
      "parts": [{"kind": "text", "text": "Write a Python function that reverses a string."}],
      "messageId": "msg-1"
    }
  }
}' --max-time 90
```

You'll see `TaskStatusUpdateEvent`s stream in as Claude generates the response (token-level chunks), then a final `TaskArtifactUpdateEvent` named `generated_code`.

### Generate code (blocking, single response)

```bash
curl -s -X POST http://localhost:8001/ -H "Content-Type: application/json" -d '{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "message/send",
  "params": {
    "message": {
      "role": "user",
      "parts": [{"kind": "text", "text": "Write a Python function that reverses a string."}],
      "messageId": "msg-1"
    }
  }
}' --max-time 90
```

**If this errors** with a billing/credit message, that's your `CLAUDE_API_KEY`'s Anthropic account, not the wiring - check [console.anthropic.com/settings/billing](https://console.anthropic.com/settings/billing).

---

## Research agent (port 8002)

### Agent card

```bash
curl -s http://localhost:8002/.well-known/agent-card.json
```

### Research a topic and get a blog post (streaming)

```bash
curl -N -s -X POST http://localhost:8002/ -H "Content-Type: application/json" -d '{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "message/stream",
  "params": {
    "message": {
      "role": "user",
      "parts": [{"kind": "text", "text": "Research the current state of protected bike lane funding in US cities and write a blog post about it."}],
      "messageId": "msg-1"
    }
  }
}' --max-time 240
```

`--max-time 240` because this involves a real DuckDuckGo search plus two model calls (the research agent's own reasoning, then the `content_writer` subagent drafting + verifying). You'll see `[tools]`/`[model]` progress events, ending in a `blog_post` artifact. Check `storage/draft-*.md` for the saved file.

**Reliability note**: with a smaller local model, this doesn't always follow the intended research → delegate → return sequence perfectly - on an off run it may answer directly instead of calling `content_writer`. Retry if the result looks like a bullet-point summary instead of a formatted post.

---

## Weather agent (port 8003)

### Agent card

```bash
curl -s http://localhost:8003/.well-known/agent-card.json
```

### A well-specified query (tests tool selection among 17 tools)

```bash
curl -s -X POST http://localhost:8003/ -H "Content-Type: application/json" -d '{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "message/send",
  "params": {
    "message": {
      "role": "user",
      "parts": [{"kind": "text", "text": "What is the current weather in Tokyo, Japan?"}],
      "messageId": "msg-1"
    }
  }
}' --max-time 90
```

Worth watching in the response: which tool it picks (`weather_forecast` vs a national-model tool like `jma_forecast`), and whether it calls `geocoding` first to resolve the place name into coordinates before calling a forecast tool - both are real, observed points of variability with local models, not guaranteed to go the same way every run.

### An underspecified query (triggers input-required)

```bash
curl -s -X POST http://localhost:8003/ -H "Content-Type: application/json" -d '{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "message/send",
  "params": {
    "message": {
      "role": "user",
      "parts": [{"kind": "text", "text": "What is the weather like today?"}],
      "messageId": "msg-1"
    }
  }
}' --max-time 60
```

The response's `result.status.state` will be `"input-required"` (not `"completed"`), with a clarifying question in `result.status.message`, and an `id` field - that's the task ID. Copy it for the next step.

### Resuming a paused task

Send a follow-up message with `taskId` set to the ID from the previous response:

```bash
curl -s -X POST http://localhost:8003/ -H "Content-Type: application/json" -d '{
  "jsonrpc": "2.0",
  "id": "2",
  "method": "message/send",
  "params": {
    "message": {
      "role": "user",
      "taskId": "<paste the task id here>",
      "parts": [{"kind": "text", "text": "Berlin, Germany"}],
      "messageId": "msg-2"
    }
  }
}' --max-time 90
```

This resumes the *same* checkpointed LangGraph state (the task ID doubles as the LangGraph thread ID) - `result.status.state` should move to `"completed"`.

**Reliability note**: the pause/resume mechanism itself is solid - I verified it triggers correctly on missing info and correctly continues the same graph state on resume, across two different local models. What's inconsistent is whether the model *acts correctly* on the resumed answer (e.g. it may re-ask for information you just gave it) - that's a model-capability limitation, matching the same pattern seen with `research_agent`'s subagent delegation. Bigger/more capable local models, or a hosted model, would likely be more reliable here.

---

## Orchestrator (port 8000)

Uses ADK's session-based REST API, not A2A JSON-RPC directly.

### Health / discovery

```bash
curl -s http://localhost:8000/health
curl -s http://localhost:8000/list-apps
```

### Create a session

```bash
curl -s -X POST "http://localhost:8000/apps/orchestrator/users/tester/sessions/s1" \
  -H "Content-Type: application/json" -d '{}'
```

Both routes below use `/run_sse` (streaming) rather than `/run` (blocking) - `-N` disables curl's output buffering so events print as they arrive instead of all at once at the end. Swap in `/run` with plain `-s` if you'd rather wait for one final JSON array.

### Route 1: coding request → coding_agent

```bash
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

Each line is a separate `data: {...}` SSE event as the orchestrator works. Look for one with `"functionCall": {"name": "transfer_to_agent", "args": {"agent_name": "coding_agent"}}` confirming the routing decision, followed by an event authored by `coding_agent` with the generated code.

### Route 2: research/content request → research_agent

Create a fresh session first (session IDs shouldn't be reused across unrelated requests):

```bash
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

Look for `"functionCall": {"name": "transfer_to_agent", "args": {"agent_name": "research_agent"}}`, followed by an event authored by `research_agent` with the blog post (which internally went through `content_writer`).

### Route 3: weather request → weather_agent

```bash
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

Look for `"functionCall": {"name": "transfer_to_agent", "args": {"agent_name": "weather_agent"}}`. If `weather_agent` pauses (missing location), ADK genuinely represents that as a paused long-running tool (`"a2a:response".status.state: "input-required"`, a `longRunningToolIds` entry on the event) rather than silently treating it as a normal completed answer - verified live. **However**: sending a follow-up message in the same ADK session does *not* resume that same paused A2A task. The orchestrator's own LLM just re-decides to `transfer_to_agent` again on the next turn, which creates a brand-new weather_agent task (different task ID) - the original paused LangGraph checkpoint is orphaned. Genuine resume-by-`taskId` only works when calling `weather_agent` directly (see above); wiring the orchestrator to correlate a follow-up turn back to the same paused A2A task is a real gap, not yet implemented.

### Pretty-printing responses

`/run_sse` emits one `data: {...}` line per event, which reads better stripped and piped through `jq`:

```bash
curl -N -s -X POST "http://localhost:8000/run_sse" ... | sed -n 's/^data: //p' | jq -r '"--- \(.author) ---\n\(.content.parts[]?.text // .content.parts[]?.functionCall // empty)"'
```

For `/run` (blocking) instead, the whole response is one JSON array:

```bash
curl -s -X POST "http://localhost:8000/run" ... | jq -r '.[] | "--- \(.author) ---\n\(.content.parts[]?.text // .content.parts[]?.functionCall // empty)"'
```

---

## Tracing

- **Jaeger** (`http://localhost:16686`): select a service from the dropdown to see its trace -
  - `orchestrator`, `code_agent_a2a`, `research_agent_a2a`, `weather_agent_a2a` - the generic A2A/HTTP layer for each service. Routing through the orchestrator (rather than hitting an agent directly) produces a single trace spanning both services.
  - `claude_code_cli` - Claude Code's own internal trace spans (LLM turns, tool calls, token counts) for every `code_agent` request, independent of the A2A layer above.
- **Langfuse** (your project dashboard, if configured): prompt/completion-level detail for `research_agent` and its `content_writer` subagent, and for `weather_agent` (via LangChain callbacks on its LangGraph run), plus the `orchestrator`'s own routing decisions (via a second OTLP export alongside Jaeger - trace structure and token counts, though not the raw prompt text, since ADK's span attributes don't match the naming Langfuse's OTLP mapper expects). Not `code_agent` - no LangChain/litellm call to hook into there.
