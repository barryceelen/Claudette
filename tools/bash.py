"""
Bash tool executor for Anthropic's bash tool (persistent session).

Runs model-requested shell commands in a long-lived ``bash`` subprocess and
returns ``tool_result`` payloads for the Messages API. The module centralizes
execution hardening (syntax check, non-interactive stdin, timeouts, child
termination), static policy (banned commands, ``cd`` sandbox), and UX
(confirmation, optional chat echo) so ``api`` stays thin.
"""

import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from typing import List, Optional, Set, Tuple, Union

import sublime


# Default banned first tokens.
_DEFAULT_BANNED_COMMANDS: Set[str] = {
    "alias",
    "curl",
    "curlie",
    "wget",
    "axel",
    "aria2c",
    "nc",
    "telnet",
    "lynx",
    "w3m",
    "links",
    "httpie",
    "xh",
    "http-prompt",
    "chrome",
    "firefox",
    "safari",
}

# Read-only style commands that may skip confirmation when enabled (see settings).
_SAFE_COMMANDS: Set[str] = {
    "git status",
    "git diff",
    "git log",
    "git branch",
    "pwd",
    "tree",
    "date",
    "which",
}


def find_bash_executable() -> Optional[str]:
    """
    Locate a usable bash binary for spawning the persistent shell.

    Tries PATH first, then common Unix locations. Needed because Windows may
    not ship bash; the tool must fail clearly rather than assume /bin/bash.
    """
    path = shutil.which("bash")
    if path:
        return path
    if os.name != "nt":
        for candidate in ("/bin/bash", "/usr/bin/bash"):
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


def _bash_single_quote(path: str) -> str:
    """
    Escape a filesystem path for embedding in single-quoted bash.

    Used when building shell snippets so temp script paths cannot break out of
    quotes or inject extra commands.
    """
    return "'" + path.replace("'", "'\"'\"'") + "'"


def _get_timeout_seconds(settings) -> float:
    """
    Per-command wall-clock limit from settings, clamped to a safe range.

    Prevents hung commands from blocking the agent loop indefinitely and caps
    absurdly high values that would freeze the UI thread polling logic.
    """
    try:
        t = float(settings.get("bash_tool_timeout", 120))
        return max(1.0, min(3600.0, t))
    except (TypeError, ValueError):
        return 120.0


def _get_max_output_bytes(settings) -> int:
    """
    Maximum captured stdout/stderr size from settings, clamped.

    Large command output can exhaust memory or API payload limits; truncation
    keeps responses bounded while still surfacing that output was cut off.
    """
    try:
        n = int(settings.get("bash_tool_max_output_bytes", 100000))
        return max(1024, min(10_000_000, n))
    except (TypeError, ValueError):
        return 100000


def _truncate_output(text: str, max_bytes: int) -> str:
    """
    Cut UTF-8 command output to a byte budget without splitting multibyte chars.

    Applied after the shell returns so we never send megabytes of noise to
    the model or chat; the notice explains truncation to the user.
    """
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


def _realpaths_for_roots(roots: List[str]) -> List[str]:
    """
    Normalize configured root paths to canonical real directories.

    Resolves symlinks and drops missing paths so cd checks and prefix tests
    compare stable absolute paths (avoids bypass via .. or symlink tricks).
    """
    out = []
    for r in roots:
        if not r or not isinstance(r, str):
            continue
        try:
            rp = os.path.realpath(r)
            if os.path.isdir(rp):
                out.append(rp)
        except OSError:
            continue
    return out


def _path_under_allowed_roots(path: str, allowed_real: List[str]) -> bool:
    """
    Return whether ``path`` lies under any allowed root (prefix match on realpath).

    Central check for post-run cwd reset and for ``cd`` validation; using
    ``realpath`` aligns with how users think about containment on disk.
    """
    try:
        rp = os.path.realpath(path)
    except OSError:
        return False
    for root in allowed_real:
        if rp == root or rp.startswith(root + os.sep):
            return True
    return False


def _split_command_segments(command: str) -> List[str]:
    """
    Split a compound shell line on ``;``, ``&&``, and ``||`` outside quotes.

    Used so banned-command and ``cd`` checks run per logical segment (e.g.
    ``cd x && ls``) without pulling in a full shell parser dependency; quote
    handling is intentionally simple but good enough for common model output.
    """
    parts = []
    buf = []
    i = 0
    n = len(command)
    quote = None
    while i < n:
        c = command[i]
        if quote == "'":
            buf.append(c)
            if c == "'":
                quote = None
            i += 1
            continue
        if quote == '"':
            buf.append(c)
            if c == "\\" and i + 1 < n:
                buf.append(command[i + 1])
                i += 2
                continue
            if c == '"':
                quote = None
            i += 1
            continue
        if c in "'\"":
            quote = c
            buf.append(c)
            i += 1
            continue
        if c == ";":
            seg = "".join(buf).strip()
            if seg:
                parts.append(seg)
            buf = []
            i += 1
            continue
        if i + 1 < n and command[i : i + 2] == "&&":
            seg = "".join(buf).strip()
            if seg:
                parts.append(seg)
            buf = []
            i += 2
            continue
        if i + 1 < n and command[i : i + 2] == "||":
            seg = "".join(buf).strip()
            if seg:
                parts.append(seg)
            buf = []
            i += 2
            continue
        buf.append(c)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def _first_executable_token(segment: str) -> str:
    """
    Return the basename of the first token in a segment (for ban list matching).

    Ignores comments and empty segments; uses ``shlex`` so leading env
    assignments still yield the real command name for basename checks.
    """
    s = segment.strip()
    if not s or s.startswith("#"):
        return ""
    try:
        parts = shlex.split(s, posix=True)
    except ValueError:
        return ""
    if not parts:
        return ""
    return os.path.basename(parts[0]).lower()


def _get_banned_set(settings) -> Set[str]:
    """
    Build the set of disallowed command basenames for this session.

    Starts from defaults (network clients, browsers),
    then merges the ``bash_tool_banned_commands_extra`` setting.
    """
    banned = set(_DEFAULT_BANNED_COMMANDS)
    extra = settings.get("bash_tool_banned_commands_extra")
    if extra and isinstance(extra, list):
        for x in extra:
            if isinstance(x, str) and x.strip():
                banned.add(x.strip().lower())
    return banned


def _resolve_cd_target(arg: str, cur_cwd: str) -> str:
    """
    Resolve a ``cd`` argument to an absolute real path using simulated cwd.

    Walks relative targets from ``cur_cwd`` so chained ``cd`` segments in one
    command validate in order; ``expanduser`` handles ``~`` in the argument.
    """
    expanded = os.path.expanduser(arg.strip(" \t\"'"))
    if os.path.isabs(expanded):
        return os.path.realpath(expanded)
    return os.path.realpath(os.path.join(cur_cwd, expanded))


def _validate_cd_and_bans(
    command: str,
    start_cwd: str,
    allowed_real: List[str],
    restrict_initial_only: bool,
    initial_cwd_real: str,
    banned: Set[str],
) -> Optional[str]:
    """
    Reject banned first tokens and illegal ``cd`` targets before execution.

    Simulates directory changes across segments so ``cd a && cd b`` validates
    ``b`` from the right cwd. When ``restrict_initial_only`` is true, only the
    initial session directory tree is allowed; otherwise the full allowed root
    union applies.

    Returns:
        Error message string, or ``None`` if the command passes static checks.
    """
    simulated = start_cwd
    segments = _split_command_segments(command)
    if not segments:
        segments = [command.strip()]
    for seg in segments:
        if not seg.strip() or seg.strip().startswith("#"):
            continue
        try:
            parts = shlex.split(seg, posix=True)
        except ValueError:
            return "Error: Invalid shell quoting in command segment."
        if not parts:
            continue
        base = os.path.basename(parts[0]).lower()
        if base in banned:
            return (
                "Error: Command '{0}' is not allowed for security reasons.".format(
                    base
                )
            )
        if parts[0] == "cd" or base == "cd":
            roots = (
                [initial_cwd_real]
                if restrict_initial_only
                else allowed_real
            )
            if len(parts) < 2:
                new_home = os.path.realpath(os.path.expanduser("~"))
                if not _path_under_allowed_roots(new_home, roots):
                    return (
                        "Error: cd without argument targets a directory outside "
                        "allowed roots."
                    )
                simulated = new_home
            else:
                target = _resolve_cd_target(parts[1], simulated)
                if not _path_under_allowed_roots(target, roots):
                    return (
                        "Error: cd to '{0}' was blocked. For security, the "
                        "shell may only use directories under allowed "
                        "roots.".format(target)
                    )
                simulated = target
    return None


def _is_unsafe_shell_pattern(command: str) -> bool:
    """
    Detect patterns where allowlist / safe-shortcut skips are unsafe.

    Pipes and shell expansions can smuggle extra commands; we still run the
    command if the user confirms, but we never auto-skip the dialog for these
    strings even when a prefix or safe-command would otherwise match.
    """
    if "|" in command:
        return True
    if "`" in command:
        return True
    if "$(" in command or "${" in command:
        return True
    return False


def _normalize_cmd_key(command: str) -> str:
    """
    Collapse internal whitespace for exact-match allowlist and safe-command sets.

    Makes ``git  status`` and ``git status`` compare equal so small model
    formatting differences do not bypass user-configured shortcuts.
    """
    return " ".join(command.split())


def _command_matches_allowlist(command: str, settings) -> bool:
    """
    Return whether the command is covered by exact or prefix allowlist settings.

    Used to skip the confirmation dialog for trusted command shapes; unsafe
    patterns are handled separately in ``_should_prompt_for_command``.
    """
    exact = settings.get("bash_tool_allow_exact")
    if exact and isinstance(exact, list):
        n = _normalize_cmd_key(command)
        for x in exact:
            if isinstance(x, str) and _normalize_cmd_key(x) == n:
                return True
    prefixes = settings.get("bash_tool_allow_prefix")
    if prefixes and isinstance(prefixes, list):
        n = command.strip()
        for p in prefixes:
            if isinstance(p, str) and p.strip():
                pre = p.strip()
                if n.startswith(pre) or n.startswith(pre + " "):
                    return True
    return False


def _command_matches_safe_shortcut(command: str, settings) -> bool:
    """
    Return whether a built-in read-only command may skip confirmation.

    Only applies when ``bash_tool_allow_safe_commands`` is enabled
    in settings.
    """
    if not settings.get("bash_tool_allow_safe_commands", False):
        return False
    return _normalize_cmd_key(command) in _SAFE_COMMANDS


def _kill_shell_children(proc: Optional[subprocess.Popen]) -> None:
    """
    Terminate child PIDs of the interactive bash process (Unix only).

    On timeout the parent shell may still be waiting while a child runs
    so ``sleep 999`` or similar cannot ignore the session timeout.
    """
    if proc is None or proc.poll() is not None:
        return
    if os.name == "nt":
        return
    pid = proc.pid
    try:
        out = subprocess.run(
            ["pgrep", "-P", str(pid)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=3,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return
    for line in (out.stdout or "").strip().split("\n"):
        line = line.strip()
        if line.isdigit():
            try:
                os.kill(int(line), signal.SIGTERM)
            except (OSError, ValueError):
                pass


def _run_bash_syntax_check(bash_exe: str, script_path: str) -> Optional[str]:
    """
    Run ``bash -n`` on the temp script before sourcing it in the live session.

    Catches parse errors early without mutating shell state; failures return a
    user-facing error string for the tool_result payload.
    """
    try:
        r = subprocess.run(
            [bash_exe, "-n", script_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return "Error: Could not validate shell syntax ({0}).".format(str(e))
    if r.returncode == 0:
        return None
    err = (r.stderr or r.stdout or "").strip() or "syntax error"
    return "Error: Shell syntax check failed: {0}".format(err[:2000])


def _parse_marker_block(data: str, marker: str) -> Optional[Tuple[str, int, str]]:
    """
    Parse stdout buffer for the unique marker, exit code, and post-command pwd.

    The marker protocol lets us delimit one command's output in a persistent
    shell; pwd is used to detect cwd escape and to update simulated cwd for
    the next validation pass.
    """
    needle = "__CTTE_BASH_{0}__".format(marker)
    pos = data.find(needle)
    if pos == -1:
        return None
    rest_start = pos + len(needle)
    if rest_start < len(data) and data[rest_start] == "\n":
        rest_start += 1
    rest = data[rest_start:]
    lines = rest.split("\n", 2)
    if len(lines) < 2:
        return None
    exit_str = lines[0].strip()
    pwd_line = lines[1].strip() if len(lines) > 1 else ""
    try:
        exit_code = int(exit_str)
    except ValueError:
        return None
    output = data[:pos].rstrip("\n")
    return output, exit_code, pwd_line


class BashSession:
    """
    Long-lived ``bash`` subprocess with serialized command execution.

    Commands are written as temp scripts and sourced so state persists across
    tool calls (cwd, env). Security hooks (syntax, stdin null, bans, cwd reset)
    wrap each invocation; ``allowed_roots`` must match ``get_allowed_roots``.
    """

    def __init__(self, cwd: str, settings, allowed_roots: Optional[List[str]] = None):
        """
        Start a bash session at ``cwd`` with security metadata from ``settings``.

        ``allowed_roots`` should be the same list as ``get_allowed_roots`` so
        bash and file tools agree on filesystem scope; when empty, falls back
        to the session cwd only.
        """
        self._cwd = cwd
        self._settings = settings
        self._bash_exe = find_bash_executable()
        try:
            self._original_cwd_real = os.path.realpath(cwd)
        except OSError:
            self._original_cwd_real = os.path.realpath(os.path.expanduser("~"))
        roots_in = allowed_roots if allowed_roots else [self._original_cwd_real]
        self._allowed_roots_real = _realpaths_for_roots(roots_in)
        if not self._allowed_roots_real:
            self._allowed_roots_real = [self._original_cwd_real]
        self._current_cwd = self._original_cwd_real
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._buf = ""
        self._buf_lock = threading.Lock()
        self._start_process()

    @property
    def bash_available(self) -> bool:
        """True if a bash binary was found and the session may run commands."""
        return self._bash_exe is not None

    @property
    def original_cwd(self) -> str:
        """Real path of the directory the session was created in (reset target)."""
        return self._original_cwd_real

    def _close_process(self) -> None:
        """
        Stop the bash process, close stdin, kill if needed, join reader thread.

        Ensures no zombie reader thread or open pipes when restarting after
        timeout or fatal shell errors.
        """
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
        """
        Background thread: read merged stdout/stderr into ``self._buf``.

        Runs for the lifetime of the process; coexists with the main thread
        that polls ``_buf`` for marker completion so stdin writes never block on
        a full pipe buffer.
        """
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
        """
        Spawn a fresh non-login ``bash`` with cwd ``self._cwd`` and start reader.

        Called on construction and after restarts; clears the output buffer so
        stale marker lines cannot confuse the next command's parser.
        """
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
        """Public shutdown: kill shell, join reader, discard buffered output."""
        self._close_process()
        with self._buf_lock:
            self._buf = ""

    def restart(self) -> None:
        """
        Replace the subprocess while keeping configured cwd and root metadata.

        Resets simulated cwd to ``self._cwd`` so validation matches a clean shell
        after crashes, timeouts, or explicit model ``restart`` tool input.
        """
        try:
            self._current_cwd = os.path.realpath(self._cwd)
        except OSError:
            self._current_cwd = self._original_cwd_real
        self._start_process()

    def validate_command(self, command: str) -> Optional[str]:
        """
        Run ban-list and ``cd`` sandbox checks without executing the command.

        Exposed so ``run_bash_tool`` can reject bad commands before showing the
        confirmation dialog; respects ``bash_tool_restrict_to_initial_root_only``.
        """
        settings = self._settings
        restrict = bool(
            settings.get("bash_tool_restrict_to_initial_root_only", True)
        )
        allowed = (
            [self._original_cwd_real]
            if restrict
            else self._allowed_roots_real
        )
        return _validate_cd_and_bans(
            command,
            self._current_cwd,
            allowed,
            restrict,
            self._original_cwd_real,
            _get_banned_set(settings),
        )

    def execute_command(
        self,
        command: str,
        skip_validation: bool = False,
        skip_cwd_reset: bool = False,
    ) -> Tuple[str, bool]:
        """
        Execute one command in the persistent shell and return captured output.

        Writes the command to a temp script, runs ``bash -n``, sources it with
        stdin from ``/dev/null``, then polls for a unique marker with exit code
        and pwd. On timeout, SIGTERMs child processes and restarts the shell.
        ``skip_validation`` is used for internal ``cd`` resets after cwd escape;
        ``skip_cwd_reset`` prevents recursive reset when fixing cwd.
        """
        if not skip_validation:
            err = self.validate_command(command)
            if err:
                return err, True

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

        syn_err = _run_bash_syntax_check(self._bash_exe, script_path)
        if syn_err:
            try:
                os.unlink(script_path)
            except OSError:
                pass
            return syn_err, True

        path_q = _bash_single_quote(script_path)
        wrapped = (
            "{{ . {path}; }} < /dev/null 2>&1; ec=$?; "
            "pwd_line=$(pwd); rm -f {path}; "
            "printf '\\n__CTTE_BASH_{marker}__\\n'; "
            "printf '%s\\n' \"$ec\"; "
            "printf '%s\\n' \"$pwd_line\"\n"
        ).format(path=path_q, marker=marker)

        with self._buf_lock:
            self._buf = ""

        proc = self._proc
        stdin = proc.stdin
        if stdin is None:
            try:
                os.unlink(script_path)
            except OSError:
                pass
            return "Error: Bash stdin is not available.", True
        try:
            stdin.write(wrapped.encode("utf-8"))
            stdin.flush()
        except (OSError, BrokenPipeError, ValueError) as e:
            try:
                os.unlink(script_path)
            except OSError:
                pass
            self.restart()
            return (
                "Error: Lost connection to bash ({0}). Session restarted.".format(
                    str(e)
                ),
                True,
            )

        deadline = time.monotonic() + timeout
        found = False
        parsed: Optional[Tuple[str, int, str]] = None
        while time.monotonic() < deadline:
            with self._buf_lock:
                data = self._buf
            if "__CTTE_BASH_{0}__".format(marker) in data:
                parsed = _parse_marker_block(data, marker)
                if parsed:
                    found = True
                    break
            time.sleep(0.05)

        if not found:
            try:
                os.unlink(script_path)
            except OSError:
                pass
            _kill_shell_children(self._proc)
            self._close_process()
            self._start_process()
            return (
                "Error: Command timed out after {0} seconds. "
                "Bash session was restarted.".format(int(timeout)),
                True,
            )

        assert parsed is not None
        output, exit_code, pwd_after = parsed

        try:
            os.unlink(script_path)
        except OSError:
            pass

        extra = ""
        roots_check = self._allowed_roots_real
        if self._settings.get("bash_tool_restrict_to_initial_root_only", True):
            roots_check = [self._original_cwd_real]
        if (
            not skip_cwd_reset
            and pwd_after
            and not _path_under_allowed_roots(pwd_after, roots_check)
        ):
            reset_target = self._original_cwd_real
            if _path_under_allowed_roots(reset_target, roots_check):
                inner = "cd {0}".format(_bash_single_quote(reset_target))
                self.execute_command(
                    inner,
                    skip_validation=True,
                    skip_cwd_reset=True,
                )
                try:
                    self._current_cwd = os.path.realpath(reset_target)
                except OSError:
                    self._current_cwd = reset_target
                extra = "\n\nShell cwd was outside allowed roots; reset to {0}.".format(
                    reset_target
                )
            else:
                extra = (
                    "\n\nWarning: shell cwd ({0}) is outside allowed roots; "
                    "could not reset automatically.".format(pwd_after)
                )
        else:
            try:
                if pwd_after:
                    self._current_cwd = os.path.realpath(pwd_after)
            except OSError:
                pass

        output = output + extra
        output = _truncate_output(output, max_out)
        is_error = exit_code != 0
        if is_error and not output.strip():
            output = "Exit code {0}".format(exit_code)
        return output, is_error


def _ask_permission_sync(command: str, cwd: str) -> bool:
    """
    Show Sublime's OK/Cancel dialog for one command (main thread only).

    Sublime UI must run on the main thread; truncates long commands and shows
    cwd so the user knows where the shell will run. Returns whether Run was
    chosen.
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
    Marshal the permission dialog onto the main thread from API worker threads.

    The non-streaming tool loop runs off the UI thread; ``set_timeout`` plus an
    ``Event`` bridges to ``_ask_permission_sync``. ``None`` means the wait
    exceeded one hour (treated as failure upstream).
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
    Optionally echo the shell command into the chat transcript as markdown.

    Dedupes with ``seen`` because the model sometimes emits duplicate bash
    tool_use blocks in one assistant message; must schedule UI append on the
    main thread like other chat mutations.
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


def _should_prompt_for_command(command: str, settings) -> bool:
    """
    Decide if the confirmation dialog is required for this command string.

    When ``bash_tool_confirm`` is on, safe shortcuts and allowlists can skip
    the prompt unless ``_is_unsafe_shell_pattern`` forces a manual check.
    """
    if _command_matches_safe_shortcut(command, settings):
        return False
    if _command_matches_allowlist(command, settings):
        if _is_unsafe_shell_pattern(command):
            return True
        return False
    return True


def run_bash_tool(
    tool_use_id: str,
    input_params: dict,
    session: BashSession,
    chat_view=None,
    chat_echo_seen: Optional[Set[str]] = None,
) -> dict:
    """
    Full handler for one Anthropic ``bash`` tool_use block.

    Validates before prompting (fail fast), optionally asks the user, echoes to
    chat if configured, then runs via ``execute_command`` with validation
    skipped to avoid duplicate work. Handles ``restart`` without executing a
    command.

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

    pre_err = session.validate_command(command)
    if pre_err:
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": pre_err,
            "is_error": True,
        }

    settings = session._settings
    if settings.get("bash_tool_confirm", True):
        if _should_prompt_for_command(command, settings):
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

    text, is_error = session.execute_command(command, skip_validation=True)
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": text,
        "is_error": is_error,
    }


def initial_bash_cwd(window, settings) -> Optional[str]:
    """
    Pick the starting cwd for a new bash session (first entry from ``get_allowed_roots``).

    Returns ``None`` when there are no sidebar folders, no ``allowed_tool_roots``,
    and no saved active file path so the API layer can refuse to start bash
    instead of falling back to an overly broad directory like home.
    """
    from .text_editor import get_allowed_roots

    roots = get_allowed_roots(window, settings)
    if roots:
        return roots[0]
    return None
