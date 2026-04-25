# Claudette – Sublime Text Plugin

## Build / Lint Commands
- **Lint:** `.venv/bin/ruff check .`
- **Lint + autofix:** `.venv/bin/ruff check --fix .`
- **Type check:** `pyright` (uses `pyrightconfig.json`; stubs in `typings/`)
- No automated test suite – testing is done by reloading the plugin in Sublime Text (`Package Control: Satisfy Dependencies` or saving a `.py` file triggers reload)

## Architecture
This is a Sublime Text 4 package. Entry point is `Claudette.py`; all modules are subpackages (`api/`, `chat/`, `context/`, `settings/`, `statusbar/`, `tools/`). All intra-package imports use relative imports (e.g. `from ..utils import ...`). Plugin commands inherit from `sublime_plugin.WindowCommand` or `sublime_plugin.TextCommand`. Use `sublime.set_timeout(fn, 0)` for all UI updates from background threads.

## Code Style
- **Python 3.8** (plugin runtime); no walrus operator, no `match`, no `TypeAlias`
- **Line length:** 79 characters (`ruff` enforces E501)
- **Formatter / linter:** `ruff` with rules `E, W, F, I` (PEP 8 + isort)
- **Imports:** stdlib → third-party → `sublime`/`sublime_plugin` → relative; one symbol per line for multi-symbol relative imports
- **Types:** Use `typing` module (`Optional`, `List`, `Dict`, `Tuple`); annotate public function signatures; avoid `Any` where possible
- **Naming:** `UpperCamelCase` for classes; `snake_case` for functions/variables; all plugin command classes prefixed `Claudette` (e.g. `ClaudetteAskQuestionCommand`); all utility functions prefixed `claudette_` (e.g. `claudette_get_api_key`)
- **Error handling:** Catch specific exceptions; surface errors to the user via `claudette_chat_status_message()` or `sublime.error_message()`; log unexpected errors with `print(f"{PLUGIN_NAME} Error: ...")` before showing a dialog
- **Docstrings:** Google-style with `Args:` / `Returns:` sections on public functions
- **f-strings** are used throughout; prefer f-strings over `.format()` for simple interpolation; use `.format()` for long multi-line strings to avoid line-length violations
- **Constants** live in `constants.py`; settings are loaded via `sublime.load_settings(SETTINGS_FILE)`
