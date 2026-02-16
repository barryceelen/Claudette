"""
Text editor tool executor for Anthropic's text editor tool.

Resolves paths against allowed roots (e.g. project folders), runs view/str_replace/create/insert
and returns results for tool_result blocks.
"""

import os
import sublime
from typing import Any, Dict, List, Optional, Tuple


def get_allowed_roots(window, settings) -> List[str]:
    """
    Return list of allowed filesystem roots for the text editor tool.

    Uses window.folders() (project folders), then text_editor_tool_roots setting,
    then fallback to current file's directory or user home.
    """
    roots = []

    if window:
        folders = window.folders()
        if folders:
            roots.extend(os.path.normpath(str(f)) for f in folders)

    extra = settings.get('text_editor_tool_roots') if settings else None
    if extra and isinstance(extra, list):
        for path in extra:
            if path and isinstance(path, str):
                p = os.path.normpath(path.strip())
                if p is not None and os.path.isdir(p) and p not in roots:
                    roots.append(p)

    if not roots and window:
        view = window.active_view()
        if view and view.file_name():
            roots.append(os.path.dirname(view.file_name()))

    if not roots:
        roots.append(os.path.expanduser('~'))

    return roots

def _find_in_context_files(path: str, context_files: Dict[str, Any]) -> Optional[str]:
    """
    Check if path matches a filename in context_files. Returns absolute_path if found.
    """
    if not context_files or not path:
        return None
    filename = os.path.basename(path)
    for rel_path, file_info in context_files.items():
        if isinstance(file_info, dict):
            context_filename = os.path.basename(rel_path)
            if context_filename == filename:
                abs_path = file_info.get('absolute_path')
                if abs_path and os.path.isfile(abs_path):
                    return abs_path
    return None


def _find_in_open_views(path: str, window) -> Optional[str]:
    """
    Check if path matches a filename in open views. Returns file_name() if found.
    """
    if not window or not path:
        return None
    filename = os.path.basename(path)
    for view in window.views():
        file_name = view.file_name()
        if file_name:
            view_filename = os.path.basename(file_name)
            if view_filename == filename:
                return file_name
    return None


def resolve_path(
    path: str,
    allowed_roots: List[str],
    context_files: Optional[Dict[str, Any]] = None,
    window=None,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Resolve a path (relative or absolute) to an absolute path under an allowed root.

    First checks context files and open views if path is just a filename (no directory).
    Then falls back to normal resolution against allowed roots.

    Returns (resolved_absolute_path, None) on success, or (None, error_message) on failure.
    Rejects path traversal and paths outside allowed roots.
    """
    if not path or not isinstance(path, str):
        return None, "Error: Invalid path."

    path = path.strip()
    if not path:
        return None, "Error: Invalid path."

    normalized = os.path.normpath(path)

    if normalized.startswith('..') or '/..' in normalized or '\\..' in normalized:
        return None, "Error: Path traversal is not allowed."

    # If path is just a filename (no directory), check context files and open views first.
    if os.path.dirname(normalized) == '' or os.path.dirname(normalized) == '.':
        if context_files:
            found = _find_in_context_files(normalized, context_files)
            if found:
                # Verify it's under an allowed root.
                if ensure_under_root(found, allowed_roots):
                    return found, None

        if window:
            found = _find_in_open_views(normalized, window)
            if found:
                # Verify it's under an allowed root.
                if ensure_under_root(found, allowed_roots):
                    return found, None

    # Normal resolution: absolute paths or relative paths against allowed roots.
    if os.path.isabs(normalized):
        for root in allowed_roots:
            try:
                if os.path.commonpath([root, normalized]) == root:
                    return normalized, None
            except ValueError:
                continue
        return None, "Error: Path is outside allowed project roots."

    for root in allowed_roots:
        candidate = os.path.normpath(os.path.join(root, normalized))
        try:
            if os.path.commonpath([root, candidate]) == root:
                return candidate, None
        except ValueError:
            continue

    return None, "Error: Path is outside allowed project roots."


def ensure_under_root(file_path: str, allowed_roots: List[str]) -> bool:
    """Return True if file_path is under one of the allowed roots."""
    try:
        for root in allowed_roots:
            if os.path.commonpath([root, file_path]) == root:
                return True
    except ValueError:
        pass
    return False


def execute_view(
    path: str,
    allowed_roots: List[str],
    view_range: Optional[List[int]] = None,
    max_characters: Optional[int] = None,
    context_files: Optional[Dict[str, Any]] = None,
    window=None,
) -> Tuple[str, bool]:
    """
    Execute the view command: read file or list directory.

    Returns (content_string, is_error).
    """
    resolved, err = resolve_path(path, allowed_roots, context_files=context_files, window=window)
    if err:
        return err, True

    if resolved is None:
        return "Error: Path resolution failed", True

    if os.path.isdir(resolved):
        try:
            names = sorted(os.listdir(resolved))
            lines = ["{0}: {1}".format(i + 1, name) for i, name in enumerate(names)]
            return "\n".join(lines), False
        except OSError as e:
            return "Error: Could not list directory: {0}".format(str(e)), True

    if not os.path.isfile(resolved):
        return "Error: File not found", True

    try:
        with open(resolved, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
    except OSError as e:
        return "Error: Could not read file: {0}".format(str(e)), True

    if view_range and isinstance(view_range, list) and len(view_range) >= 2:
        start_line = max(1, int(view_range[0]) if view_range[0] is not None else 1)
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
            for i, line in enumerate(lines[start_line - 1:end_line], start=start_line)
        )
    else:
        lines = content.splitlines()
        content = "\n".join("{0}: {1}".format(i, line) for i, line in enumerate(lines, start=1))

    if max_characters is not None and max_characters > 0 and len(content) > max_characters:
        content = content[:max_characters] + "\n... (truncated)"

    return content, False


def execute_str_replace(
    path: str,
    old_str: str,
    new_str: str,
    allowed_roots: List[str],
    context_files: Optional[Dict[str, Any]] = None,
    window=None,
) -> Tuple[str, bool]:
    """
    Replace old_str with new_str in file exactly once.

    Returns (result_message, is_error).
    """
    resolved, err = resolve_path(path, allowed_roots, context_files=context_files, window=window)
    if err:
        return err, True

    if resolved is None:
        return "Error: Path resolution failed", True

    if not os.path.isfile(resolved):
        return "Error: File not found", True

    if not ensure_under_root(resolved, allowed_roots):
        return "Error: Path is outside allowed project roots.", True

    try:
        with open(resolved, 'r', encoding='utf-8') as f:
            content = f.read()
    except OSError as e:
        return "Error: Could not read file: {0}".format(str(e)), True

    count = content.count(old_str)
    if count == 0:
        return "Error: No match found for replacement. Please check your text and try again.", True
    if count > 1:
        return (
            "Error: Found {0} matches for replacement text. Please provide more context to make a unique match.".format(count),
            True,
        )

    new_content = content.replace(old_str, new_str, 1)
    try:
        with open(resolved, 'w', encoding='utf-8') as f:
            f.write(new_content)
    except OSError as e:
        return "Error: Permission denied. Cannot write to file. {0}".format(str(e)), True

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

    Returns (result_message, is_error).
    """
    resolved, err = resolve_path(path, allowed_roots, context_files=context_files, window=window)
    if err:
        return err, True

    if resolved is None:
        return "Error: Path resolution failed", True

    if os.path.exists(resolved):
        return "Error: File already exists.", True

    parent = os.path.dirname(resolved)
    if parent and not os.path.isdir(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as e:
            return "Error: Could not create directory: {0}".format(str(e)), True

    if not ensure_under_root(resolved, allowed_roots):
        return "Error: Path is outside allowed project roots.", True

    try:
        with open(resolved, 'w', encoding='utf-8') as f:
            f.write(file_text)
    except OSError as e:
        return "Error: Permission denied. Cannot write to file. {0}".format(str(e)), True

    return "Successfully created file.", False


def execute_insert(
    path: str,
    insert_line: int,
    insert_text: str,
    allowed_roots: List[str],
    context_files: Optional[Dict[str, Any]] = None,
    window=None,
) -> Tuple[str, bool]:
    """
    Insert text after the given line number (0 = beginning of file).

    Returns (result_message, is_error).
    """
    resolved, err = resolve_path(path, allowed_roots, context_files=context_files, window=window)
    if err:
        return err, True

    if resolved is None:
        return "Error: Path resolution failed", True

    if not os.path.isfile(resolved):
        return "Error: File not found", True

    if not ensure_under_root(resolved, allowed_roots):
        return "Error: Path is outside allowed project roots.", True

    try:
        with open(resolved, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except OSError as e:
        return "Error: Could not read file: {0}".format(str(e)), True

    insert_line = max(0, int(insert_line))
    if insert_line > len(lines):
        insert_line = len(lines)

    if insert_line == 0:
        new_content = insert_text + ("\n" if lines and not lines[0].endswith("\n") else "") + "".join(lines)
    else:
        before = lines[:insert_line]
        after = lines[insert_line:]
        new_content = "".join(before) + insert_text + ("\n" if after and not insert_text.endswith("\n") else "") + "".join(after)

    try:
        with open(resolved, 'w', encoding='utf-8') as f:
            f.write(new_content)
    except OSError as e:
        return "Error: Permission denied. Cannot write to file. {0}".format(str(e)), True

    return "Successfully inserted text.", False


def run_text_editor_tool(
    tool_use_id: str,
    tool_name: str,
    input_params: dict,
    window,
    settings,
    max_characters: Optional[int] = None,
    context_files: Optional[Dict[str, Any]] = None,
) -> dict:
    """
    Execute a single text editor tool call and return a tool_result block.

    input_params must contain command, path, and any command-specific fields.
    Returns a dict suitable for inclusion in API user message content:
    {"type": "tool_result", "tool_use_id": ..., "content": ..., "is_error": ...}
    """
    allowed_roots = get_allowed_roots(window, settings)
    command = (input_params or {}).get('command', '')
    path = (input_params or {}).get('path', '')

    if not command:
        return {
            'type': 'tool_result',
            'tool_use_id': tool_use_id,
            'content': 'Error: Missing command.',
            'is_error': True,
        }

    if command == 'view':
        view_range = input_params.get('view_range')
        content, is_error = execute_view(
            path,
            allowed_roots,
            view_range=view_range,
            max_characters=max_characters,
            context_files=context_files,
            window=window,
        )
        return {
            'type': 'tool_result',
            'tool_use_id': tool_use_id,
            'content': content,
            'is_error': is_error,
        }

    if command == 'str_replace':
        old_str = input_params.get('old_str', '')
        new_str = input_params.get('new_str', '')
        content, is_error = execute_str_replace(
            path, old_str, new_str, allowed_roots,
            context_files=context_files, window=window
        )
        return {
            'type': 'tool_result',
            'tool_use_id': tool_use_id,
            'content': content,
            'is_error': is_error,
        }

    if command == 'create':
        file_text = input_params.get('file_text', '')
        content, is_error = execute_create(
            path, file_text, allowed_roots,
            context_files=context_files, window=window
        )
        return {
            'type': 'tool_result',
            'tool_use_id': tool_use_id,
            'content': content,
            'is_error': is_error,
        }

    if command == 'insert':
        insert_line = input_params.get('insert_line', 0)
        insert_text = input_params.get('insert_text', '')
        content, is_error = execute_insert(
            path, insert_line, insert_text, allowed_roots,
            context_files=context_files, window=window
        )
        return {
            'type': 'tool_result',
            'tool_use_id': tool_use_id,
            'content': content,
            'is_error': is_error,
        }

    return {
        'type': 'tool_result',
        'tool_use_id': tool_use_id,
        'content': "Error: Unknown command '{0}'.".format(command),
        'is_error': True,
    }
