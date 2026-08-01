# Bug: weather_agent's MCP tool calls intermittently fail with a blank error

**Status:** Fixed
**Component:** Our own code (`weather_agent.py`), working around a documented but easy-to-miss behavior of `langchain_mcp_adapters`
**Affects:** Any MCP tool call made by `weather_agent` (`geocoding`, `weather_forecast`, and the rest of the Open-Meteo toolset)

## Explanation

`weather_agent.py` loaded its MCP tools via `self.mcp_client.get_tools()` (a `MultiServerMCPClient` method). What isn't obvious from the call site - though it *is* stated directly in the method's own docstring - is that tools returned this way are stateless: **a brand-new MCP session is created for every single tool call**, not just once at startup.

For a `stdio` transport (as used here - `npx -y -p open-meteo-mcp-server open-meteo-mcp-server`), "new session" means a brand-new Node.js subprocess gets spawned, initialized, used for exactly one tool call, and presumably torn down - for *every* `geocoding`/`weather_forecast`/etc. call the agent makes. This is slow (visible as repeated `server_start`/`session_initialized` log lines, one pair per tool call) and, worse, introduced a real reliability problem: the freshly-spawned process would sometimes make its first real HTTP call to the actual Open-Meteo API before its own network stack (DNS resolution, socket setup) was fully ready, causing that call to fail - and the `open-meteo-mcp-server` package's error handling doesn't surface a useful message for this failure, just an empty string.

## Symptoms

- Live weather queries would sometimes work and sometimes fail with no useful information, e.g. this real captured sequence:
  ```
  [agent] geocoding({'name': 'Paris', 'countryCode': 'FR'})
  [tools] [{'type': 'text', 'text': 'Error: ', 'id': '...'}]
  ```
  Note the error message is completely blank after `"Error: "` - nothing for the model (or a human reading the log) to act on.
- The MCP server's own structured log confirms the blank error at the source: `{"level":"error","event":"tool_error","tool":"geocoding","error":""}`.
- **Purely intermittent** - the exact same tool call, with the exact same arguments, sent repeatedly in a tight loop, failed 3 times out of 5 in one measured run and succeeded the other 2 - no pattern in which specific calls failed.
- Every tool call (successful or not) was preceded by its own fresh `{"event":"server_start"}` / `{"event":"session_initialized"}` pair in the MCP server's log - one new subprocess per call, confirmed directly rather than inferred.

## Tried fixes / investigation

1. **Reproduced the exact failure directly**, bypassing the full LangGraph agent, by calling the `geocoding` `BaseTool` object's `.ainvoke()` in isolation with the same arguments seen in a live failure.

2. **Ruled out the arguments as the cause**: called `geocoding.ainvoke({"name": "Paris", "countryCode": "FR"})` five times in a row with byte-for-byte identical arguments - 3 failures, 2 successes. Confirmed intermittent, not deterministic.

3. **Noticed the repeated `server_start`/`session_initialized` log pairs** - one set per tool call, not one for the agent's whole lifetime - and traced this to `langchain_mcp_adapters/tools.py`'s `load_mcp_tools()`/`call_tool()` implementation: when no persistent `session` is passed in (which is exactly what `MultiServerMCPClient.get_tools()` does under the hood), `execute_tool()` does `async with create_session(effective_connection, ...) as tool_session: ...` - a brand-new session for that one call, every time. `get_tools()`'s own docstring states this directly: *"A new session will be created for each tool call."*

4. **Confirmed `MultiServerMCPClient` offers a persistent alternative**: `client.session(server_name)`, an async context manager yielding one long-lived `ClientSession`, intended to be entered once and reused - paired with `langchain_mcp_adapters.tools.load_mcp_tools(session)` (passing the live session directly, instead of `None`) to get tools bound to that persistent connection instead of a fresh one per call.

## Solution

Restructured `WeatherAgent` to open **one** persistent MCP session for the agent's whole lifetime instead of relying on `get_tools()`'s default per-call session:

```python
from contextlib import AsyncExitStack
from langchain_mcp_adapters.tools import load_mcp_tools

class WeatherAgent:
    def __init__(self) -> None:
        self.mcp_client = MultiServerMCPClient(_MCP_SERVER_CONFIG)
        ...
        self._exit_stack = AsyncExitStack()

    async def _ensure_graph(self):
        if self._graph is not None:
            return self._graph

        session = await self._exit_stack.enter_async_context(self.mcp_client.session("open_meteo"))
        mcp_tools = await load_mcp_tools(session)
        ...

    async def aclose(self) -> None:
        """Closes the persistent MCP session opened by _ensure_graph."""
        await self._exit_stack.aclose()
```

`AsyncExitStack` is needed because the session is an async context manager that has to stay *open* for the agent's whole lifetime rather than being used in a single `async with` block - `enter_async_context` enters it and keeps it open until `aclose()` is called (or the process exits).

**Verified**: the same "5 identical calls in a row" test that previously showed 3/5 failures now shows 6/6 successes, with exactly one `server_start`/`session_initialized` pair for the entire test run (not one per call) and noticeably faster, more consistent per-call latency (~250-400ms vs. the previous ~270-1200ms range, which included subprocess spin-up overhead on every call). Confirmed live end-to-end via direct A2A calls too: `geocoding` → `weather_forecast` → a correct final weather answer, with no errors, for both Paris and Berlin queries.
