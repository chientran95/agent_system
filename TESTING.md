# Testing guide

Sample `curl` commands for each service. Start whichever services you need first (see [README.md](README.md)):

```bash
uv run code-agent        # http://localhost:8001
uv run research-agent    # http://localhost:8002
uv run orchestrator      # http://localhost:8000
```

The two A2A servers (`code-agent`, `research-agent`) speak JSON-RPC 2.0 over `POST /`, with `message/send` for a single blocking response or `message/stream` (SSE) for live progress - both are streaming-capable, so `message/stream` is generally the more useful one to test with. The orchestrator is a different shape entirely: it's ADK's own session-based REST API (`get_fast_api_app`), not raw A2A.

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

### Route 1: coding request → coding_agent

```bash
curl -s -X POST "http://localhost:8000/run" -H "Content-Type: application/json" -d '{
  "appName": "orchestrator",
  "userId": "tester",
  "sessionId": "s1",
  "newMessage": {
    "role": "user",
    "parts": [{"text": "Write a Python function that reverses a string."}]
  }
}' --max-time 90
```

The response is a JSON array of ADK events. Look for an event with `"functionCall": {"name": "transfer_to_agent", "args": {"agent_name": "coding_agent"}}` confirming the routing decision, followed by an event authored by `coding_agent` with the generated code.

### Route 2: research/content request → research_agent

Create a fresh session first (session IDs shouldn't be reused across unrelated requests):

```bash
curl -s -X POST "http://localhost:8000/apps/orchestrator/users/tester/sessions/s2" \
  -H "Content-Type: application/json" -d '{}'

curl -s -X POST "http://localhost:8000/run" -H "Content-Type: application/json" -d '{
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

### Pretty-printing responses

Both `/run` calls return a JSON array that's easier to read piped through `jq`, e.g.:

```bash
curl -s -X POST "http://localhost:8000/run" ... | jq -r '.[] | "--- \(.author) ---\n\(.content.parts[]?.text // .content.parts[]?.functionCall // empty)"'
```

---

## Tracing

- **Jaeger** (`http://localhost:16686`): select service `code_agent_a2a`, `research_agent_a2a`, or `orchestrator` from the dropdown to see the HTTP/A2A-level trace for any of the above requests. Routing through the orchestrator (rather than hitting an agent directly) produces a single trace spanning both services.
- **Langfuse** (your project dashboard, if configured): shows prompt/completion-level detail for `research_agent` and its `content_writer` subagent - not `code_agent` (no LangChain involved there).
