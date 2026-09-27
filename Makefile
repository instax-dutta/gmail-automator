.PHONY: install test lint fmt typecheck check migrate run

install:      ; uv sync --extra dev
test:         ; uv run pytest
lint:         ; uv run ruff check .
fmt:          ; uv run ruff format .
typecheck:    ; uv run mypy src
check:        ; uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest
migrate:      ; uv run gmail-automator migrate
run:          ; uv run gmail-automator serve
