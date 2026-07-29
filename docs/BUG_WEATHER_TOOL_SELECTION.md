# Bug: weather_agent fails to select a tool from its 17-tool MCP toolset

**Status:** Partially fixed — a real context-window truncation bug is fixed and verified; a second, deeper problem remains unresolved
**Component:** Our own code (`weather_agent.py`) for the truncation half; local model capability (`mistral-small3.2:24b`, `qwen2.5:7b` via Ollama) for the unresolved half
**Affects:** `weather_agent`'s ability to actually call any of the Open-Meteo MCP server's 17 tools (`geocoding`, `weather_forecast`, and the various national-model forecast tools)

## Explanation

`weather_agent` binds all 17 tools from the `open-meteo-mcp` server to its LangGraph `agent` node via `model_with_tools = self.model.bind_tools(tools)`. There turned out to be two separate problems stacked on top of each other, discovered in order:

### Problem 1 (fixed): silent context-window truncation

The 17 tool schemas alone serialize to **157,816 characters of JSON** (roughly 39,000+ tokens) — the Open-Meteo MCP server's tool definitions are unusually verbose. `weather_agent.py` constructed its `ChatOllama` model with no `num_ctx` set:

```python
self.model = ChatOllama(model=WEATHER_AGENT_MODEL)
```

Ollama defaults to a small context window (observed: exactly 4096 tokens) when a client doesn't explicitly request a larger one via `options.num_ctx` — **regardless of the model's own actual maximum context length**. With tool schemas alone already exceeding that default several times over, Ollama silently truncated the request down to fit, cutting the tool definitions out (partially or entirely) before the model ever saw them. The model then correctly, from its own truncated point of view, reported having no tools available.

### Problem 2 (unresolved): the model still fails even with full, untruncated visibility

Fixing problem 1 (see Solution below) and re-verifying with `mistral-small3.2:24b` (native context: 131,072 tokens, comfortably above what's needed) confirmed the model now receives the **complete, untruncated** tool schemas — `prompt_eval_count` in the response came back as `48143`, well inside the configured window, not clamped to a small number.

Despite that, the model still returns an **effectively empty response**: no text, no tool call, only ~12 tokens generated, `done_reason: 'stop'`. This is not a truncation artifact — the model has full visibility of every tool and still fails to productively use any of them, apparently overwhelmed by the sheer volume of tool-schema text it has to reason over in a single turn.

A separate, related finding: `qwen2.5:7b`'s native context ceiling is only **32,768 tokens** — smaller than what the 17 tool schemas alone require (~39,000+ tokens). No `num_ctx` setting can fix truncation for that model; requesting a larger window (e.g. `num_ctx=65536`) simply gets silently clamped back down to the model's own hard maximum. This is a genuine model-capacity mismatch, not a bug.

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

**Problem 2 (model fails to use tools even with full context) — not fixed.** Accepted as a known local-model-capability limitation for now (decision made explicitly rather than defaulted into): `.env`'s `WEATHER_AGENT_MODEL` stays on `mistral-small3.2:24b`, and no further code changes were made to work around it.

**Not yet attempted, most likely real fix:** reduce how much tool information the model has to reason over per turn, rather than continuing to throw larger context windows or different models at the volume problem. Concretely: bind a smaller, pre-filtered/relevant subset of the 17 tools per query (e.g. route based on keywords or a lightweight classification step) instead of always binding all 17 at once. Not implemented in this pass.
