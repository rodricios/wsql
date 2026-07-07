# Contributing to WSQL

Thank you for your interest in contributing.

## Development setup

```bash
git clone https://github.com/rodricios/wsql.git
cd wsql
uv sync --extra test --extra dev
```

## Checks

Run these before opening a pull request:

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run pytest
```

## Code style

- Python 3.10+ with type annotations on public APIs.
- Google-style docstrings on functions and modules.
- Format and lint with [ruff](https://docs.astral.sh/ruff/).
- Add or update tests for behavior changes.

## Pull requests

1. Open an issue for large changes when possible.
2. Keep diffs focused on one concern.
3. Ensure CI passes.
4. Update `CHANGELOG.md` under `[Unreleased]` for user-visible changes.

## Design notes

Longer-form design discussion lives in [docs/design/](docs/design/).

## License

By contributing, you agree that your contributions will be licensed under the
project's [AGPL-3.0](LICENSE) license.
