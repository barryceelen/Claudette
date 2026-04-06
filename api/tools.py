"""Tool definition builders for Claude API requests.

Builds the JSON tool config dicts that go into the API request body.
This is tool *configuration*, not tool *execution* — execution lives in tools/.
"""


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

    allowed = settings.get("web_search_allowed_domains")
    blocked = settings.get("web_search_blocked_domains")
    if allowed and isinstance(allowed, list) and len(allowed) > 0:
        tool_def["allowed_domains"] = [
            str(d).strip() for d in allowed if str(d).strip()
        ]
    elif blocked and isinstance(blocked, list) and len(blocked) > 0:
        tool_def["blocked_domains"] = [
            str(d).strip() for d in blocked if str(d).strip()
        ]

    user_loc = settings.get("web_search_user_location")
    if (
        user_loc
        and isinstance(user_loc, dict)
        and user_loc.get("type") == "approximate"
        and (
            user_loc.get("city")
            or user_loc.get("country")
            or user_loc.get("timezone")
        )
    ):
        tool_def["user_location"] = {
            "type": "approximate",
            "city": str(user_loc.get("city", "")),
            "region": str(user_loc.get("region", "")),
            "country": str(user_loc.get("country", "")),
            "timezone": str(user_loc.get("timezone", "")),
        }

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

    Args:
        source_lines: Markdown link lines (e.g. ["- [Title](url)", ...]).

    Returns:
        str: Markdown section with heading, or empty if no lines.
    """
    if not source_lines:
        return ""
    return "### Search Results\n\n" + "\n".join(source_lines) + "\n\n"
