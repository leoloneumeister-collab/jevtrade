.PHONY: check lint types test fmt
check: lint types test      ## everything CI runs
lint:
	ruff check . && ruff format --check .
types:
	mypy
test:
	pytest -q
fmt:
	ruff format . && ruff check . --fix
