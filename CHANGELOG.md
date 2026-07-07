# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- WSQL parser, transpiler, and executor for SQL-like web crawl queries.
- CTE and `INNER JOIN` semantics with horizontal, dependency-directed crawling.
- Test suite for parser, operators, and executor orchestration.
- Example script: `examples/quotes_to_wikipedia.py`.
- CI workflow for `ruff` and `pytest`.
- `row_to_dict()` utility for normalizing wxpath/`XPathMap` engine output to plain dict rows.

### Changed

- `WSQLExecutor.execute()`, `execute_collect()`, and `wsql_execute()` always yield/return plain `dict` rows.

## [0.0.1] - 2026-07-07

### Added

- Initial public release of the core WSQL engine.

[Unreleased]: https://github.com/rodricios/wsql/compare/v0.0.1...HEAD
[0.0.1]: https://github.com/rodricios/wsql/releases/tag/v0.0.1
