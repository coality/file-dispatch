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
	@sh -n dispatch.sh && bash -n tests/run_tests.sh && echo "shell syntax OK"
	@python3 -m py_compile dispatch.py engine.py tests/test_engine.py && echo "python compile OK"

check:
	./dispatch.sh --check dispatch.conf.example
