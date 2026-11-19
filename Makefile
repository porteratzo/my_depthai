
.PHONY: help install install-dev format lint check test clean setup-precommit

help:
	@echo "Available commands:"
	@echo "  make install         - Install package dependencies"
	@echo "  make install-dev     - Install package + development dependencies"
	@echo "  make format          - Format code with black and isort"
	@echo "  make lint            - Run all linters (flake8)"
	@echo "  make check           - Run all checks (format check + lint)"
	@echo "  make type-check      - Run mypy type checking"
	@echo "  make setup-precommit - Install pre-commit hooks"
	@echo "  make clean           - Remove build artifacts and cache files"

install:
	pip install -e .

install-dev:
	pip install -e ".[dev]"

format:
	@echo "Formatting code with black..."
	black mydepthai/ examples/ tests/
	@echo "Sorting imports with isort..."
	isort mydepthai/ examples/ tests/

lint:
	@echo "Running flake8..."
	flake8 mydepthai/ examples/ tests/

check:
	@echo "Checking code formatting with black..."
	black --check mydepthai/ examples/ tests/
	@echo "Checking import sorting with isort..."
	isort --check-only mydepthai/ examples/ tests/
	@echo "Running flake8..."
	flake8 mydepthai/ examples/ tests/

type-check:
	@echo "Running mypy type checking..."
	mypy mydepthai/

setup-precommit:
	@echo "Installing pre-commit..."
	pip install pre-commit
	@echo "Setting up pre-commit hooks (using root config)..."
	pre-commit install
	@echo "Running pre-commit on all files..."
	pre-commit run --all-files

clean:
	@echo "Cleaning build artifacts..."
	rm -rf build/ dist/ *.egg-info
	@echo "Cleaning cache files..."
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete
	find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
