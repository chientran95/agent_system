# Bug: weather_agent fails to select a tool from its 17-tool MCP toolset

**Status:** Fixed, via a model swap + a two-pass tool-selection redesign — not via the originally-suspected cause
**Component:** Our own code (`weather_agent.py`) for the truncation fix and the two-pass redesign; a `mistral-small3.2:24b`-specific tool-calling failure (root cause not identified, worked around by switching models) for the middle problem
**Affects:** `weather_agent`'s ability to actually call any of the Open-Meteo MCP server's 17 tools (`geocoding`, `weather_forecast`, and the various national-model forecast tools)

## Explanation

`weather_agent` binds all 17 tools from the `open-meteo-mcp` server to its LangGraph `agent` node via `model_with_tools = self.model.bind_tools(tools)`. There turned out to be two separate problems stacked on top of each other, discovered in order:

### Problem 1 (fixed): silent context-window truncation

The 17 tool schemas alone serialize to **157,816 characters of JSON** (roughly 39,000+ tokens) — the Open-Meteo MCP server's tool definitions are unusually verbose. `weather_agent.py` constructed its `ChatOllama` model with no `num_ctx` set:

```python
self.model = ChatOllama(model=WEATHER_AGENT_MODEL)
```

Ollama defaults to a small context window (observed: exactly 4096 tokens) when a client doesn't explicitly request a larger one via `options.num_ctx` — **regardless of the model's own actual maximum context length**. With tool schemas alone already exceeding that default several times over, Ollama silently truncated the request down to fit, cutting the tool definitions out (partially or entirely) before the model ever saw them. The model then correctly, from its own truncated point of view, reported having no tools available.

### Problem 2 (root cause not identified, worked around): `mistral-small3.2:24b` fails to call tools regardless of prompt size

Fixing problem 1 (see Solution below) and re-verifying with `mistral-small3.2:24b` (native context: 131,072 tokens, comfortably above what's needed) confirmed the model now receives the **complete, untruncated** tool schemas — `prompt_eval_count` in the response came back as `48143`, well inside the configured window, not clamped to a small number.

Despite that, the model still returns an **effectively empty response**: no text, no tool call, only ~12-14 tokens generated, `done_reason: 'stop'`. The original hypothesis was that this was still a volume problem - the model being overwhelmed by the sheer amount of tool-schema text even though it technically fits in context - which motivated the two-pass redesign below.

**That hypothesis turned out to be wrong.** After implementing the two-pass selection (see Solution), the narrowed-down agent turn - just 3 small tools, `prompt_eval_count: 5849` - *still* returned an identical empty response with `mistral-small3.2:24b`. To isolate whether this was even specific to our own code, the same system prompt + the same 3 tool schemas + the same user message was sent **directly to Ollama's native `/api/chat` endpoint, bypassing `langchain_ollama`, LangGraph, and our own code entirely**. Result: byte-for-byte the same failure (`content: ''`, `tool_calls: None`, `eval_count: 14`, `done_reason: 'stop'`). This rules out tool-schema volume, `langchain_ollama`, and our own graph logic as the cause - it's `mistral-small3.2:24b` itself failing to produce a tool call for this prompt shape via Ollama, for reasons not further identified.

**Switching `WEATHER_AGENT_MODEL` to `qwen2.5:7b` resolved it** - tool-calling started working. Note `qwen2.5:7b`'s native context ceiling is only **32,768 tokens** - smaller than the ~40,000 tokens the full 17-tool schema set requires, so this model could never have worked against the *original* all-17-tools binding regardless of `num_ctx` (Ollama silently clamps `num_ctx` requests above a model's own native maximum). It only became viable once the two-pass redesign (Problem 3) cut the per-turn tool set down to a handful of small schemas that fit comfortably within its smaller window.

## Symptoms

- Direct query (bypassing A2A/orchestrator entirely) for "What is the current weather in Tokyo, Japan?" returns a plain-text non-answer instead of calling a tool, e.g.:
  > "I'm sorry, but I currently don't have the tools to provide you with the current weather in Tokyo, Japan. However, you can easily find this information by checking a reliable weather website or application."
- After the `num_ctx` fix: the same query instead returns a **silently empty** final answer (`state: completed`, empty text) — no hallucinated excuse, but also no real answer.
- Raw LangChain message inspection during the empty-response case shows:
  ```
  type: ai
  content: ''
  tool_calls: []
  response_metadata: {..., 'done_reason': 'stop', 'eval_count': 12, ...}
  ```
- An underspecified query ("What is the weather like today?") sometimes produces a reasonable-looking clarifying question as **direct answer text** ("I need a location to check the weather for.") rather than genuinely pausing via the `ask_user` tool into `input-required` state — i.e. even when the model does something sensible, it isn't necessarily going through the intended tool-calling path.

## Tried fixes / investigation

1. **Reproduced in complete isolation**, bypassing A2A and the orchestrator entirely (calling `WeatherAgent.astream_query()` directly in a standalone script) — confirmed the failure is in the model/LangChain layer, not in A2A plumbing, the orchestrator, or the mesh.

2. **Measured the actual request size.** Extracted the 17 tools' JSON schemas via `langchain_core.utils.function_calling.convert_to_openai_tool` and measured: 157,816 characters of tool-schema JSON alone, against a 1,687-character system prompt.

3. **Confirmed the truncation via Ollama's own `/api/chat` endpoint directly** (bypassing LangChain too), sending the real system prompt + all 17 tool schemas: `prompt_eval_count` came back as exactly `4096` — a suspiciously round number matching Ollama's known default context window, confirming truncation rather than a model decision.

4. **Checked the model's actual native context length** via `/api/show`: `mistral-small3.2:24b` reports `131072`, `qwen2.5:7b` reports `32768`. Both are far larger than the 4096 default being silently applied.

5. **Applied the fix** (see below) and re-verified via direct raw-message inspection (not just the final text, which had previously masked whether a tool call was attempted) that the model now genuinely receives an untruncated prompt.

6. **Tested with `qwen2.5:7b`** post-fix: `prompt_eval_count` came back as `32768` — clamped to the model's own native ceiling despite requesting `num_ctx=65536` — and still an empty response (`content: ''`, `tool_calls: []`). Confirms `qwen2.5:7b`'s context is simply too small for this tool set, independent of the fix.

7. **Tested with `mistral-small3.2:24b`** post-fix, whose native context is large enough to not be clamped: `prompt_eval_count` came back as `48143` (no clamping — genuinely under the configured 65536 ceiling), yet the response was *still* empty (`content: ''`, `tool_calls: []`, `eval_count: 12`). This isolates problem 2 as real and distinct from problem 1 — full context visibility is not sufficient on its own.

8. **Along the way, found and fixed a related observability bug** in `weather_agent.py`'s `_astream_graph`: it only yielded a progress chunk when `message.content` was non-empty. A normal, *successful* tool-calling turn has empty `content` (the decision lives in the separate `tool_calls` field instead) — so this was silently hiding real tool-call attempts from progress streaming and logs, making it look like "nothing happened" even on turns where a tool call genuinely was attempted. Fixed to also surface a `tool_name(args)` summary when `content` is empty but `tool_calls` is populated.

9. **Implemented the two-pass tool-selection redesign** (see Solution) to address the volume hypothesis. First rollout hit a bug: `WeatherState` stored the selected `BaseTool` objects directly, and since the graph is compiled with a checkpointer that msgpack-serializes state after every step, this crashed with `TypeError: Type is not msgpack serializable: StructuredTool`. Fixed by storing only tool *names* (strings) in state and resolving them back to actual tool objects from a closure dict inside `agent_node`.

10. **Re-tested `mistral-small3.2:24b` against the narrowed two-pass output**: the selector node correctly picked `['weather_forecast']` from name+description alone. The subsequent agent turn - now just 3 small tools bound, `prompt_eval_count: 5849` - *still* returned an empty response (`content: ''`, `tool_calls: []`, `eval_count: 14`). This directly falsified the volume hypothesis: the narrowed prompt is tiny and it still fails identically.

11. **Isolated the failure to Ollama itself, independent of any Python framework**: sent the exact same system prompt + the same 3 tool schemas + the same user message straight to Ollama's native `/api/chat` endpoint via a raw HTTP call, with `langchain_ollama`, LangGraph, and our own code entirely out of the loop. Result: byte-for-byte the same empty response (`content: ''`, `tool_calls: None`, `eval_count: 14`, `done_reason: 'stop'`). This is decisive - the bug is in `mistral-small3.2:24b`'s tool-calling for this prompt shape via Ollama, not in `langchain_ollama`, not in our graph code, and not in tool-schema volume.

12. **Swapped `WEATHER_AGENT_MODEL` to `qwen2.5:7b`** (on top of the two-pass redesign, which is what makes this viable given `qwen2.5:7b`'s smaller 32,768-token native context) and re-tested live via the ADK Dev UI: tool-calling now works, and the agent correctly reaches `input-required` (asking a clarifying question) instead of returning an empty response. Confirmed the failure was specific to `mistral-small3.2:24b`, not a fundamental Ollama/local-model limitation.

## Solution

**Problem 1 (context truncation) — fixed.** `weather_agent.py`:

```python
# The Open-Meteo MCP server's 17 tool schemas alone run to ~40K
# tokens; Ollama silently truncates to its default 4096-token
# context if not told otherwise, which drops the tool definitions
# entirely and makes the model think it has no tools available.
self.model = ChatOllama(model=WEATHER_AGENT_MODEL, num_ctx=65536)
```

Verified via `response_metadata.prompt_eval_count` no longer being clamped to a small number on a model with sufficient native context.

**Problem 2 (`mistral-small3.2:24b` tool-calling failure) — worked around, root cause not identified.** Switching `WEATHER_AGENT_MODEL` to `qwen2.5:7b` resolved it in practice. The underlying "why does `mistral-small3.2:24b` return an empty response to this exact prompt via Ollama" question was not answered - it was decisively isolated to the model/Ollama combination (step 11 above) but not root-caused further, since a working alternative was available and the isolation work already consumed significant effort.

**Update: `weather_agent` has since moved off Ollama entirely.** `ChatOllama` was replaced with `ChatNVIDIA` (NVIDIA's hosted API), currently running `stepfun-ai/step-3.7-flash`. This was a separate, later decision (not because `qwen2.5:7b` stopped working) - see the model-selection discussion in this project's history for the reasoning. The two-pass tool-selection redesign below is unaffected by the provider change and remains the reason tool-calling works reliably regardless of which backend model is used.

**Two-pass tool selection — implemented** (this is what makes `qwen2.5:7b` viable, and is worth keeping regardless of which model is used, since it also cuts per-turn latency/token cost significantly). `weather_agent.py`'s `_ensure_graph()` now has three nodes instead of two:

1. **`select_tools`** (new, runs first): shows the model only `name: description` for all 17 tools (no parameter schemas - roughly 4-5KB total vs. 157KB for full schemas) and asks it to pick up to 3 via `with_structured_output(ToolSelection)`. Falls back to `["weather_forecast"]` if the selector errors or returns nothing usable.
2. **`agent`** (modified): looks up the actual tool objects for the selected names from a closure dict, and dynamically binds `[always_included (geocoding), *selected, ask_user]` - typically 3-5 tools with full schemas, instead of all 17 - for that turn's `bind_tools()` call.
3. **`tools`** (unchanged): still constructed with the full `[*mcp_tools, ask_user]` set, since it only needs to be able to *execute* whatever tool call comes back, not know in advance which subset was bound.

`geocoding` is always force-included regardless of what the selector picks, since almost every query needs it to resolve a place name to coordinates and it's cheap (932 bytes) - leaving it up to the selector's judgment was judged too risky given how much local-model reliability trouble this whole investigation surfaced.

Selection re-runs once per new query (graph entry point), not on every loop iteration - resuming a paused `ask_user` interrupt via `Command(resume=...)` continues from inside the interrupted node and does not re-enter `select_tools`, so the original selection for that conversation stays in effect.

## A separate bug you'll hit next

Once `weather_agent` reaches `input-required` (e.g. asking for a location) via the **ADK Dev UI / orchestrator**, answering it hits a *different*, already-documented, unresolved bug: see [`BUG_ORCHESTRATOR_RESUME.md`](BUG_ORCHESTRATOR_RESUME.md). Symptom: `Queue is closed. Event will not be dequeued.` logged and no further output after answering. This is not related to tool selection - direct agent-to-agent resume against `weather_agent`'s own port (bypassing the orchestrator) has been separately verified to work correctly.
