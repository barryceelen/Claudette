"""
Text editor tool executor for Anthropic's text editor tool.

Resolves paths against allowed roots (e.g. project folders), executes
view/str_replace/create/insert commands, and returns tool_result payloads.
Enforces symlink-safe containment, read-before-write staleness checks,
encoding-preserving round-trips, atomic writes, and .ipynb rejection.
"""

import os
import tempfile
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
    """Return normalized, deduplicated directories from the allowed_tool_roots setting."""
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


def _is_claudette_chat_view(view) -> bool:
    """Return True if ``view`` is a Claudette chat view, not user code."""
    if view is None:
        return False
    try:
        return bool(view.settings().get("claudette_is_chat_view", False))
    except Exception:
        return False


def get_allowed_roots(window, settings) -> List[str]:
    """
    Return list of allowed filesystem roots for the text editor and bash tools.

    Order:
      1. Sidebar folders (``window.folders()``)
      2. ``allowed_tool_roots`` from settings
      3. Fallback when both of the above are empty: directories of all saved
         non-chat views in the window. The active view's directory comes
         first when it qualifies so a single open file keeps behaving
         naturally; other open files are included as well so the user can
         still operate when the chat view has focus (in which case
         ``active_view()`` is the chat view itself, not their source file).
    """
    roots: List[str] = []

    if window:
        folders = window.folders()
        if folders:
            roots.extend(os.path.normpath(str(f)) for f in folders)

    for p in _extra_allowed_roots_from_settings(settings):
        if p not in roots:
            roots.append(p)

    if not roots and window:
        seen = set()

        def add(file_path: str) -> None:
            if not file_path:
                return
            d = os.path.normpath(os.path.dirname(file_path))
            if d and d not in seen:
                seen.add(d)
                roots.append(d)

        active = window.active_view()
        if (
            active
            and active.file_name()
            and not _is_claudette_chat_view(active)
        ):
            add(active.file_name())

        for view in window.views():
            if view is active:
                continue
            if _is_claudette_chat_view(view):
                continue
            fn = view.file_name()
            if fn:
                add(fn)

    return roots


def _is_ipynb_path(resolved: str) -> bool:
    """Return True if the path points to a Jupyter Notebook file."""
    return resolved.lower().endswith(".ipynb")


def _read_file_with_encoding(path: str) -> Tuple[str, str, str]:
    """
    Read file text with a single binary read; detect encoding and line endings.

    Returns (text, encoding, line_ending) where:

    - ``encoding`` is ``"utf-8-sig"`` (BOM present) or ``"utf-8"``.  Raises
      on failure — no silent Latin-1 fallback that could corrupt unknown
      encodings.
    - ``line_ending`` is ``"\\r\\n"`` only when the file is *purely* CRLF
      (every ``\\n`` is preceded by ``\\r``), else ``"\\n"``.  Mixed files
      degrade to LF so the writer does not silently flip their bare LF
      lines to CRLF.  The returned ``text`` is always LF-internal so string
      operations do not trip over stray ``\\r`` on mixed files.
    """
    with open(path, "rb") as f:
        raw = f.read()

    if raw[:3] == b"\xef\xbb\xbf":
        text = raw[3:].decode("utf-8")
        enc = "utf-8-sig"
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise OSError(
                "File is not valid UTF-8. Open it in an editor that "
                "supports its encoding."
            )
        enc = "utf-8"

    # Only treat the file as CRLF when every newline is paired; mixed files
    # (any bare ``\n``) fall back to LF to avoid silently converting them.
    crlf_count = raw.count(b"\r\n")
    if crlf_count > 0 and raw.count(b"\n") == crlf_count:
        line_ending = "\r\n"
    else:
        line_ending = "\n"

    # Normalize to LF internally so downstream string ops see clean lines.
    if b"\r\n" in raw:
        text = text.replace("\r\n", "\n")

    return text, enc, line_ending


def _atomic_write_bytes(path: str, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically via a sibling tempfile + rename.

    The tempfile lives in the same directory so ``os.replace`` is a same-
    filesystem rename. Preserves existing permission bits; new files default
    to ``0o644``. On any error the tempfile is unlinked before re-raising.
    """
    directory = os.path.dirname(path) or "."

    orig_mode: Optional[int] = None
    try:
        orig_mode = os.stat(path).st_mode & 0o777
    except OSError:
        pass

    fd, tmp = tempfile.mkstemp(
        prefix=".claudette-" + os.path.basename(path) + ".",
        dir=directory,
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        target_mode = orig_mode if orig_mode is not None else 0o644
        try:
            os.chmod(tmp, target_mode)
        except OSError:
            pass
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_file_with_encoding(
    path: str, text: str, encoding: str, line_ending: str = "\n"
) -> None:
    """
    Write text back to disk atomically, preserving BOM and line endings.
    """
    # Strip a stray BOM sentinel to avoid emitting a double BOM.
    if text.startswith("\ufeff"):
        text = text[1:]

    if line_ending == "\r\n":
        text = text.replace("\n", "\r\n")

    raw = text.encode("utf-8")
    if encoding == "utf-8-sig":
        raw = b"\xef\xbb\xbf" + raw

    _atomic_write_bytes(path, raw)


def _timestamp_key(path: str) -> str:
    """Return the canonical path used as a key in read_file_timestamps."""
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
    # Exact equality: we store whatever getmtime returned, so a legitimate
    # unchanged file always compares bit-identical.
    if current != read_file_timestamps[key]:
        return (
            "Error: File has been modified since it was read. Read it again "
            "before writing."
        )
    return None


def _record_read(
    path: str,
    read_file_timestamps: Optional[Dict[str, float]],
) -> None:
    """Record the mtime of a successfully read file for later staleness checks."""
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
    """Refresh the stored mtime after a write so the next write isn't falsely stale."""
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

    # After normpath, a traversing relative path starts with "..".
    # Split on both separators to catch Windows-style payloads on Unix.
    first_component = normalized.split("/", 1)[0].split("\\", 1)[0]
    if first_component == "..":
        return None, "Error: Path traversal is not allowed."

    # Bare filename: resolve by priority before falling through to roots.
    #   1. Active view  2. Context files (error if ambiguous)
    #   3. Other open views  4. Allowed roots (below)
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

        # Priority 2: Context files (error if ambiguous)
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

    # Absolute path or relative path — resolve against allowed roots.
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

    if not ensure_under_root(resolved, allowed_roots):
        return "Error: Path is outside allowed project roots.", True

    if os.path.isdir(resolved):
        try:
            with os.scandir(resolved) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as e:
            return "Error: Could not list directory: {0}".format(str(e)), True
        out_lines = []
        for i, entry in enumerate(entries, start=1):
            try:
                is_dir = entry.is_dir()
            except OSError:
                is_dir = False
            suffix = "/" if is_dir else ""
            out_lines.append("{0}: {1}{2}".format(i, entry.name, suffix))
        return "\n".join(out_lines), False

    if not os.path.isfile(resolved):
        return "Error: File not found", True

    if _is_ipynb_path(resolved):
        return _IPYNB_NOT_SUPPORTED, True

    try:
        content, _enc, _le = _read_file_with_encoding(resolved)
    except OSError as e:
        return "Error: Could not read file: {0}".format(str(e)), True

    lines = content.splitlines()
    total = len(lines)

    if view_range and isinstance(view_range, list) and len(view_range) >= 2:
        try:
            start_raw = view_range[0]
            end_raw = view_range[1]
            start_line = 1 if start_raw is None else int(start_raw)
            end_line = -1 if end_raw is None else int(end_raw)
        except (TypeError, ValueError):
            return (
                "Error: Invalid view_range (start and end must be integers).",
                True,
            )
        start_line = max(1, start_line)
        if end_line == -1:
            end_line = total
        else:
            end_line = min(total, max(1, end_line))

        # Empty file with an explicit range: return empty content rather
        # than treating it as an error — viewing an empty file is valid.
        if total == 0:
            _record_read(resolved, read_file_timestamps)
            return "", False

        if start_line > end_line or start_line > total:
            return "Error: Invalid view_range", True

        content = "\n".join(
            "{0}: {1}".format(i, line)
            for i, line in enumerate(
                lines[start_line - 1 : end_line], start=start_line
            )
        )
    else:
        content = "\n".join(
            "{0}: {1}".format(i, line) for i, line in enumerate(lines, start=1)
        )

    if (
        max_characters is not None
        and max_characters > 0
        and len(content) > max_characters
    ):
        # Truncate on a line boundary to avoid splitting a line-number prefix.
        cut = content.rfind("\n", 0, max_characters)
        if cut <= 0:
            cut = max_characters
        content = content[:cut] + "\n... (truncated)"

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
        content, enc, le = _read_file_with_encoding(resolved)
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

    match_index = content.find(old_str)
    match_line = content.count("\n", 0, match_index) + 1 if match_index >= 0 else 0

    new_content = content.replace(old_str, new_str, 1)
    try:
        _write_file_with_encoding(resolved, new_content, enc, le)
    except OSError as e:
        return "Error: Permission denied. Cannot write to file. {0}".format(
            str(e)
        ), True

    _after_write_update(resolved, read_file_timestamps)
    return "Successfully replaced text at line {0}.".format(match_line), False


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

    raw = file_text.encode("utf-8")

    try:
        fd = os.open(resolved, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.close(fd)
    except FileExistsError:
        return "Error: File already exists.", True
    except OSError as e:
        return "Error: Permission denied. Cannot write to file. {0}".format(
            str(e)
        ), True

    try:
        _atomic_write_bytes(resolved, raw)
    except OSError as e:
        try:
            os.unlink(resolved)  # Remove placeholder so no zero-byte file lingers.
        except OSError:
            pass
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
        content, enc, le = _read_file_with_encoding(resolved)
    except OSError as e:
        return "Error: Could not read file: {0}".format(str(e)), True

    lines = content.splitlines(keepends=True)

    try:
        insert_line = int(insert_line)
    except (TypeError, ValueError):
        return (
            "Error: Invalid insert_line (must be an integer).",
            True,
        )
    insert_line = max(0, insert_line)
    if insert_line > len(lines):
        insert_line = len(lines)

    needs_separator = bool(lines[insert_line:]) and not insert_text.endswith("\n")
    before = lines[:insert_line]
    after = lines[insert_line:]
    new_content = (
        "".join(before)
        + insert_text
        + ("\n" if needs_separator else "")
        + "".join(after)
    )

    try:
        _write_file_with_encoding(resolved, new_content, enc, le)
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

    ``input_params`` must contain ``command``, ``path``, and any command-
    specific fields. Returns a ``{"type": "tool_result", ...}`` dict.

    ``read_file_timestamps`` is a realpath -> mtime map populated by ``view``
    calls; when provided, str_replace and insert reject writes to files that
    have changed since they were last read. Pass None to skip those checks.
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
