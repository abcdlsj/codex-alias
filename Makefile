.PHONY: help install uninstall sync test lint clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Install the codexalias CLI onto PATH via uv
	uv tool install --force .

uninstall: ## Remove the installed codexalias CLI
	uv tool uninstall codex-alias

sync: ## Create/refresh the local dev environment
	uv sync

test: ## Run the test suite
	uv run pytest

clean: ## Remove build artifacts and caches
	rm -rf dist build *.egg-info src/*.egg-info .pytest_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
