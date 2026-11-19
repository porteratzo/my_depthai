# Linting and Formatting Setup

This document describes the linting and formatting tools configured for this project.

## Tools Configured

- **Black**: Opinionated code formatter
- **isort**: Import statement sorter
- **Flake8**: Style guide enforcement (PEP 8)
- **Pylint**: Advanced code analysis
- **mypy**: Static type checker

## Quick Start

### 1. Install Development Dependencies

```bash
pip install -r requirements-dev.txt
```

Or use the Makefile:

```bash
make install-dev
```

### 2. Format Your Code

```bash
make format
```

This will run Black and isort on your code.

### 3. Check Your Code

```bash
make check
```

This will check formatting and run all linters without modifying files.

### 4. Run Linters Only

```bash
make lint
```

## Available Make Commands

- `make install` - Install package dependencies
- `make install-dev` - Install package + development dependencies
- `make format` - Format code with black and isort
- `make lint` - Run all linters (flake8, pylint)
- `make check` - Run all checks (format check + lint)
- `make type-check` - Run mypy type checking
- `make clean` - Remove build artifacts and cache files

## Configuration Files

- `pyproject.toml` - Configuration for Black, isort, Pylint, and mypy
- `.config/flake8` - Configuration for Flake8
- `.config/editorconfig` - Editor configuration for consistent coding styles
- `.config/pre-commit-config.yaml` - Pre-commit hooks configuration

All linting-specific configuration files are organized in the `.config/` directory.

## Pre-commit Hooks (Optional)

To automatically format and lint your code before each commit:

1. Install pre-commit:
```bash
pip install pre-commit
```

2. Install the git hooks:
```bash
pre-commit install --config=.config/pre-commit-config.yaml
```

3. (Optional) Run against all files:
```bash
pre-commit run --all-files --config=.config/pre-commit-config.yaml
```

Or simply use:
```bash
make setup-precommit
```

Now the linters will run automatically before each commit!

## Manual Commands

If you prefer to run tools individually:

### Black (Formatting)
```bash
# Format code
black mydepthai/ examples/ setup.py

# Check without modifying
black --check mydepthai/ examples/ setup.py
```

### isort (Import Sorting)
```bash
# Sort imports
isort mydepthai/ examples/ setup.py

# Check without modifying
isort --check-only mydepthai/ examples/ setup.py
```

### Flake8 (Linting)
```bash
flake8 --config=.config/flake8 mydepthai/ examples/ setup.py
```

### Pylint (Advanced Linting)
```bash
pylint mydepthai/ examples/ setup.py
```

### mypy (Type Checking)
```bash
mypy mydepthai/
```

## Style Guidelines

- **Line Length**: 100 characters (configurable in `pyproject.toml` and `.flake8`)
- **Import Order**: Following Black's profile (stdlib, third-party, first-party)
- **Python Version**: 3.7+

## Ignoring Specific Rules

### In-line Comments
```python
# Ignore specific flake8 rule
some_code()  # noqa: E501

# Ignore specific pylint rule
some_code()  # pylint: disable=invalid-name
```

### File-level
```python
# At the top of the file
# flake8: noqa
# pylint: skip-file
```

## CI/CD Integration

A GitHub Actions workflow is configured in `.github/workflows/lint.yml` that will:
- Run on pushes and pull requests
- Test against multiple Python versions
- Check formatting and run all linters

## Customization

To adjust the configuration, edit:
- `pyproject.toml` for Black, isort, Pylint, and mypy settings
- `.config/flake8` for Flake8 settings
- `.config/pre-commit-config.yaml` for pre-commit hook versions and settings
- `.config/editorconfig` for editor-specific settings
