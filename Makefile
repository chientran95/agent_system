.PHONY: install run-code-agent run-content-agent run-orchestrator check clean env example jaeger

# Install the project dependencies using uv.
install:
	@python3 -m pip show uv > /dev/null 2>&1 || python3 -m pip install -U uv
	uv sync

# Run each agent as its own process; start these in separate terminals,
# code-agent and content-agent first since the orchestrator calls them over A2A.
run-code-agent:
	uv run code-agent

run-content-agent:
	uv run content-agent

run-orchestrator:
	uv run orchestrator

# Validate Python source files for syntax errors and lint with ruff.
check:
	find src -name '*.py' | sort | xargs python3 -m py_compile
	ruff check src

# Create a local .env from the example file.
env:
	cp .env.example .env

# Display the example env template.
example:
	@cat .env.example

# Start a local Jaeger instance for OpenTelemetry tracing.
jaeger:
	docker run -d -p 16686:16686 -p 4317:4317 jaegertracing/all-in-one

# Remove Python cache files.
clean:
	find . -type d -name '__pycache__' -prune -exec rm -rf {} +
	find . -type f -name '*.py[cod]' -delete
