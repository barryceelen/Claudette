"""
Text editor tool executor for Anthropic's text editor tool.

Resolves paths against allowed roots (e.g. project folders), runs
view/str_replace/create/insert, and returns tool_result payloads.

Security and safety: symlink-safe containment (``ensure_under_root``),
read-before-write mtime tracking when ``read_file_timestamps`` is passed,
encoding detection for read/write round-trips, and rejection of ``.ipynb``
files (use a dedicated notebook editor).
"""

import os
from typing import Any, Dict, List, Optional, Tuple

_IPYNB_NOT_SUPPORTED = (
    "Error: Jupyter Notebook (.ipynb) files are not supported by the text "
    "editor tool. Edit notebooks outside Claudette or convert to a script."
)

NO_ALLOWED_ROOTS_MESSAGE = (
    "Error: No allowed project roots. Add a folder to the sidebar, add "
    "paths to the allowed_tool_roots setting, or save the active file so its "
    "directory can be used."
)


def _extra_allowed_roots_from_settings(settings) -> List[str]:
    """
    Extra directories from the allowed_tool_roots setting (text editor and bash tools).

    Entries are deduplicated by normalized path; order is preserved.
    """
    ordered: List[str] = []
    seen = set()
    if not settings:
        return ordered
    block = settings.get("allowed_tool_roots")
    if not block or not isinstance(block, list):
        return ordered
    for path in block:
        if not path or not isinstance(path, str):
            continue
        p = os.path.normpath(path.strip())
        if p and os.path.isdir(p) and p not in seen:
            seen.add(p)
            ordered.append(p)
    return ordered


def get_allowed_roots(window, settings) -> List[str]:
    """
    Return list of allowed filesystem roots for the text editor and bash tools.

    Order: sidebar folders (window.folders()), then allowed_tool_roots from settings.
    If there are no sidebar folders yet, uses the active view's file directory when
    the file is saved to disk.
    """
    roots = []

    if window:
        folders = window.folders()
        if folders:
            roots.extend(os.path.normpath(str(f)) for f in folders)

    for p in _extra_allowed_roots_from_settings(settings):
        if p not in roots:
            roots.append(p)

    if not roots and window:
        view = window.active_view()
        if view and view.file_name():
            roots.append(os.path.dirname(view.file_name()))

    return roots


def _is_ipynb_path(resolved: str) -> bool:
    return resolved.lower().endswith(".ipynb")


def _read_file_with_encoding(path: str) -> Tuple[str, str]:
    """
    Read file text; detect encoding (UTF-8 BOM, UTF-8, Latin-1).

    Latin-1 is a last resort that decodes every byte; use for write-back only
    when that path was chosen.
    """
    # Only select utf-8-sig if a BOM is actually present. Using utf-8-sig as a
    # blind first try would also "work" for plain UTF-8 files and then writes
    # would re-save them with a BOM unexpectedly.
    try:
        with open(path, "rb") as f:
            prefix = f.read(3)
    except OSError:
        prefix = b""

    encodings = ("utf-8-sig", "utf-8", "latin-1")
    if prefix != b"\xef\xbb\xbf":
        encodings = ("utf-8", "latin-1")

    for enc in encodings:
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read(), enc
        except UnicodeDecodeError:
            continue
    raise OSError("Could not decode file as text.")


def _write_file_with_encoding(path: str, text: str, encoding: str) -> None:
    # Normalize utf-8-sig writes to utf-8 to avoid emitting BOM bytes.
    write_encoding = "utf-8" if encoding == "utf-8-sig" else encoding
    with open(path, "w", encoding=write_encoding, newline="") as f:
        f.write(text)


def _timestamp_key(path: str) -> str:
    try:
        return os.path.realpath(path)
    except OSError:
        return path


def _stale_write_error(
    path: str,
    read_file_timestamps: Optional[Dict[str, float]],
) -> Optional[str]:
    """
    Return an error if the file was not read or changed on disk since read.

    Prevents overwriting edits made outside the tool loop.
    When ``read_file_timestamps`` is None, checks are skipped (callers that do
    not track reads).
    """
    if read_file_timestamps is None:
        return None
    key = _timestamp_key(path)
    if key not in read_file_timestamps:
        return (
            "Error: File has not been read yet. Read it first before writing "
            "to it."
        )
    try:
        current = os.path.getmtime(path)
    except OSError as e:
        return "Error: Could not verify file state ({0}).".format(str(e))
    if abs(current - read_file_timestamps[key]) > 1e-6:
        return (
            "Error: File has been modified since it was read. Read it again "
            "before writing."
        )
    return None


def _record_read(
    path: str,
    read_file_timestamps: Optional[Dict[str, float]],
) -> None:
    if read_file_timestamps is None:
        return
    if not os.path.isfile(path):
        return
    try:
        read_file_timestamps[_timestamp_key(path)] = os.path.getmtime(path)
    except OSError:
        pass


def _after_write_update(
    path: str,
    read_file_timestamps: Optional[Dict[str, float]],
) -> None:
    if read_file_timestamps is None:
        return
    try:
        read_file_timestamps[_timestamp_key(path)] = os.path.getmtime(path)
    except OSError:
        pass


def _find_in_context_files(
    path: str, context_files: Dict[str, Any]
) -> Tuple[Optional[str], Optional[str]]:
    """
    Match path against filenames in context_files.

    Returns (abs_path, None) for one match, (None, err) for several, else
    (None, None).
    """
    if not context_files or not path:
        return None, None
    filename = os.path.basename(path)
    matches = []
    for rel_path, file_info in context_files.items():
        if isinstance(file_info, dict):
            context_filename = os.path.basename(rel_path)
            if context_filename == filename:
                abs_path = file_info.get("absolute_path")
                if abs_path and os.path.isfile(abs_path):
                    matches.append(abs_path)
    if len(matches) == 0:
        return None, None
    if len(matches) > 1:
        paths_list = "\n".join(f"  - {p}" for p in matches)
        return None, (
            "Error: Multiple files named '{0}' found in chat context. "
            "Please specify the full path. Found:\n{1}".format(
                filename, paths_list
            )
        )
    return matches[0], None


def _find_in_open_views(path: str, window) -> Tuple[Optional[str], bool]:
    """
    Check if path matches a filename in open views. Prioritizes active view.
    Returns (file_name, is_active_view) if found, (None, False) if not found.
    """
    if not window or not path:
        return None, False
    filename = os.path.basename(path)
    active_view = window.active_view()
    active_match = None
    other_matches = []
    for view in window.views():
        file_name = view.file_name()
        if file_name:
            view_filename = os.path.basename(file_name)
            if view_filename == filename:
                if view == active_view:
                    active_match = file_name
                else:
                    other_matches.append(file_name)
    if active_match:
        return active_match, True
    if other_matches:
        return other_matches[0], False
    return None, False


def resolve_path(
    path: str,
    allowed_roots: List[str],
    context_files: Optional[Dict[str, Any]] = None,
    window=None,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Resolve a path to an absolute path under an allowed root.

    For bare filenames, checks context files and open views first, then
    resolves against allowed_roots.

    Returns (absolute_path, None) on success, (None, err) on failure.
    Rejects traversal and paths outside allowed roots.
    """
    if not path or not isinstance(path, str):
        return None, "Error: Invalid path."

    path = path.strip()
    if not path:
        return None, "Error: Invalid path."

    normalized = os.path.normpath(path)

    if (
        normalized.startswith("..")
        or "/.." in normalized
        or "\\.." in normalized
    ):
        return None, "Error: Path traversal is not allowed."

    # If path is just a filename (no directory), check with priority:
    # 1. Active view (if open)
    # 2. Context files (single match only - error if multiple)
    # 3. Other open views
    # 4. Project folders (normal resolution below)
    if os.path.dirname(normalized) == "" or os.path.dirname(normalized) == ".":
        # Priority 1: Active view
        if window:
            found, is_active = _find_in_open_views(normalized, window)
            if found and is_active:
                if ensure_under_root(found, allowed_roots):
                    try:
                        return os.path.realpath(found), None
                    except OSError:
                        return found, None

        # Priority 2: Context files (error if multiple matches)
        if context_files:
            found, err = _find_in_context_files(normalized, context_files)
            if err:
                return None, err
            if found:
                if ensure_under_root(found, allowed_roots):
                    try:
                        return os.path.realpath(found), None
                    except OSError:
                        return found, None

        # Priority 3: Other open views (non-active)
        if window:
            found, is_active = _find_in_open_views(normalized, window)
            if found and not is_active:
                if ensure_under_root(found, allowed_roots):
                    try:
                        return os.path.realpath(found), None
                    except OSError:
                        return found, None

    # Normal: absolute paths or paths relative to allowed roots.
    if os.path.isabs(normalized):
        try:
            rp = os.path.realpath(normalized)
        except OSError:
            rp = normalized
        for root in allowed_roots:
            try:
                rr = os.path.realpath(root)
                if os.path.commonpath([rr, rp]) == rr:
                    return rp, None
            except (OSError, ValueError):
                continue
        return None, "Error: Path is outside allowed project roots."

    for root in allowed_roots:
        candidate = os.path.normpath(os.path.join(root, normalized))
        try:
            rr = os.path.realpath(root)
            rp = os.path.realpath(candidate)
            if os.path.commonpath([rr, rp]) == rr:
                return rp, None
        except (OSError, ValueError):
            continue

    return None, "Error: Path is outside allowed project roots."


def ensure_under_root(file_path: str, allowed_roots: List[str]) -> bool:
    """
    Return True if ``file_path`` lies under an allowed root (symlink-safe).

    Compares ``os.path.realpath`` of the file and each root so a path under a
    root cannot escape via symlinks.
    """
    try:
        fp = os.path.realpath(file_path)
        for root in allowed_roots:
            try:
                rr = os.path.realpath(root)
                if os.path.commonpath([rr, fp]) == rr:
                    return True
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return False


def execute_view(
    path: str,
    allowed_roots: List[str],
    view_range: Optional[List[int]] = None,
    max_characters: Optional[int] = None,
    context_files: Optional[Dict[str, Any]] = None,
    window=None,
    read_file_timestamps: Optional[Dict[str, float]] = None,
) -> Tuple[str, bool]:
    """
    Execute the view command: read file or list directory.

    Records mtime for regular files when ``read_file_timestamps`` is provided
    so later writes can detect stale content.

    Returns (content_string, is_error).
    """
    resolved, err = resolve_path(
        path, allowed_roots, context_files=context_files, window=window
    )
    if err:
        return err, True

    if resolved is None:
        return "Error: Path resolution failed", True

    if os.path.isdir(resolved):
        try:
            names = sorted(os.listdir(resolved))
            lines = [
                "{0}: {1}".format(i + 1, name) for i, name in enumerate(names)
            ]
            return "\n".join(lines), False
        except OSError as e:
            return "Error: Could not list directory: {0}".format(str(e)), True

    if not os.path.isfile(resolved):
        return "Error: File not found", True

    if _is_ipynb_path(resolved):
        return _IPYNB_NOT_SUPPORTED, True

    try:
        content, _enc = _read_file_with_encoding(resolved)
    except OSError as e:
        return "Error: Could not read file: {0}".format(str(e)), True

    if view_range and isinstance(view_range, list) and len(view_range) >= 2:
        start_line = max(
            1, int(view_range[0]) if view_range[0] is not None else 1
        )
        end_line = view_range[1]
        lines = content.splitlines()
        total = len(lines)
        if end_line == -1:
            end_line = total
        else:
            end_line = min(total, max(1, int(end_line)))
        if start_line > end_line or start_line > total:
            return "Error: Invalid view_range", True
        content = "\n".join(
            "{0}: {1}".format(i, line)
            for i, line in enumerate(
                lines[start_line - 1 : end_line], start=start_line
            )
        )
    else:
        lines = content.splitlines()
        content = "\n".join(
            "{0}: {1}".format(i, line) for i, line in enumerate(lines, start=1)
        )

    if (
        max_characters is not None
        and max_characters > 0
        and len(content) > max_characters
    ):
        content = content[:max_characters] + "\n... (truncated)"

    _record_read(resolved, read_file_timestamps)
    return content, False


def execute_str_replace(
    path: str,
    old_str: str,
    new_str: str,
    allowed_roots: List[str],
    context_files: Optional[Dict[str, Any]] = None,
    window=None,
    read_file_timestamps: Optional[Dict[str, float]] = None,
) -> Tuple[str, bool]:
    """
    Replace old_str with new_str in file exactly once.

    Returns (result_message, is_error).
    """
    resolved, err = resolve_path(
        path, allowed_roots, context_files=context_files, window=window
    )
    if err:
        return err, True

    if resolved is None:
        return "Error: Path resolution failed", True

    if not os.path.isfile(resolved):
        return "Error: File not found", True

    if not ensure_under_root(resolved, allowed_roots):
        return "Error: Path is outside allowed project roots.", True

    if _is_ipynb_path(resolved):
        return _IPYNB_NOT_SUPPORTED, True

    stale = _stale_write_error(resolved, read_file_timestamps)
    if stale:
        return stale, True

    try:
        content, enc = _read_file_with_encoding(resolved)
    except OSError as e:
        return "Error: Could not read file: {0}".format(str(e)), True

    count = content.count(old_str)
    if count == 0:
        return (
            "Error: No match found for replacement. "
            "Please check your text and try again.",
            True,
        )
    if count > 1:
        return (
            "Error: Found {0} matches for replacement text. "
            "Please provide more context to make a unique match.".format(
                count
            ),
            True,
        )

    new_content = content.replace(old_str, new_str, 1)
    try:
        _write_file_with_encoding(resolved, new_content, enc)
    except OSError as e:
        return "Error: Permission denied. Cannot write to file. {0}".format(
            str(e)
        ), True

    _after_write_update(resolved, read_file_timestamps)
    return "Successfully replaced text at exactly one location.", False


def execute_create(
    path: str,
    file_text: str,
    allowed_roots: List[str],
    context_files: Optional[Dict[str, Any]] = None,
    window=None,
) -> Tuple[str, bool]:
    """
    Create a new file with the given content.

    Containment is checked before creating parent directories so we never
    ``makedirs`` outside allowed roots.

    Returns (result_message, is_error).
    """
    resolved, err = resolve_path(
        path, allowed_roots, context_files=context_files, window=window
    )
    if err:
        return err, True

    if resolved is None:
        return "Error: Path resolution failed", True

    if os.path.exists(resolved):
        return "Error: File already exists.", True

    if _is_ipynb_path(resolved):
        return _IPYNB_NOT_SUPPORTED, True

    if not ensure_under_root(resolved, allowed_roots):
        return "Error: Path is outside allowed project roots.", True

    parent = os.path.dirname(resolved)
    if parent:
        if not ensure_under_root(parent, allowed_roots):
            return "Error: Path is outside allowed project roots.", True
        if not os.path.isdir(parent):
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError as e:
                return "Error: Could not create directory: {0}".format(
                    str(e)
                ), True

    if file_text.startswith("\ufeff"):
        file_text = file_text[1:]

    try:
        with open(resolved, "w", encoding="utf-8", newline="") as f:
            f.write(file_text)
    except OSError as e:
        return "Error: Permission denied. Cannot write to file. {0}".format(
            str(e)
        ), True

    return "Successfully created file.", False


def execute_insert(
    path: str,
    insert_line: int,
    insert_text: str,
    allowed_roots: List[str],
    context_files: Optional[Dict[str, Any]] = None,
    window=None,
    read_file_timestamps: Optional[Dict[str, float]] = None,
) -> Tuple[str, bool]:
    """
    Insert text after the given line number (0 = beginning of file).

    Returns (result_message, is_error).
    """
    resolved, err = resolve_path(
        path, allowed_roots, context_files=context_files, window=window
    )
    if err:
        return err, True

    if resolved is None:
        return "Error: Path resolution failed", True

    if not os.path.isfile(resolved):
        return "Error: File not found", True

    if not ensure_under_root(resolved, allowed_roots):
        return "Error: Path is outside allowed project roots.", True

    if _is_ipynb_path(resolved):
        return _IPYNB_NOT_SUPPORTED, True

    stale = _stale_write_error(resolved, read_file_timestamps)
    if stale:
        return stale, True

    try:
        raw, enc = _read_file_with_encoding(resolved)
    except OSError as e:
        return "Error: Could not read file: {0}".format(str(e)), True

    lines = raw.splitlines(keepends=True)
    if not lines and raw:
        lines = [raw]

    insert_line = max(0, int(insert_line))
    if insert_line > len(lines):
        insert_line = len(lines)

    if insert_line == 0:
        new_content = (
            insert_text
            + ("\n" if lines and not lines[0].endswith("\n") else "")
            + "".join(lines)
        )
    else:
        before = lines[:insert_line]
        after = lines[insert_line:]
        new_content = (
            "".join(before)
            + insert_text
            + ("\n" if after and not insert_text.endswith("\n") else "")
            + "".join(after)
        )

    try:
        _write_file_with_encoding(resolved, new_content, enc)
    except OSError as e:
        return "Error: Permission denied. Cannot write to file. {0}".format(
            str(e)
        ), True

    _after_write_update(resolved, read_file_timestamps)
    return "Successfully inserted text.", False


def run_text_editor_tool(
    tool_use_id: str,
    tool_name: str,
    input_params: dict,
    window,
    settings,
    max_characters: Optional[int] = None,
    context_files: Optional[Dict[str, Any]] = None,
    read_file_timestamps: Optional[Dict[str, float]] = None,
) -> dict:
    """
    Execute a single text editor tool call and return a tool_result block.

    input_params must contain command, path, and command-specific fields.

    Returns a dict for API user content, e.g. type tool_result with
    tool_use_id, content, and is_error.

    Args:
        read_file_timestamps: Optional map of realpath -> mtime from successful
            ``view`` calls in this agent loop; required for str_replace/insert
            stale checks. Omitted or None disables those checks.
    """
    allowed_roots = get_allowed_roots(window, settings)
    if not allowed_roots:
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": NO_ALLOWED_ROOTS_MESSAGE,
            "is_error": True,
        }

    command = (input_params or {}).get("command", "")
    path = (input_params or {}).get("path", "")

    if not command:
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": "Error: Missing command.",
            "is_error": True,
        }

    if command == "view":
        view_range = input_params.get("view_range")
        content, is_error = execute_view(
            path,
            allowed_roots,
            view_range=view_range,
            max_characters=max_characters,
            context_files=context_files,
            window=window,
            read_file_timestamps=read_file_timestamps,
        )
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": content,
            "is_error": is_error,
        }

    if command == "str_replace":
        old_str = input_params.get("old_str", "")
        new_str = input_params.get("new_str", "")
        content, is_error = execute_str_replace(
            path,
            old_str,
            new_str,
            allowed_roots,
            context_files=context_files,
            window=window,
            read_file_timestamps=read_file_timestamps,
        )
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": content,
            "is_error": is_error,
        }

    if command == "create":
        file_text = input_params.get("file_text", "")
        content, is_error = execute_create(
            path,
            file_text,
            allowed_roots,
            context_files=context_files,
            window=window,
        )
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": content,
            "is_error": is_error,
        }

    if command == "insert":
        insert_line = input_params.get("insert_line", 0)
        insert_text = input_params.get("insert_text", "")
        content, is_error = execute_insert(
            path,
            insert_line,
            insert_text,
            allowed_roots,
            context_files=context_files,
            window=window,
            read_file_timestamps=read_file_timestamps,
        )
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": content,
            "is_error": is_error,
        }

    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": "Error: Unknown command '{0}'.".format(command),
        "is_error": True,
    }
