"""
LLM-backed bash command prefix extraction for "don't ask again" rules.

Before the inline confirmation prompt is shown, a small/fast Haiku model
classifies the command into one of three shapes so the user sees the right
"don't ask again" option:

- ``{"kind": "prefix", "value": "git commit"}`` — we can safely skip approval
  for any command that starts with this prefix (e.g. ``git commit -m "x"``).
- ``{"kind": "full"}`` — the LLM returned ``none`` (no reusable prefix) but the
  command is otherwise safe, so we can offer an exact-match allow for the full
  command string.
- ``{"kind": "none"}`` — command injection was detected, the command is an
  unsafe compound (pipes, subshells, backticks, ``$()``), or the extractor
  call failed; the "don't ask again" option must be hidden.

The extractor fails closed: any network/API/parse error returns
``{"kind": "none"}`` so the user still gets the plain Yes/No prompt and no
allow-rule is silently written. Results are memoized per-command string for
the life of the Sublime process.
"""

import json
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from ..constants import ANTHROPIC_VERSION, PLUGIN_NAME


_SYSTEM_PROMPT = (
    "Your task is to process Bash commands that an AI coding agent wants "
    "to run.\n\n"
    "This policy spec defines how to determine the prefix of a Bash command:"
)


_USER_PROMPT_TEMPLATE = (
    "<policy_spec>\n"
    "# Claudette Bash command prefix detection\n\n"
    "This document defines risk levels for actions that the Claude agent "
    "may take. This classification system is part of a broader safety "
    "framework and is used to determine when additional user confirmation "
    "or oversight may be needed.\n\n"
    "## Definitions\n\n"
    "**Command Injection:** Any technique used that would result in a "
    "command being run other than the detected prefix.\n\n"
    "## Command prefix extraction examples\n"
    "Examples:\n"
    "- cat foo.txt => cat\n"
    "- cd src => cd\n"
    "- cd path/to/files/ => cd\n"
    "- find ./src -type f -name \"*.ts\" => find\n"
    "- gg cat foo.py => gg cat\n"
    "- gg cp foo.py bar.py => gg cp\n"
    "- git commit -m \"foo\" => git commit\n"
    "- git diff HEAD~1 => git diff\n"
    "- git diff --staged => git diff\n"
    "- git diff $(pwd) => command_injection_detected\n"
    "- git status => git status\n"
    "- git status# test(`id`) => command_injection_detected\n"
    "- git status`ls` => command_injection_detected\n"
    "- git push => none\n"
    "- git push origin master => git push\n"
    "- git log -n 5 => git log\n"
    "- git log --oneline -n 5 => git log\n"
    "- grep -A 40 \"from foo.bar.baz import\" alpha/beta/gamma.py => grep\n"
    "- pig tail zerba.log => pig tail\n"
    "- npm test => none\n"
    "- npm test --foo => npm test\n"
    "- npm test -- -f \"foo\" => npm test\n"
    "- pwd\n"
    " curl example.com => command_injection_detected\n"
    "- pytest foo/bar.py => pytest\n"
    "- scalac build => none\n"
    "</policy_spec>\n\n"
    "The user has allowed certain command prefixes to be run, and will "
    "otherwise be asked to approve or deny the command.\n"
    "Your task is to determine the command prefix for the following "
    "command.\n\n"
    "IMPORTANT: Bash commands may run multiple commands that are chained "
    "together.\n"
    "For safety, if the command seems to contain command injection, you "
    "must return \"command_injection_detected\". \n"
    "(This will help protect the user: if they think that they're "
    "allowlisting command A, \n"
    "but the AI coding agent sends a malicious command that technically "
    "has the same prefix as command A, \n"
    "then the safety system will see that you said "
    "\u201ccommand_injection_detected\u201d and ask the user for manual "
    "confirmation.)\n\n"
    "Note that not every command has a prefix. If a command has no "
    "prefix, return \"none\".\n\n"
    "ONLY return the prefix. Do not return any other text, markdown "
    "markers, or other content or formatting.\n\n"
    "Command: {command}\n"
)


_DEFAULT_MODEL = "claude-haiku-4-5"

# Memoize extractor results per raw command string (matches CC's
# ``memoize(_, command => command)``). Protected by a lock because the
# extractor may be called from tool-worker threads concurrently across
# chat views.
_cache = {}
_cache_lock = threading.Lock()


def _is_unsafe_shell_pattern(command: str) -> bool:
    """Cheap check for shell metacharacters that can smuggle extra commands.

    Mirrors ``tools.bash._is_unsafe_shell_pattern`` so we can bail out before
    paying for an API call. Intentionally excludes plain ``&&`` / ``||`` /
    ``;`` lists because those can still be approved as a single exact-match
    allow-rule.
    """
    if "|" in command:
        return True
    if "`" in command:
        return True
    if "$(" in command or "${" in command:
        return True
    return False


def _memo_get(command: str):
    with _cache_lock:
        return _cache.get(command)


def _memo_put(command: str, value: dict) -> None:
    with _cache_lock:
        _cache[command] = value


def _build_ssl_context(verify_ssl: bool):
    if verify_ssl:
        return ssl.create_default_context()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _query_haiku(
    command: str,
    api_key: str,
    base_url: str,
    model: str,
    verify_ssl: bool,
    timeout: float,
) -> Optional[str]:
    """Call the Anthropic Messages API and return the assistant text.

    Returns ``None`` on any error so the caller fails closed.
    """
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    data = {
        "model": model,
        "max_tokens": 512,
        "temperature": 0,
        "system": _SYSTEM_PROMPT,
        "messages": [
            {
                "role": "user",
                "content": _USER_PROMPT_TEMPLATE.format(command=command),
            }
        ],
    }
    try:
        req = urllib.request.Request(
            urllib.parse.urljoin(base_url, "messages"),
            data=json.dumps(data).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        ctx = _build_ssl_context(verify_ssl)
        with urllib.request.urlopen(
            req, context=ctx, timeout=timeout
        ) as response:
            raw = response.read()
        body = json.loads(raw.decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
        print(
            "{0} bash prefix extractor: network error ({1})".format(
                PLUGIN_NAME, e
            )
        )
        return None
    except (ValueError, json.JSONDecodeError) as e:
        print(
            "{0} bash prefix extractor: parse error ({1})".format(
                PLUGIN_NAME, e
            )
        )
        return None

    content = body.get("content") if isinstance(body, dict) else None
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ):
                return block["text"].strip()
    return None


def extract_bash_allow_prefix(
    command: str,
    api_key: str,
    base_url: str,
    model: str = _DEFAULT_MODEL,
    verify_ssl: bool = True,
    timeout: float = 15.0,
) -> dict:
    """Classify ``command`` into a prefix/full/none shape for the allow prompt.

    See the module docstring for the output contract. Uses in-process
    memoization keyed by the raw command string; failures are not cached so a
    transient network error doesn't permanently disable the "don't ask again"
    option for that command.

    Args:
        command: The literal bash command the model wants to run.
        api_key: Anthropic API key (same one the main chat request uses).
        base_url: Anthropic base URL (respects the user's ``base_url`` setting
            so self-hosted gateways keep working).
        model: Small/fast model id. Defaults to ``claude-haiku-4-5`` and can
            be overridden via the ``bash_prefix_extractor_model`` setting.
        verify_ssl: Whether to verify TLS certs; mirrors ``verify_ssl``.
        timeout: Per-request timeout in seconds.
    """
    if not isinstance(command, str) or not command.strip():
        return {"kind": "none", "value": None}

    cached = _memo_get(command)
    if cached is not None:
        return cached

    if _is_unsafe_shell_pattern(command):
        return {"kind": "none", "value": None}

    if not api_key:
        return {"kind": "none", "value": None}

    text = _query_haiku(
        command, api_key, base_url, model, verify_ssl, timeout
    )
    if text is None:
        return {"kind": "none", "value": None}

    prefix = text.strip()
    result: dict
    if prefix == "command_injection_detected":
        result = {"kind": "none", "value": None}
    elif prefix == "none":
        result = {"kind": "full", "value": None}
    elif prefix == "git":
        result = {"kind": "none", "value": None}
    elif not prefix:
        result = {"kind": "none", "value": None}
    else:
        result = {"kind": "prefix", "value": prefix}

    _memo_put(command, result)
    return result
