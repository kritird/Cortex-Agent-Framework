# Contributing to Cortex Agent Framework

Thank you for your interest in contributing! This project is maintained by [Kriti Ranjan Das](https://github.com/kritird) and welcomes contributions of all kinds — bug fixes, new features, documentation improvements, and tests.

---

## Getting Started

### 1. Fork and clone the repository

```bash
git clone https://github.com/kritird/cortex-agent-framework.git
cd cortex-agent-framework
```

### 2. Set up a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate      # macOS/Linux
.venv\Scripts\activate         # Windows
```

### 3. Install in development mode

```bash
pip install -e ".[dev]"
```

### 4. Run the tests

```bash
pytest tests/ -v
```

Integration tests (require an Anthropic API key) are skipped automatically if `ANTHROPIC_API_KEY` is not set.

---

## How to Contribute

### Reporting Bugs

Open a GitHub Issue and include:
- A clear title and description
- Steps to reproduce
- Expected vs actual behaviour
- Python version and OS
- Relevant `cortex.yaml` config (redact any secrets)

### Suggesting Features

Open a GitHub Issue tagged `enhancement`. Describe:
- The problem you're solving
- Your proposed solution
- Any alternatives you considered

### Submitting a Pull Request

1. Create a branch from `main`:
   ```bash
   git checkout -b feature/my-feature
   ```

2. Make your changes. Follow the code style guidelines below.

3. Add or update tests in `tests/` for any changed behaviour.

4. Run the full test suite and confirm it passes:
   ```bash
   pytest tests/ -v
   ```

5. Push your branch and open a Pull Request against `main`.

6. Fill in the PR description — explain *what* changed and *why*.

---

## Code Style Guidelines

- **Python 3.11+** — use modern type hints (`list[str]`, `str | None`, etc.)
- **Async everywhere** — all I/O must be async; no blocking calls in async functions
- **No print statements** — use `logging.getLogger(__name__)` instead
- **Pydantic v2** for all config models
- **Docstrings** on public classes and methods
- Keep functions focused — if a function is doing more than one thing, split it
- Security-sensitive code (sanitiser, scrubber, sandbox) requires extra care and review

---

## Project Structure

```
cortex/
├── framework.py          # Main public API — CortexFramework
├── config/               # YAML loading, Pydantic schema, validation
├── modules/              # Core orchestration modules
├── llm/                  # LLM providers (Anthropic, Bedrock, Azure, custom)
├── storage/              # Storage backends (memory, SQLite, Redis)
├── security/             # Input sanitisation, credential scrubbing, bash sandbox
├── streaming/            # SSE event generator and status events
├── testing/              # Test harness, mock server, handler runner
├── cli/                  # Click CLI commands
└── wizard/               # Browser-based setup wizard
tests/                    # Unit and integration tests
```

---

## Areas Where Contributions Are Especially Welcome

- **New LLM providers** — add a file in `cortex/llm/providers/` following the existing pattern
- **Storage backends** — e.g. PostgreSQL, DynamoDB
- **Tool server transports** — improve WebSocket and stdio MCP support
- **CLI improvements** — richer `cortex dev` output, better error messages
- **Documentation** — examples, tutorials, cookbook recipes
- **Tests** — increase coverage, especially for edge cases in the task graph and session manager

---

## Commit Message Style

Use the following prefixes:

| Prefix | Use for |
|--------|---------|
| `feat:` | New feature |
| `fix:` | Bug fix |
| `docs:` | Documentation only |
| `test:` | Adding or updating tests |
| `refactor:` | Code change with no functional difference |
| `chore:` | Build, CI, dependency updates |

Example: `feat: add PostgreSQL storage backend`

---

## Code of Conduct

Be respectful and constructive. This project follows the [Contributor Covenant Code of Conduct](https://www.contributor-covenant.org/version/2/1/code_of_conduct/).

---

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE) — the same license as this project.

---

*Maintained by [Kriti Ranjan Das](https://github.com/kritird). Questions? Open an issue or start a discussion on GitHub.*
