"""Tool definition builders for Claude API requests.

Builds the JSON tool config dicts that go into the API request body.
This is tool *configuration*, not tool *execution* — execution lives in tools/.
"""

from typing import List, Set, Tuple

from ..constants import PLUGIN_NAME


# Cache of (allowed, blocked) tuples we've already warned about so a
# misconfiguration is reported once per unique value. Reset implicitly at
# process start; re-fires if the user edits settings and hits the conflict
# again with a different list.
_warned_domain_conflicts: Set[Tuple[Tuple[str, ...], Tuple[str, ...]]] = set()


def _clean_domain_list(value) -> List[str]:
    """Normalize a domain-list setting into a clean list of strings.

    Drops non-string entries and values that are empty after trimming,
    collapses duplicates, and preserves original order. Returns an empty
    list if ``value`` is not a list at all, so callers can treat a missing
    or malformed setting as "nothing configured" without extra checks.
    """
    if not isinstance(value, list):
        return []
    seen: Set[str] = set()
    out: List[str] = []
    for d in value:
        if not isinstance(d, str):
            continue
        s = d.strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _warn_both_domain_lists_once(
    allowed: List[str], blocked: List[str]
) -> None:
    """Log a one-time warning when both domain lists are populated.

    The Anthropic web-search tool rejects sending ``allowed_domains`` and
    ``blocked_domains`` The warning surfaces the misconfiguration in the Sublime
    console so users don't wonder why their block list has no effect.
    """
    key = (tuple(allowed), tuple(blocked))
    if key in _warned_domain_conflicts:
        return
    _warned_domain_conflicts.add(key)
    print(
        "{0}: both web_search_allowed_domains and web_search_blocked_domains "
        "are set; the Anthropic API rejects both at once so "
        "web_search_blocked_domains is being ignored. Clear one of the two "
        "lists to silence this warning.".format(PLUGIN_NAME)
    )


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

    allowed = _clean_domain_list(settings.get("web_search_allowed_domains"))
    blocked = _clean_domain_list(settings.get("web_search_blocked_domains"))
    if allowed and blocked:
        _warn_both_domain_lists_once(allowed, blocked)
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
            if url:
                lines.append("- [{0}]({1})".format(title, url))
    return lines, has_error


def format_search_results(source_lines):
    """Format web search source lines into a markdown section.

    The heading is emitted as an h2 so Sublime Text folds the block as a
    single collapsible section at the end of the assistant response. The
    blank-line separator before the heading is inserted by the streaming
    response handler (see ``_ensure_blank_line``) based on the view's
    current tail, so this format does not prepend its own newline.

    Args:
        source_lines: Markdown link lines (e.g. ["- [Title](url)", ...]).

    Returns:
        str: Markdown section with heading, or empty if no lines.
    """
    if not source_lines:
        return ""
    return "## Sources\n\n" + "\n".join(source_lines) + "\n"
