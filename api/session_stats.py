"""Session statistics tracking and cost calculation."""


def _default_session_stats():
    """Return a fresh default session stats dict."""
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cost": 0.0,
        "web_search_requests": 0,
        "web_fetch_requests": 0,
    }


def calculate_cost(
    pricing,
    model,
    input_tokens,
    output_tokens,
    cache_read_tokens=0,
    cache_write_tokens=0,
):
    """Calculate cost based on token usage and model.

    Args:
        pricing: Pricing dict from settings (tier_name -> input/output/etc.).
        model: Model name string.
        input_tokens: Number of input tokens.
        output_tokens: Number of output tokens.
        cache_read_tokens: Number of tokens read from cache.
        cache_write_tokens: Number of tokens written to cache.

    Returns:
        float: The calculated cost.
    """
    if not pricing or not model:
        return 0

    price_tier = None
    model_lower = model.lower()

    for tier in pricing.keys():
        if tier in model_lower:
            price_tier = pricing[tier]
            break

    if not price_tier:
        return 0

    # Pricing is per 1M tokens
    # Non-cached input tokens are charged at the full input rate.
    # Clamp to zero in case cache tokens exceed input_tokens.
    non_cached_tokens = max(0, input_tokens - cache_read_tokens - cache_write_tokens)
    input_cost = (non_cached_tokens / 1_000_000) * price_tier["input"]
    output_cost = (output_tokens / 1_000_000) * price_tier["output"]
    cache_write_cost = (cache_write_tokens / 1_000_000) * price_tier.get(
        "cache_write", 0
    )
    cache_read_cost = (cache_read_tokens / 1_000_000) * price_tier.get(
        "cache_read", 0
    )

    return input_cost + output_cost + cache_write_cost + cache_read_cost


def update_session_stats(
    view,
    input_tokens,
    output_tokens,
    cost,
    web_search_requests=0,
    web_fetch_requests=0,
):
    """Update the session stats stored on a view's settings.

    Reads the current 'claudette_session_stats' from the view, adds the new
    values, and writes back. Returns the updated stats dict.

    Args:
        view: A sublime.View with a settings() method.
        input_tokens: Tokens to add to session input total.
        output_tokens: Tokens to add to session output total.
        cost: Cost to add to session total.
        web_search_requests: Web search requests to add to session total.
        web_fetch_requests: web_fetch tool calls to add to session total.

    Returns:
        dict: The updated session stats, or None if view has no settings.
    """
    if not view or not hasattr(view, "settings"):
        return None

    settings = view.settings()
    sess = settings.get("claudette_session_stats", _default_session_stats())

    if "web_search_requests" not in sess:
        sess["web_search_requests"] = 0
    if "web_fetch_requests" not in sess:
        sess["web_fetch_requests"] = 0

    sess["input_tokens"] = sess.get("input_tokens", 0) + input_tokens
    sess["output_tokens"] = sess.get("output_tokens", 0) + output_tokens
    sess["cost"] = sess.get("cost", 0.0) + cost
    sess["web_search_requests"] = (
        sess.get("web_search_requests", 0) + web_search_requests
    )
    sess["web_fetch_requests"] = (
        sess.get("web_fetch_requests", 0) + web_fetch_requests
    )

    settings.set("claudette_session_stats", sess)
    return sess


def format_status_message(
    input_tokens,
    output_tokens,
    current_cost,
    session_cost,
    cache_read_tokens=0,
    cache_write_tokens=0,
    web_search_requests=0,
    web_fetch_requests=0,
):
    """Format a status bar message with token and cost information.

    Args:
        input_tokens: Number of input tokens for this message.
        output_tokens: Number of output tokens for this message.
        current_cost: Cost of the current message.
        session_cost: Total session cost so far.
        cache_read_tokens: Number of tokens read from cache.
        cache_write_tokens: Number of tokens written to cache.
        web_search_requests: Number of web searches in this message.
        web_fetch_requests: Number of web_fetch tool uses in this message.

    Returns:
        str: The formatted status message.
    """
    parts = []
    parts.append("{0:,} in, {1:,} out".format(input_tokens, output_tokens))

    cache_parts = []
    if cache_read_tokens > 0:
        cache_parts.append("{0:,} cache read".format(cache_read_tokens))
    if cache_write_tokens > 0:
        cache_parts.append("{0:,} cache write".format(cache_write_tokens))
    if cache_parts:
        parts.append(", ".join(cache_parts))

    status = "Tokens: " + ", ".join(parts) + "."

    if web_search_requests > 0:
        status += " Web searches: {0}.".format(web_search_requests)

    if web_fetch_requests > 0:
        status += " Web fetches: {0}.".format(web_fetch_requests)

    if session_cost > 0:
        status += " Cost: ${0:.2f} (${1:.2f} session)".format(
            current_cost, session_cost
        )

    return status
