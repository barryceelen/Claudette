"""
Bash tool executor for Anthropic's bash tool (persistent session).

Runs commands in a long-lived bash subprocess and returns tool_result payloads.
"""

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from typing import Optional, Set, Tuple, Union

import sublime


def find_bash_executable() -> Optional[str]:
    """Return path to bash, or None if not available."""
    path = shutil.which("bash")
    if path:
        return path
    if os.name != "nt":
        for candidate in ("/bin/bash", "/usr/bin/bash"):
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


def _bash_single_quote(path: str) -> str:
    """Return path safe for single-quoted bash context."""
    return "'" + path.replace("'", "'\"'\"'") + "'"


def _get_timeout_seconds(settings) -> float:
    try:
        t = float(settings.get("bash_tool_timeout", 120))
        return max(1.0, min(3600.0, t))
    except (TypeError, ValueError):
        return 120.0


def _get_max_output_bytes(settings) -> int:
    try:
        n = int(settings.get("bash_tool_max_output_bytes", 100000))
        return max(1024, min(10_000_000, n))
    except (TypeError, ValueError):
        return 100000


def _truncate_output(text: str, max_bytes: int) -> str:
    """Truncate UTF-8 text to max_bytes with a notice."""
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= max_bytes:
        return text
    cut = raw[: max_bytes - 1]
    while cut and (cut[-1] & 0xC0) == 0x80:
        cut = cut[:-1]
    truncated = cut.decode("utf-8", errors="replace")
    return (
        truncated
        + "\n\n... Output truncated ({0} bytes total, limit {1}) ...".format(
            len(raw), max_bytes
        )
    )


class BashSession:
    """Persistent bash subprocess for Claude bash tool calls."""

    def __init__(self, cwd: str, settings):
        self._cwd = cwd
        self._settings = settings
        self._bash_exe = find_bash_executable()
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._buf = ""
        self._buf_lock = threading.Lock()
        self._start_process()

    @property
    def bash_available(self) -> bool:
        return self._bash_exe is not None

    def _close_process(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except (OSError, BrokenPipeError, ValueError):
            pass
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if self._reader is not None:
            self._reader.join(timeout=3)
            self._reader = None

    def _read_loop(self, proc: subprocess.Popen) -> None:
        stdout = proc.stdout
        if stdout is None:
            return
        while True:
            try:
                chunk = stdout.read(4096)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            if isinstance(chunk, bytes):
                chunk = chunk.decode("utf-8", errors="replace")
            with self._buf_lock:
                self._buf += chunk

    def _start_process(self) -> None:
        self._close_process()
        if not self._bash_exe:
            return
        self._buf = ""
        try:
            self._proc = subprocess.Popen(
                [self._bash_exe],
                cwd=self._cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=False,
                bufsize=0,
                env=os.environ.copy(),
            )
        except OSError:
            self._proc = None
            return
        proc = self._proc
        self._reader = threading.Thread(
            target=self._read_loop,
            args=(proc,),
            daemon=True,
        )
        self._reader.start()

    def close(self) -> None:
        """Terminate bash and join the reader thread."""
        self._close_process()
        with self._buf_lock:
            self._buf = ""

    def restart(self) -> None:
        """Start a new bash process at the same initial cwd."""
        self._start_process()

    def execute_command(self, command: str) -> Tuple[str, bool]:
        """
        Run one command in the session. Returns (output_text, is_error).

        On timeout or dead shell, the session is restarted when possible.
        """
        if not self._bash_exe:
            return (
                "Error: bash was not found on PATH. Install bash or add it to "
                "PATH (e.g. Git Bash on Windows).",
                True,
            )
        if not self._proc or self._proc.poll() is not None:
            self._start_process()
        if not self._proc or self._proc.poll() is not None:
            return "Error: Could not start bash session.", True

        timeout = _get_timeout_seconds(self._settings)
        max_out = _get_max_output_bytes(self._settings)

        marker = uuid.uuid4().hex
        marker_line_re = re.compile(
            r"^__CTTE_BASH_{0}__ ([0-9]+)\s*$".format(re.escape(marker)),
            re.MULTILINE,
        )

        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".sh",
                delete=False,
            ) as tf:
                tf.write(command)
                script_path = tf.name
        except OSError as e:
            return "Error: Could not write command script: {0}".format(str(e)), True

        path_q = _bash_single_quote(script_path)
        wrapped = (
            "{{ . {path}; }} 2>&1; ec=$?; rm -f {path}; "
            "printf '\\n__CTTE_BASH_{marker}__ %s\\n' \"$ec\"\n"
        ).format(path=path_q, marker=marker)

        with self._buf_lock:
            self._buf = ""

        try:
            self._proc.stdin.write(wrapped.encode("utf-8"))
            self._proc.stdin.flush()
        except (OSError, BrokenPipeError, ValueError) as e:
            os.unlink(script_path)
            self.restart()
            return (
                "Error: Lost connection to bash ({0}). Session restarted.".format(
                    str(e)
                ),
                True,
            )

        deadline = time.monotonic() + timeout
        found = False
        while time.monotonic() < deadline:
            with self._buf_lock:
                data = self._buf
            if "__CTTE_BASH_{0}__".format(marker) in data:
                m = marker_line_re.search(data)
                if m:
                    found = True
                    exit_code = int(m.group(1))
                    before = data[: m.start()].rstrip("\n")
                    output = before
                    break
            time.sleep(0.05)

        if not found:
            try:
                os.unlink(script_path)
            except OSError:
                pass
            self._close_process()
            self._start_process()
            return (
                "Error: Command timed out after {0} seconds. "
                "Bash session was restarted.".format(int(timeout)),
                True,
            )

        try:
            os.unlink(script_path)
        except OSError:
            pass

        output = _truncate_output(output, max_out)
        is_error = exit_code != 0
        if is_error and not output.strip():
            output = "Exit code {0}".format(exit_code)
        return output, is_error


def _ask_permission_sync(command: str, cwd: str) -> bool:
    """
    Ask user for permission to run a bash command.

    Must be called from main thread. Returns True if user approves.
    """
    display_cmd = command
    if len(display_cmd) > 500:
        display_cmd = display_cmd[:497] + "..."

    cwd_line = ""
    if cwd:
        cwd_display = cwd
        if len(cwd_display) > 120:
            cwd_display = "…" + cwd_display[-116:]
        cwd_line = "\nWorking directory:\n{0}\n".format(cwd_display)

    return sublime.ok_cancel_dialog(
        "Claude wants to run this command:{0}\n{1}\n\nAllow?".format(
            cwd_line, display_cmd
        ),
        "Run",
    )


def _ask_permission(command: str, cwd: str) -> Union[bool, None]:
    """
    Ask user for permission (thread-safe).

    Blocks until user responds. Safe to call from background thread.
    Returns True if allowed, False if denied, None if the wait timed out.
    """
    result = [False]
    event = threading.Event()

    def ask_on_main():
        result[0] = _ask_permission_sync(command, cwd)
        event.set()

    sublime.set_timeout(ask_on_main, 0)
    if not event.wait(timeout=3600):
        return None
    return result[0]


def _display_command_in_chat(
    chat_view,
    command: str,
    seen: Optional[Set[str]] = None,
) -> None:
    """
    Display the bash command in the chat view.

    If seen is provided, skip when command is already in seen (same assistant
    message often contains duplicate tool_use blocks for the same shell line).
    """
    if chat_view is None:
        return
    if seen is not None:
        if command in seen:
            return
        seen.add(command)

    def append_on_main():
        text = "**Running command:**\n\n```bash\n{0}\n```\n\n".format(command)
        chat_view.append_text(text)

    sublime.set_timeout(append_on_main, 0)


def run_bash_tool(
    tool_use_id: str,
    input_params: dict,
    session: BashSession,
    chat_view=None,
    chat_echo_seen: Optional[Set[str]] = None,
) -> dict:
    """
    Handle one bash tool_use and return a tool_result block.

    Args:
        tool_use_id: API tool use id.
        input_params: Model input (command, and/or restart).
        session: Active BashSession for this agent turn.
        chat_view: Optional ClaudetteChatView for displaying commands.
        chat_echo_seen: When echo-in-chat is on, dedupe keys for this assistant
            tool_use batch (same set for all bash calls in one response).

    Returns:
        Dict suitable for API user message content (tool_result).
    """
    inp = input_params or {}

    if inp.get("restart"):
        session.restart()
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": "Bash session restarted",
        }

    command = inp.get("command")
    if command is None or (
        isinstance(command, str) and not command.strip()
    ):
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": "Error: Missing command.",
            "is_error": True,
        }

    if not isinstance(command, str):
        command = str(command)

    settings = session._settings
    if settings.get("bash_tool_confirm", True):
        decision = _ask_permission(command, session._cwd)
        if decision is None:
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": (
                    "Error: Confirmation dialog did not complete (timed out "
                    "after 1 hour)."
                ),
                "is_error": True,
            }
        if not decision:
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": "Command execution denied by user.",
                "is_error": True,
            }

    if settings.get("bash_tool_echo_in_chat", False):
        _display_command_in_chat(chat_view, command, seen=chat_echo_seen)

    text, is_error = session.execute_command(command)
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": text,
        "is_error": is_error,
    }


def initial_bash_cwd(window, settings) -> str:
    """Working directory for a new bash session (first project folder or home)."""
    from .text_editor import get_allowed_roots

    roots = get_allowed_roots(window, settings)
    if roots:
        return roots[0]
    return os.path.expanduser("~")
