PYTHON ?= python3

.PHONY: ci test

ci: test

test:
	$(PYTHON) -m pytest
