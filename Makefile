.PHONY: test lint check help

help:
	@echo "make test   - run unit + end-to-end tests"
	@echo "make lint   - shellcheck (if installed) + python compile check"
	@echo "make check  - validate dispatch.conf.example"

test:
	./tests/run_tests.sh

lint:
	@if command -v shellcheck >/dev/null 2>&1; then \
		shellcheck dispatch.sh tests/run_tests.sh && echo "shellcheck OK"; \
	else \
		echo "shellcheck not installed; skipping shell lint"; \
	fi
	@bash -n dispatch.sh tests/run_tests.sh && echo "bash syntax OK"
	@python3 -m py_compile engine.py tests/test_engine.py && echo "python compile OK"

check:
	./dispatch.sh --check dispatch.conf.example
