"""
CLAUDE.md project memory loader.

Mirrors Claude Code's CLAUDE.md behavior (see ``claude-code-sourcemap``):

- **Ancestor walk**: for each allowed project root, walk from the root up to
  the filesystem root and read every ``CLAUDE.md`` encountered. Contents are
  concatenated (outer-to-inner) under the same "strict style guidelines"
  preamble Claude Code uses, so the user's personal and organizational
  CLAUDE.md files higher up the tree compose with the project-specific one.
- **Nested scan**: after the ancestor walk, a bounded ``os.walk`` per root
  collects any additional ``CLAUDE.md`` files under the project. Their
  contents are *not* inlined — only a bullet list of absolute paths, so
  Claude knows to read them with the text editor tool when it works in
  those subdirectories. A 3-second wall-clock deadline and a prune list
  (``.git``, ``node_modules``, ...) keep large repos from stalling
  request startup.

The returned system message is marked ``cache_control: ephemeral`` so
repeated turns in the same conversation do not re-bill CLAUDE.md tokens
after the first turn (matching the caching already used for the
``<reference_files>`` block in ``ClaudetteClaudeAPI._build_system_messages``).
"""

import os
import time
from typing import Dict, List, Optional, Tuple

from ..tools.text_editor import get_allowed_roots

# Cap per-file read so a runaway CLAUDE.md can't blow out the token budget.
# 100 KB is ~25k tokens, well above any sensible project memory file.
_MAX_BYTES_PER_FILE = 100 * 1024

# Wall-clock budget for the nested scan across all roots; matches
# ``getClaudeFiles`` in ``claude-code-sourcemap/src/context.ts``.
_NESTED_SCAN_TIMEOUT_SECONDS = 3.0

# Directories pruned during the nested scan. Kept intentionally short —
# the goal is avoiding well-known vendor/build dumps, not perfect filtering.
_PRUNE_DIRS = frozenset(
    [
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "env",
        ".env",
        "dist",
        "build",
        ".next",
        ".nuxt",
        "target",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".cache",
        ".idea",
        ".vscode",
    ]
)

_STYLE_PROMPT = (
    "The codebase follows strict style guidelines shown below. All code "
    "changes must strictly adhere to these guidelines to maintain "
    "consistency and quality."
)

_NESTED_NOTE_PROMPT = (
    "NOTE: Additional CLAUDE.md files were found. When working in these "
    "directories, make sure to read and follow the instructions in the "
    "corresponding CLAUDE.md file:"
)


def _read_claude_md(path: str) -> Optional[str]:
    """Read a CLAUDE.md file, capped at ``_MAX_BYTES_PER_FILE``."""
    try:
        with open(path, "rb") as f:
            raw = f.read(_MAX_BYTES_PER_FILE + 1)
    except OSError:
        return None
    truncated = len(raw) > _MAX_BYTES_PER_FILE
    if truncated:
        raw = raw[:_MAX_BYTES_PER_FILE]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
    if truncated:
        text += (
            "\n\n[... CLAUDE.md truncated at {0} bytes ...]"
        ).format(_MAX_BYTES_PER_FILE)
    return text


def _realpath_or_none(path: str) -> Optional[str]:
    try:
        return os.path.realpath(path)
    except OSError:
        return None


def _walk_ancestor_claude_mds(
    root: str, seen_real: set
) -> List[Tuple[str, str]]:
    """
    Walk from ``root`` up to the filesystem root, collecting CLAUDE.md files.

    Returns a list of ``(display_path, contents)`` tuples ordered from the
    *outermost* ancestor down to ``root`` itself, matching Claude Code's
    ``styles.reverse()`` ordering. ``seen_real`` is updated in-place with the
    realpath of every returned file so the nested scan can skip them.
    """
    collected: List[Tuple[str, str]] = []
    try:
        current = os.path.abspath(root)
    except OSError:
        return collected

    # Guard against symlink loops and weird filesystems by bounding depth.
    for _ in range(64):
        candidate = os.path.join(current, "CLAUDE.md")
        if os.path.isfile(candidate):
            real = _realpath_or_none(candidate)
            if real is not None and real not in seen_real:
                contents = _read_claude_md(candidate)
                if contents is not None and contents.strip():
                    collected.append((candidate, contents))
                    seen_real.add(real)
        parent = os.path.dirname(current)
        if not parent or parent == current:
            break
        current = parent

    # Outer-to-inner order: deepest ancestor last.
    collected.reverse()
    return collected


def _scan_nested_claude_mds(
    root: str, deadline: float, seen_real: set
) -> List[str]:
    """
    Return absolute paths of CLAUDE.md files strictly *under* ``root``.

    Excludes the root-level ``CLAUDE.md`` (already handled by the ancestor
    walk) and any file whose realpath is already in ``seen_real``. Walk is
    pruned against ``_PRUNE_DIRS`` and aborts once ``deadline`` passes.
    """
    found: List[str] = []
    try:
        root_abs = os.path.abspath(root)
    except OSError:
        return found
    if not os.path.isdir(root_abs):
        return found

    for dirpath, dirnames, filenames in os.walk(
        root_abs, topdown=True, followlinks=False
    ):
        if time.monotonic() > deadline:
            break
        dirnames[:] = [d for d in dirnames if d not in _PRUNE_DIRS]
        if "CLAUDE.md" not in filenames:
            continue
        if os.path.normpath(dirpath) == root_abs:
            # Root-level CLAUDE.md is handled by the ancestor walk.
            continue
        candidate = os.path.join(dirpath, "CLAUDE.md")
        real = _realpath_or_none(candidate)
        if real is None or real in seen_real:
            continue
        seen_real.add(real)
        found.append(candidate)

    found.sort()
    return found


def build_claude_md_system_block(window, settings) -> Optional[Dict]:
    """
    Build a cached system message for CLAUDE.md context, or ``None``.

    Uses ``get_allowed_roots`` so sidebar folders, ``allowed_tool_roots``,
    and the saved-file fallback all resolve identically to the text editor
    and bash tools. Returns a single content block of the form
    ``{"type": "text", "text": ..., "cache_control": {"type": "ephemeral"}}``
    or ``None`` when no CLAUDE.md is found anywhere reachable.
    """
    if window is None:
        return None

    roots = get_allowed_roots(window, settings) or []
    if not roots:
        return None

    seen_real: set = set()
    ancestor_entries: List[Tuple[str, str]] = []
    for root in roots:
        ancestor_entries.extend(
            _walk_ancestor_claude_mds(root, seen_real)
        )

    deadline = time.monotonic() + _NESTED_SCAN_TIMEOUT_SECONDS
    nested_paths: List[str] = []
    for root in roots:
        if time.monotonic() > deadline:
            break
        nested_paths.extend(
            _scan_nested_claude_mds(root, deadline, seen_real)
        )

    if not ancestor_entries and not nested_paths:
        return None

    sections: List[str] = []
    if ancestor_entries:
        style_blocks = [
            "Contents of {0}:\n\n{1}".format(path, contents)
            for path, contents in ancestor_entries
        ]
        sections.append(
            "{0}\n\n{1}".format(_STYLE_PROMPT, "\n\n".join(style_blocks))
        )
    if nested_paths:
        bullet_list = "\n".join("- {0}".format(p) for p in nested_paths)
        sections.append("{0}\n{1}".format(_NESTED_NOTE_PROMPT, bullet_list))

    text = "\n\n".join(sections)
    return {
        "type": "text",
        "text": text,
        "cache_control": {"type": "ephemeral"},
    }
