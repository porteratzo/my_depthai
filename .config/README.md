# Linting Configuration Structure

All linting and formatting configuration files are organized in the `.config/` directory to keep the repository root clean.

## Directory Structure

```
.config/
├── flake8                    # Flake8 configuration
├── editorconfig             # EditorConfig settings
└── pre-commit-config.yaml   # Pre-commit hooks

pyproject.toml               # Black, isort, Pylint, mypy config (kept in root for build system)
Makefile                     # Convenient commands (handles config paths automatically)
LINTING.md                   # Complete documentation
```

## Usage

All commands work seamlessly through the Makefile, which automatically references the correct config paths:

```bash
make format       # Format code
make lint         # Run linters
make check        # Check without modifying
make setup-precommit  # Setup pre-commit hooks
```

## Tool-Specific Commands

If you need to run tools directly:

```bash
# Flake8
flake8 --config=.config/flake8 mydepthai/ examples/ setup.py

# Pre-commit
pre-commit install --config=.config/pre-commit-config.yaml
pre-commit run --all-files --config=.config/pre-commit-config.yaml

# Black, isort, pylint, mypy (read from pyproject.toml automatically)
black mydepthai/
isort mydepthai/
pylint mydepthai/
mypy mydepthai/
```

## Why pyproject.toml is in the root?

`pyproject.toml` must remain in the repository root because:
1. It's the standard location for Python build system configuration
2. Most tools (Black, isort, Pylint, mypy) automatically look for it there
3. Moving it would break the build process and require explicit config paths for all tools

## Benefits

✅ Clean repository root
✅ Organized configuration files
✅ No symlinks needed
✅ All tools work with explicit paths via Makefile
✅ CI/CD properly configured
