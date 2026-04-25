"""Tool definition builders for Claude API requests.

Builds the JSON tool config dicts that go into the API request body.
This is tool *configuration*, not tool *execution* — execution lives in tools/.
"""

import urllib.parse

from typing import Dict, List, Optional, Set, Tuple

# Only render links pointing at plain web URLs. ``javascript:``, ``data:``,
# ``file:``, etc. are silently dropped — both to avoid confusing the user
# with scheme-prefixed links they cannot click and to shut the door on
# result-title spoofing that hides a scary scheme behind a friendly label.
_ALLOWED_URL_SCHEMES = ("http://", "https://")


def _escape_md_url(url: str) -> str:
    """Escape markdown-link destination metacharacters in a URL.

    ``[text](url)`` treats ``)`` as the end of the destination unless the
    character is escaped. ``\\`` is also special in markdown link parsing.
    Escaping both keeps the URL inside the destination without altering the
    underlying target.
    """
    return url.replace("\\", "\\\\").replace(")", "\\)")


def _escape_md_text(s: str) -> str:
    """Neutralize link-breaking characters in attacker-controlled titles.

    Search result / fetch titles are rendered inside ``[ ... ](url)``.
    A title containing a stray ``]`` can break out of the link and
    forge prose in the assistant's reply. Newlines split the link
    across lines. Backticks confuse Sublime's markdown highlighter.
    Escaping these characters keeps malicious titles inert.
    """
    if not isinstance(s, str):
        return ""
    return (
        s.replace("\\", "\\\\")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("`", "\\`")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def format_md_link(title, url) -> Optional[str]:
    """Build a ``[title](url)`` string with title escaping + URL gating.

    Returns ``None`` when the URL is missing, non-string, uses a scheme
    outside the http/https allowlist, or contains characters that would
    tear the markdown ``[text](url)`` structure (whitespace, CR/LF,
    angle brackets, quotes). Well-formed http(s) URLs percent-encode
    all of those, so rejecting them is safe. Callers should treat
    ``None`` as "render nothing".
    """
    if not isinstance(url, str):
        return None
    u = url.strip()
    if not u:
        return None
    lowered = u.lower()
    if not any(lowered.startswith(s) for s in _ALLOWED_URL_SCHEMES):
        return None
    if any(c in u for c in " \t\r\n<>\"'"):
        return None
    raw_title = (
        title.strip()
        if isinstance(title, str) and title.strip()
        else u
    )
    t = _escape_md_text(raw_title) or u
    return "[{0}]({1})".format(t, _escape_md_url(u))


# Cache of (tool, setting_key, entry) tuples we've already warned about.
# Prevents every request from re-spamming the chat when the user's domain
# lists are misconfigured. Resets implicitly at process start. The
# sentinel ``"__conflict__"`` is used for the "both lists set" warning.
_warned_domain_issues: Set[Tuple[str, str, str]] = set()


def _domain_issue(entry: str) -> Optional[str]:
    """Return a reason string if ``entry`` is obviously non-matching.

    Anthropic's ``allowed_domains`` / ``blocked_domains`` take bare
    hosts (e.g. ``example.com``, ``docs.example.com``). Entries with a
    scheme, path component, wildcard, credentials, or embedded
    whitespace silently fail to match server-side — so we drop them
    locally and surface a one-time explanation to the user instead of
    letting them wonder why their filter has no effect.
    """
    if "://" in entry:
        return "remove the scheme (http://, https://)"
    if "/" in entry:
        return "remove the path component (use bare host only)"
    if "*" in entry:
        return "wildcards are not supported"
    if "@" in entry:
        return "remove credentials (user@host)"
    if any(c.isspace() for c in entry):
        return "contains whitespace"
    if ":" in entry:
        return "remove the port (use bare host only)"
    return None


def _clean_domain_list(
    value,
) -> Tuple[List[str], List[Tuple[str, str]]]:
    """Normalize a domain-list setting into ``(clean, rejected)``.

    Drops non-string entries and values that are empty after trimming,
    lowercases hosts for deduplication and API payloads (domains are
    case-insensitive), collapses duplicates, and preserves original order
    of first occurrence. Entries flagged by ``_domain_issue`` are kept out
    of the clean list and returned alongside so the caller can explain
    the rejection to the user
    (e.g. "ignored 'https://example.com' — remove the scheme"). A
    missing or malformed setting yields two empty lists.
    """
    if not isinstance(value, list):
        return [], []
    seen: Set[str] = set()
    out: List[str] = []
    rejected: List[Tuple[str, str]] = []
    for d in value:
        if not isinstance(d, str):
            continue
        s = d.strip()
        if not s:
            continue
        key = s.lower()
        if key in seen:
            continue
        reason = _domain_issue(s)
        if reason:
            rejected.append((s, reason))
            continue
        seen.add(key)
        out.append(key)
    return out, rejected


def check_domain_list_issues(settings, tool_label: str) -> List[str]:
    """Return human-readable warnings about domain-list misconfiguration.

    Covers both per-entry rejections (scheme/path/wildcard/etc.) and
    the mutual-exclusion conflict between ``{tool}_allowed_domains``
    and ``{tool}_blocked_domains``. Each distinct issue is emitted at
    most once per process via ``_warned_domain_issues`` so the chat
    doesn't fill up with duplicates on repeat requests. Callers are
    responsible for routing the returned strings to the user (chat
    status message preferred, console as a last resort).
    """
    allowed_key = "{0}_allowed_domains".format(tool_label)
    blocked_key = "{0}_blocked_domains".format(tool_label)
    allowed, allowed_bad = _clean_domain_list(settings.get(allowed_key))
    blocked, blocked_bad = _clean_domain_list(settings.get(blocked_key))

    messages: List[str] = []
    for key, bad in (
        (allowed_key, allowed_bad),
        (blocked_key, blocked_bad),
    ):
        for entry, reason in bad:
            sig = (tool_label, key, entry)
            if sig in _warned_domain_issues:
                continue
            _warned_domain_issues.add(sig)
            messages.append(
                "{0}: ignored '{1}' — {2}".format(key, entry, reason)
            )

    if allowed and blocked:
        # Key the dedupe on the clean lists so editing one list
        # re-fires the warning if the conflict is still present.
        sig = (
            tool_label,
            "__conflict__",
            "|".join(sorted(allowed)) + "::" + "|".join(sorted(blocked)),
        )
        if sig not in _warned_domain_issues:
            _warned_domain_issues.add(sig)
            messages.append(
                "Both {0} and {1} are set; the Anthropic API rejects "
                "both at once, so {1} is being ignored. Clear one of "
                "the two lists to silence this warning.".format(
                    allowed_key, blocked_key
                )
            )

    return messages


def build_web_search_tool_def(settings):
    """Build web search tool definition from settings, or None if disabled.

    Args:
        settings: Sublime Text settings object (or dict-like).

    Returns:
        dict or None: The web search tool definition for the API request.
    """
    if not settings.get("web_search", False):
        return None

    try:
        max_uses = int(settings.get("web_search_max_uses", 5))
        max_uses = max(1, min(20, max_uses))
    except (TypeError, ValueError):
        max_uses = 5

    tool_def = {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": max_uses,
    }

    allowed, _ = _clean_domain_list(
        settings.get("web_search_allowed_domains")
    )
    blocked, _ = _clean_domain_list(
        settings.get("web_search_blocked_domains")
    )
    if allowed:
        tool_def["allowed_domains"] = allowed
    elif blocked:
        tool_def["blocked_domains"] = blocked

    user_loc = settings.get("web_search_user_location")
    if (
        isinstance(user_loc, dict)
        and user_loc.get("type") == "approximate"
    ):
        # Only include keys the user actually populated. The Anthropic API
        # treats empty strings as data rather than absence, so sending
        # ``"region": ""`` for a user who set only ``city`` subtly degrades
        # localization. Trim and skip anything that doesn't look like a
        # meaningful value.
        loc_out = {"type": "approximate"}
        for key in ("city", "region", "country", "timezone"):
            val = user_loc.get(key)
            if isinstance(val, str) and val.strip():
                loc_out[key] = val.strip()
        if len(loc_out) > 1:
            tool_def["user_location"] = loc_out

    return tool_def


def get_web_search_cost_per_search(settings) -> float:
    """Return the per-search cost, coerced to float with a safe fallback."""
    default = 0.01
    try:
        return float(settings.get("web_search_cost_per_search", default))
    except (TypeError, ValueError):
        return default


def build_text_editor_tool_def(settings, model):
    """Build text editor tool definition, or None if disabled.

    Args:
        settings: Sublime Text settings object (or dict-like).
        model: The model name string (e.g. 'claude-sonnet-4-5').

    Returns:
        dict or None: The text editor tool definition for the API request.
    """
    if not settings.get("text_editor_tool", False):
        return None

    model_lower = (model or "").lower()
    if "claude-3-7" in model_lower:
        tool_def = {
            "type": "text_editor_20250124",
            "name": "str_replace_editor",
        }
    else:
        tool_def = {
            "type": "text_editor_20250728",
            "name": "str_replace_based_edit_tool",
        }
        try:
            max_chars = int(settings.get("text_editor_tool_max_characters", 0))
            if max_chars > 0:
                tool_def["max_characters"] = max_chars
        except (TypeError, ValueError):
            pass

    return tool_def


def build_web_fetch_tool_def(settings):
    """Build web fetch tool definition from settings, or None if disabled.

    Uses the server-side ``web_fetch_20250910`` tool (content fetched on
    Anthropic's infrastructure). There is no per-URL client hook; users
    control exposure via ``web_fetch_allowed_domains`` / ``_blocked_domains``
    and ``web_fetch_max_uses``.

    Args:
        settings: Sublime Text settings object (or dict-like).

    Returns:
        dict or None: The web fetch tool definition for the API request.
    """
    if not settings.get("web_fetch", False):
        return None

    try:
        max_uses = int(settings.get("web_fetch_max_uses", 5))
        max_uses = max(1, min(20, max_uses))
    except (TypeError, ValueError):
        max_uses = 5

    tool_def = {
        "type": "web_fetch_20250910",
        "name": "web_fetch",
        "max_uses": max_uses,
    }

    allowed, _ = _clean_domain_list(
        settings.get("web_fetch_allowed_domains")
    )
    blocked, _ = _clean_domain_list(
        settings.get("web_fetch_blocked_domains")
    )
    if allowed:
        tool_def["allowed_domains"] = allowed
    elif blocked:
        tool_def["blocked_domains"] = blocked

    try:
        max_content = int(settings.get("web_fetch_max_content_tokens", 0))
    except (TypeError, ValueError):
        max_content = 0
    if max_content > 0:
        tool_def["max_content_tokens"] = max_content

    if settings.get("web_fetch_citations", True):
        tool_def["citations"] = {"enabled": True}

    return tool_def


def build_bash_tool_def(settings):
    """Build bash tool definition from settings, or None if disabled.

    Args:
        settings: Sublime Text settings object (or dict-like).

    Returns:
        dict or None: The bash tool definition for the API request.
    """
    if not settings.get("bash_tool", False):
        return None

    return {"type": "bash_20250124", "name": "bash"}


def parse_web_search_items(items):
    """Parse web search result items into markdown source lines.

    Args:
        items: List of content items from a web_search_tool_result block.

    Returns:
        tuple: (markdown link lines, whether an error was present)
    """
    lines = []
    has_error = False
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "web_search_tool_result_error":
            has_error = True
            break
        if item.get("type") == "web_search_result":
            url = item.get("url", "")
            title = item.get("title", url)
            link = format_md_link(title, url)
            if link:
                lines.append("- " + link)
    return lines, has_error


# Friendly messages for Anthropic web_fetch error codes. Unknown codes fall
# through to the raw code so new errors still surface something useful.
WEB_FETCH_ERROR_MESSAGES = {
    "invalid_input": "invalid URL format",
    "url_too_long": "URL exceeded the 250-character limit",
    "url_not_allowed": (
        "URL blocked by domain filtering rules or model restrictions"
    ),
    "url_not_accessible": "failed to fetch the URL (HTTP error)",
    "too_many_requests": (
        "rate limit exceeded — try again shortly"
    ),
    "unsupported_content_type": (
        "content type not supported (only text and PDF)"
    ),
    "max_uses_exceeded": (
        "hit the per-turn fetch limit — raise web_fetch_max_uses in settings"
    ),
    "unavailable": "service temporarily unavailable",
}


def parse_web_fetch_result(block_content):
    """Parse a ``web_fetch_tool_result`` block's content.

    Unlike ``web_search_tool_result`` (a list of result items), the web fetch
    result content is a single dict — either ``web_fetch_result`` on success
    or ``web_fetch_tool_error`` on failure.

    Args:
        block_content: The ``content`` field of a ``web_fetch_tool_result``
            block.

    Returns:
        tuple: (source_line, error_code, url)
          - ``source_line``: markdown bullet for the fetched URL, or
            ``None`` on error / missing URL.
          - ``error_code``: the error code string if the fetch failed,
            else ``None``.
          - ``url``: the URL reported by the server on success, else
            ``None``. On error the server does not echo the URL here;
            callers correlate with the preceding ``server_tool_use``.
    """
    if not isinstance(block_content, dict):
        return None, None, None
    ctype = block_content.get("type")
    if ctype == "web_fetch_tool_error":
        return (
            None,
            block_content.get("error_code") or "unavailable",
            None,
        )
    if ctype != "web_fetch_result":
        return None, None, None
    url = block_content.get("url") or ""
    if not url:
        return None, None, None
    title = url
    inner = block_content.get("content")
    if isinstance(inner, dict):
        t = inner.get("title")
        if isinstance(t, str) and t.strip():
            title = t.strip()
    # Always return the URL so the audit log can name what the model
    # asked to fetch, even when the scheme is outside our http(s)
    # allowlist. Only the ``## Sources`` list item is suppressed.
    link = format_md_link(title, url)
    source_line = ("- " + link) if link else None
    return source_line, None, url


def format_search_results(source_lines):
    """Format web search source lines into a markdown section.

    Args:
        source_lines: Markdown link lines (e.g. ["- [Title](url)", ...]).

    Returns:
        str: Markdown section with heading, or empty if no lines.
    """
    if not source_lines:
        return ""
    seen_lines = set()
    seen_urls = set()
    deduped = []
    for line in source_lines:
        # Deduplicate by URL when possible so duplicate hits with different
        # titles do not bloat the sources list.
        url = ""
        if isinstance(line, str):
            start = line.rfind("](")
            if start != -1 and line.endswith(")"):
                url = line[start + 2 : -1]
                if "\\)" in url or "\\\\" in url:
                    url = url.replace("\\)", ")").replace("\\\\", "\\")
        if url:
            if url in seen_urls:
                continue
            seen_urls.add(url)
        if line in seen_lines:
            continue
        seen_lines.add(line)
        deduped.append(line)
    return "## Sources\n\n" + "\n".join(deduped) + "\n"
