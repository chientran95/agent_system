.PHONY: install run dev check clean env example jaeger

# Install the project dependencies using uv.
install:
	@python3 -m pip show uv > /dev/null 2>&1 || python3 -m pip install -U uv
	uv install

# Run the local FastAPI app through the uv entry point.
run:
	uv run agent-system

# Run the app with auto-reload for development.
dev:
	uv run agent-system --reload

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
