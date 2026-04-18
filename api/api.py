import json
import os
import select
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request

import sublime

from ..constants import (
    ANTHROPIC_VERSION,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    DEFAULT_VERIFY_SSL,
    MAX_TOKENS,
    PLUGIN_NAME,
    SETTINGS_FILE,
)
from ..statusbar.spinner import ClaudetteSpinner
from ..tools.bash import BashSession, initial_bash_cwd, run_bash_tool
from ..tools.confirmation_errors import ToolUseDeniedError
from ..tools.text_editor import (
    NO_ALLOWED_ROOTS_MESSAGE,
    get_allowed_roots,
    resolve_path,
    run_text_editor_tool,
)
from ..utils import claudette_chat_status_message, claudette_get_api_key_value
from . import session_stats
from .cancellation import CancellationToken
from .errors import (
    handle_model_not_found,
    is_model_not_found_error,
    parse_api_error,
)
from .session_stats import format_status_message, update_session_stats
from .tools import (
    build_bash_tool_def,
    build_text_editor_tool_def,
    build_web_search_tool_def,
    format_search_results,
    parse_web_search_items,
)


class CancelledException(Exception):
    """Raised when a request is cancelled."""

    pass


class ClaudetteClaudeAPI:
    def __init__(self):
        self.settings = sublime.load_settings(SETTINGS_FILE)
        self.api_key = claudette_get_api_key_value()
        self.base_url = self.settings.get("base_url", DEFAULT_BASE_URL)
        try:
            self.max_tokens = int(self.settings.get("max_tokens", MAX_TOKENS))
        except (TypeError, ValueError):
            self.max_tokens = MAX_TOKENS
        self.model = self.settings.get("model", DEFAULT_MODEL)
        self.temperature = self.settings.get("temperature", "1.0")
        self.session_cost = 0.0
        self.session_input_tokens = 0
        self.session_output_tokens = 0
        self.spinner = ClaudetteSpinner()
        self.pricing = self.settings.get("pricing")
        self.verify_ssl = self.settings.get("verify_ssl", DEFAULT_VERIFY_SSL)

    def _get_ssl_context(self):
        """Create and return an SSL context based on verify_ssl setting."""
        if self.verify_ssl:
            # Use default SSL context with verification enabled
            return ssl.create_default_context()
        else:
            # Create unverified SSL context for self-signed certificates
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            return ssl_context

    def _get_custom_headers(self):
        """Return custom headers from settings, if any."""
        custom = self.settings.get("custom_headers", {})
        if isinstance(custom, dict):
            return {str(k): str(v) for k, v in custom.items() if k}
        return {}

    @staticmethod
    def get_valid_temperature(temp):
        try:
            temp = float(temp)
            if 0.0 <= temp <= 1.0:
                return temp
            return 1.0
        except (TypeError, ValueError):
            return 1.0

    def _maybe_offer_web_search_enable(self, chat_view) -> bool:
        """One-time prompt to turn on ``web_search`` globally when unset.

        The prompt fires only when the user has *never* expressed an
        opinion on ``web_search``. Detection relies on the default
        ``Claudette.sublime-settings`` *not* defining the key — so
        ``self.settings.has("web_search")`` is True iff the user wrote
        a value into their overlay (via the prompt below or by hand).
        Picking Yes / No writes the key; cancelling (view close,
        timeout) writes nothing so the offer re-arms next request.

        Returns True iff the user just accepted — in which case the
        setting has been flipped on and every subsequent request will
        include the hosted web_search tool without further confirmation
        (to revoke, set ``"web_search": false`` in user settings).
        """
        # Respect any explicit user choice, true or false.
        if self.settings.has("web_search"):
            return False

        view = getattr(chat_view, "view", chat_view)
        if view is None:
            return False
        window = view.window()
        if window is None:
            return False

        from ..chat.chat_view import ClaudetteChatView
        from ..chat.confirmation import (
            ConfirmationOption,
            ConfirmationRequest,
        )

        mgr = ClaudetteChatView._instances.get(window.id())
        if mgr is None or mgr.confirmation is None:
            return False

        request = ConfirmationRequest(
            title="Enable Web Search?",
            icon="🌍",
            message_markdown=(
                "Claudette can give Claude access to Anthropic's hosted "
                "web search tool so it can look up current information. "
                "This setting applies globally to all future chats and "
                "can be changed later in `Claudette.sublime-settings`."
            ),
            question="Enable web search?",
            options=[
                ConfirmationOption(
                    id="yes", label="Yes, enable web search"
                ),
                ConfirmationOption(id="no", label="No"),
            ],
            cancel_index=1,
        )
        result = mgr.request_confirmation(
            request, view_id=view.id(), spinner=self.spinner
        )

        # Only persist a choice when the user actually picked an option.
        # ``RESULT_CANCELLED`` means the view was closed or the prompt
        # timed out before they answered — leave the setting absent so
        # the offer re-arms on the next request. Esc routes to
        # ``cancel_index`` (= explicit "No") and comes back as ``"no"``.
        if result not in ("yes", "no"):
            return False

        self.settings.set("web_search", result == "yes")
        sublime.save_settings(SETTINGS_FILE)
        return result == "yes"

    @staticmethod
    def _message_has_content(msg):
        """Return True if message has content (str or list for tool turns)."""
        content = msg.get("content")
        if isinstance(content, str):
            return bool(content.strip())
        if isinstance(content, list):
            return len(content) > 0
        return False

    def _get_text_editor_tool_def(self):
        """Return text editor tool definition, or None if disabled."""
        return build_text_editor_tool_def(self.settings, self.model)

    def _build_system_messages(self, chat_view=None):
        """Build system messages list (with optional context files)."""
        system_messages = [
            {
                "type": "text",
                "text": (
                    "Format responses in markdown. Do not add a summary "
                    "before your answer. Wrap code in fenced code blocks.\n\n"
                    "If the reponse warrants being structured in sections, "
                    "use this heading structure (h1 is reserved for the chat "
                    "interface):\n\n"
                    "Content here.\n\n"
                    "## Subtopic\n\n"
                    "More content.\n\n"
                    "```python\n"
                    "# code example\n"
                    "```"
                ),
            }
        ]

        settings_system_messages = self.settings.get("system_messages", [])
        default_index = self.settings.get("default_system_message_index", 0)

        if (
            settings_system_messages
            and isinstance(settings_system_messages, list)
            and isinstance(default_index, int)
            and 0 <= default_index < len(settings_system_messages)
        ):
            selected_message = settings_system_messages[default_index]
            if selected_message and selected_message.strip():
                system_messages.append(
                    {"type": "text", "text": selected_message.strip()}
                )

        if self.settings.get("text_editor_tool", False) and chat_view:
            view = getattr(chat_view, "view", chat_view)
            window = view.window() if view else None
            if window:
                allowed_roots = get_allowed_roots(window, self.settings)
                if allowed_roots:
                    lines = [
                        (
                            "You have file access. Use paths relative to "
                            "the project root(s) below."
                        ),
                        (
                            "Use 'view' with '.' to list the project root, "
                            "or with a path like 'chat/ask_question.py' "
                            "to read a file."
                        ),
                        "Top-level project structure:",
                    ]
                    for root in allowed_roots:
                        try:
                            names = sorted(os.listdir(root))[:50]
                            entries = ", ".join(names) if names else "(empty)"
                            lines.append("- {0}: {1}".format(root, entries))
                        except OSError:
                            lines.append("- {0}: (cannot list)".format(root))
                    system_messages.append(
                        {
                            "type": "text",
                            "text": "\n".join(lines),
                        }
                    )

        if chat_view:
            context_files = chat_view.settings().get(
                "claudette_context_files", {}
            )
            if context_files:
                combined_content = "<reference_files>\n"
                for file_path, file_info in context_files.items():
                    if file_info.get("content"):
                        combined_content += "<file>\n"
                        combined_content += f"<path>{file_path}</path>\n"
                        combined_content += (
                            f"<content>\n{file_info['content']}\n</content>\n"
                        )
                        combined_content += "</file>\n"
                combined_content += "</reference_files>"

                if combined_content != "<reference_files>\n</reference_files>":
                    system_message = {"type": "text", "text": combined_content}
                    system_message["cache_control"] = {"type": "ephemeral"}
                    system_messages.append(system_message)

        if self.settings.get("bash_tool", False) and chat_view:
            view = getattr(chat_view, "view", chat_view)
            window_bash = view.window() if view else None
            if window_bash:
                cwd_hint = initial_bash_cwd(window_bash, self.settings)
                if cwd_hint:
                    system_messages.append(
                        {
                            "type": "text",
                            "text": (
                                "You have a persistent bash session. Initial "
                                "working directory: {0}. Shell commands run on "
                                "the user's machine with their permissions."
                            ).format(cwd_hint),
                        }
                    )

        return system_messages

    def _stream_one_turn(
        self,
        chunk_callback,
        messages,
        system_messages,
        tools_list=None,
        cancellation_token=None,
        error_view=None,
    ):
        """
        Stream a single assistant turn from the API.

        Text deltas are forwarded to ``chunk_callback(text, is_done=False)``
        as they arrive. Tool-use blocks are reassembled from
        ``input_json_delta`` fragments and returned to the caller for
        execution. Web-search sources are accumulated and returned so the
        caller can render them (typically deferred until the final turn).

        Returns:
            tuple: ``(stop_reason, assistant_content, usage, sources_lines)``
              - ``stop_reason``: str or None (e.g. ``"tool_use"``,
                ``"end_turn"``).
              - ``assistant_content``: list of content blocks (text,
                tool_use, server_tool_use, web_search_tool_result) in
                the order the API emitted them — suitable for echoing
                back in the next turn's messages.
              - ``usage``: dict with input/output token counts, cache
                fields, and ``server_tool_use.web_search_requests``.
              - ``sources_lines``: markdown list-item strings for any
                web-search results encountered during this turn.

        Raises ``CancelledException`` if the cancellation token fires.
        Network and HTTP errors propagate to the caller.
        """
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        headers.update(self._get_custom_headers())

        data = {
            "messages": messages,
            "max_tokens": self.max_tokens,
            "model": self.model,
            "stream": True,
            "system": system_messages,
            "temperature": self.get_valid_temperature(self.temperature),
        }
        if tools_list:
            data["tools"] = tools_list

        req = urllib.request.Request(
            urllib.parse.urljoin(self.base_url, "messages"),
            data=json.dumps(data).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        # Parser state: blocks indexed by API-assigned index so we can
        # accumulate text/JSON fragments across many delta events and
        # reassemble them on ``content_block_stop``.
        blocks_by_index = {}
        block_order = []
        sources_lines = []
        stop_reason = None
        input_tokens = 0
        output_tokens = 0
        cache_read_tokens = 0
        cache_write_tokens = 0
        web_search_requests = 0

        ssl_context = self._get_ssl_context()
        with urllib.request.urlopen(
            req, context=ssl_context, timeout=30
        ) as response:
            sock = None
            try:
                if hasattr(response.fp, "raw"):
                    raw = response.fp.raw
                    if hasattr(raw, "_sock"):
                        sock = raw._sock
            except Exception:
                pass

            if sock is None:
                try:
                    response.fp._sock.settimeout(0.5)
                except Exception:
                    pass

            while True:
                if cancellation_token and cancellation_token.is_cancelled():
                    response.close()
                    raise CancelledException()

                if sock is not None:
                    try:
                        ready, _, _ = select.select([sock], [], [], 0.3)
                        if not ready:
                            continue
                    except (ValueError, OSError, TypeError):
                        sock = None
                        try:
                            response.fp._sock.settimeout(0.5)
                        except Exception:
                            pass

                try:
                    line = response.readline()
                except socket.timeout:
                    continue
                if not line:
                    break
                if line.isspace():
                    continue

                raw_line = line.decode("utf-8")
                if not raw_line.startswith("data: "):
                    continue
                payload = raw_line[6:]
                if payload.strip() == "[DONE]":
                    break

                try:
                    event = json.loads(payload)
                except (json.JSONDecodeError, ValueError):
                    continue

                event_type = event.get("type")

                if event_type == "message_start":
                    msg = event.get("message", {}) or {}
                    usage = msg.get("usage", {}) or {}
                    input_tokens = usage.get(
                        "input_tokens", input_tokens
                    )
                    cache_read_tokens = usage.get(
                        "cache_read_input_tokens", cache_read_tokens
                    )
                    cache_write_tokens = usage.get(
                        "cache_write_input_tokens", cache_write_tokens
                    )

                elif event_type == "content_block_start":
                    idx = event.get("index")
                    block = dict(event.get("content_block", {}) or {})
                    btype = block.get("type")
                    # Attach reassembly buffers; stripped before returning.
                    block["_text_buf"] = ""
                    block["_json_buf"] = ""
                    blocks_by_index[idx] = block
                    if idx not in block_order:
                        block_order.append(idx)

                    if (
                        btype == "server_tool_use"
                        and block.get("name") == "web_search"
                    ):
                        sublime.set_timeout(
                            lambda: sublime.status_message(
                                "Searching the web..."
                            ),
                            0,
                        )
                    elif btype == "web_search_tool_result":
                        items_lines, has_error = parse_web_search_items(
                            block.get("content", [])
                        )
                        if has_error:
                            self._report_web_search_error(
                                block.get("content", []), error_view
                            )
                        else:
                            sources_lines.extend(items_lines)
                            sublime.set_timeout(
                                lambda: sublime.status_message(""), 0
                            )

                elif event_type == "content_block_delta":
                    idx = event.get("index")
                    delta = event.get("delta", {}) or {}
                    block_state = blocks_by_index.get(idx)
                    delta_type = delta.get("type")

                    if delta_type == "text_delta":
                        text = delta.get("text", "")
                        if text:
                            if block_state is not None:
                                block_state["_text_buf"] += text
                            sublime.set_timeout(
                                lambda t=text: chunk_callback(
                                    t, is_done=False
                                ),
                                0,
                            )
                    elif delta_type == "input_json_delta":
                        fragment = delta.get("partial_json", "")
                        if fragment and block_state is not None:
                            block_state["_json_buf"] += fragment
                    elif (
                        block_state is not None
                        and block_state.get("type")
                        == "web_search_tool_result"
                    ):
                        items = delta.get("content")
                        incremental = None
                        if isinstance(items, list):
                            incremental = items
                        elif isinstance(items, dict):
                            incremental = [items]
                        if incremental is not None:
                            new_lines, has_error = parse_web_search_items(
                                incremental
                            )
                            if has_error:
                                self._report_web_search_error(
                                    incremental, error_view
                                )
                            else:
                                sources_lines.extend(new_lines)

                    # Inline citations render as markdown links next to
                    # the text that used them (model-controlled; may be
                    # absent even when sources exist).
                    citations = delta.get("citations")
                    if isinstance(citations, list):
                        for cit in citations:
                            if not isinstance(cit, dict):
                                continue
                            url = cit.get("url") or ""
                            title = (
                                cit.get("title") or url or "Source"
                            )
                            if url:
                                link_md = " [{0}]({1}) ".format(
                                    title, url
                                )
                                sublime.set_timeout(
                                    lambda md=link_md: chunk_callback(
                                        md, is_done=False
                                    ),
                                    0,
                                )

                elif event_type == "content_block_stop":
                    idx = event.get("index")
                    block_state = blocks_by_index.get(idx)
                    if block_state is None:
                        continue
                    btype = block_state.get("type")
                    if btype == "text":
                        block_state["text"] = block_state.get(
                            "_text_buf", ""
                        ) or block_state.get("text", "")
                    elif btype in ("tool_use", "server_tool_use"):
                        raw_json = block_state.get("_json_buf") or ""
                        if raw_json:
                            try:
                                block_state["input"] = json.loads(raw_json)
                            except (json.JSONDecodeError, ValueError):
                                # Leave the partial buffer in place so the
                                # caller can surface a helpful error in
                                # the tool_result rather than crashing.
                                block_state["input"] = {
                                    "_malformed_json": raw_json
                                }

                elif event_type == "message_delta":
                    delta = event.get("delta", {}) or {}
                    if delta.get("stop_reason"):
                        stop_reason = delta["stop_reason"]
                    usage_delta = event.get("usage", {}) or {}
                    if "output_tokens" in usage_delta:
                        output_tokens = usage_delta["output_tokens"]
                    stu = usage_delta.get("server_tool_use")
                    if isinstance(stu, dict):
                        web_search_requests = stu.get(
                            "web_search_requests", web_search_requests
                        )

                elif event_type == "message_stop":
                    # Stream is finalized; the readline loop will exit
                    # on the next iteration when the server closes.
                    pass

        # Assemble assistant content in API-emission order, stripping
        # the internal reassembly buffers.
        assistant_content = []
        for idx in block_order:
            bs = blocks_by_index.get(idx)
            if not bs:
                continue
            clean = {k: v for k, v in bs.items() if not k.startswith("_")}
            assistant_content.append(clean)

        usage = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read_tokens,
            "cache_write_input_tokens": cache_write_tokens,
            "server_tool_use": {
                "web_search_requests": web_search_requests
            },
        }

        return stop_reason, assistant_content, usage, sources_lines

    def _report_web_search_error(self, items, error_view):
        """Surface a web-search error as a chat-status message."""
        err_item = next(
            (
                it
                for it in (items or [])
                if isinstance(it, dict)
                and it.get("type") == "web_search_tool_result_error"
            ),
            None,
        )
        error_code = (
            err_item.get("error_code", "unavailable")
            if err_item
            else "unavailable"
        )
        err_msg = "Web search error: {0}".format(error_code)

        def show(msg=err_msg, v=error_view):
            if v is not None and v.window() is not None:
                claudette_chat_status_message(v.window(), msg, "⚠️")
            sublime.status_message(msg)

        sublime.set_timeout(show, 0)

    def run_with_text_editor_loop(
        self,
        chunk_callback,
        messages,
        chat_view,
        on_complete_cb=None,
        cancellation_token=None,
    ):
        """
        Run the agent tool loop, streaming each turn to the chat view.

        Issues streaming requests in a loop: each turn's text is forwarded
        to ``chunk_callback`` live as deltas arrive, client-side tool_use
        blocks are executed between turns, and web-search sources are
        accumulated and emitted as a deferred block at the very end
        (rendered after the final text by the chat view's response
        handler).

        Calls ``chunk_callback("", is_done=True, usage_info=...)`` exactly
        once when the loop finishes normally. If cancellation_token is
        provided, cancellation is polled during streaming and between tool
        executions so the user isn't stuck waiting for the full HTTP
        timeout.
        """

        def handle_error(error_msg):
            sublime.set_timeout(
                lambda: chunk_callback(error_msg, is_done=True), 0
            )

        def check_cancelled():
            if cancellation_token and cancellation_token.is_cancelled():
                raise CancelledException()

        filtered = [m for m in messages if self._message_has_content(m)]
        if not filtered:
            return

        if not self.api_key:
            handle_error(
                "[Error] The API key is not set. Please check your API key "
                "configuration."
            )
            return

        text_editor_tool = self._get_text_editor_tool_def()
        bash_tool_def = build_bash_tool_def(self.settings)
        tools_list = []
        if text_editor_tool:
            tools_list.append(text_editor_tool)
        if bash_tool_def:
            tools_list.append(bash_tool_def)
        if not tools_list:
            handle_error(
                "[Error] No agent tools are enabled (text editor or bash)."
            )
            return

        self._maybe_offer_web_search_enable(chat_view)
        web_search_tool = build_web_search_tool_def(self.settings)
        if web_search_tool:
            tools_list.append(web_search_tool)
        print(
            "{0} agent loop tools: {1}".format(
                PLUGIN_NAME,
                [t.get("name", t.get("type", "?")) for t in tools_list],
            )
        )

        # chat_view may be sublime View or ClaudetteChatView (.view,
        # .set_tool_status, .clear_tool_status).
        view_for_api = getattr(chat_view, "view", chat_view)
        chat_view_for_status = (
            chat_view if hasattr(chat_view, "set_tool_status") else None
        )

        system_messages = self._build_system_messages(view_for_api)
        window = view_for_api.window() if view_for_api else None
        settings = self.settings
        try:
            max_chars = int(
                self.settings.get("text_editor_tool_max_characters", 0)
            )
        except (TypeError, ValueError):
            max_chars = None

        bash_session = None
        if bash_tool_def:
            bash_allowed_roots = get_allowed_roots(window, settings)
            bash_cwd = initial_bash_cwd(window, settings)
            if not bash_allowed_roots or bash_cwd is None:
                handle_error(
                    "[Error] {0}".format(
                        NO_ALLOWED_ROOTS_MESSAGE.replace("Error: ", "", 1).strip()
                    )
                )
                return
            bash_session = BashSession(
                bash_cwd,
                settings,
                allowed_roots=bash_allowed_roots,
            )
            if not bash_session.bash_available:
                handle_error(
                    "[Error] bash was not found on PATH. Install bash or add "
                    "it to PATH (e.g. Git Bash on Windows)."
                )
                return

        try:
            self.spinner.start("Fetching response")
            # Let the chat view's ConfirmationManager pause the spinner while
            # an inline prompt is awaiting input, and resume it afterwards.
            if chat_view_for_status is not None:
                chat_view_for_status.active_spinner = self.spinner
            current_messages = list(filtered)
            # Paths read in this agent loop (realpath -> mtime) for write safety.
            read_file_timestamps = {}

            try:
                max_iterations = max(1, int(
                    settings.get("max_tool_iterations", 50)
                ))
            except (TypeError, ValueError):
                max_iterations = 50
            iteration = 0

            # Accumulate usage/sources across all turns so the final cost
            # line and the deferred ``## Sources`` block cover the whole
            # exchange rather than only the last turn.
            acc_input_tokens = 0
            acc_output_tokens = 0
            acc_cache_read_tokens = 0
            acc_cache_write_tokens = 0
            acc_web_search_requests = 0
            acc_sources_lines = []

            while True:
                check_cancelled()
                iteration += 1
                if iteration > max_iterations:
                    self.spinner.stop()
                    if chat_view_for_status:
                        sublime.set_timeout(
                            lambda: chat_view_for_status.clear_tool_status(), 0
                        )
                    handle_error(
                        "[Error] Tool loop exceeded {0} iterations. "
                        "Stopping to prevent runaway execution. Adjust "
                        "max_tool_iterations in settings to raise the "
                        "limit.".format(max_iterations)
                    )
                    return
                try:
                    (
                        stop_reason,
                        content,
                        usage,
                        turn_sources,
                    ) = self._stream_one_turn(
                        chunk_callback,
                        current_messages,
                        system_messages,
                        tools_list,
                        cancellation_token=cancellation_token,
                        error_view=view_for_api,
                    )
                except CancelledException:
                    raise
                except urllib.error.HTTPError as e:
                    self.spinner.stop()
                    if chat_view_for_status:
                        sublime.set_timeout(
                            lambda: chat_view_for_status.clear_tool_status(), 0
                        )
                    error_type, error_message = parse_api_error(e)
                    if is_model_not_found_error(
                        e.code, error_type, error_message
                    ):
                        handle_model_not_found(
                            error_message, window, settings, handle_error
                        )
                        return
                    handle_error("[Error] {0}".format(error_message))
                    return
                except urllib.error.URLError as e:
                    self.spinner.stop()
                    if chat_view_for_status:
                        sublime.set_timeout(
                            lambda: chat_view_for_status.clear_tool_status(), 0
                        )
                    handle_error("[Error] {0}".format(str(e)))
                    return

                acc_input_tokens += usage.get("input_tokens", 0) or 0
                acc_output_tokens += usage.get("output_tokens", 0) or 0
                acc_cache_read_tokens += (
                    usage.get("cache_read_input_tokens", 0) or 0
                )
                acc_cache_write_tokens += (
                    usage.get("cache_write_input_tokens", 0) or 0
                )
                stu = usage.get("server_tool_use") or {}
                acc_web_search_requests += (
                    stu.get("web_search_requests", 0) or 0
                )
                acc_sources_lines.extend(turn_sources or [])

                # If the API omits stop_reason but we got text content,
                # treat it as end_turn.
                if not stop_reason and content:
                    has_text = any(
                        isinstance(b, dict) and b.get("type") == "text"
                        for b in content
                    )
                    if has_text:
                        stop_reason = "end_turn"

                if stop_reason == "tool_use":

                    def update_status(label):
                        self.spinner.set_message(label)
                        if chat_view_for_status:
                            chat_view_for_status.set_tool_status(label)

                    sublime.set_timeout(
                        lambda: update_status("Running tools…"), 0
                    )
                    tool_results = []
                    # Echo the full assistant turn back (text + any
                    # tool_use, server_tool_use, web_search_tool_result
                    # blocks) so the API sees the same context on the
                    # next round-trip. Drop only the empty text blocks.
                    assistant_content = [
                        b
                        for b in content
                        if isinstance(b, dict)
                        and not (
                            b.get("type") == "text" and not b.get("text")
                        )
                        and b.get("type")
                        in (
                            "text",
                            "tool_use",
                            "server_tool_use",
                            "web_search_tool_result",
                        )
                    ]
                    bash_chat_echo_seen = set()
                    # Per-message denial flag: once a tool_use is rejected by
                    # the user, every remaining tool_use in this assistant
                    # message is marked aborted rather than executed — matches
                    # Claude Code's ``abortController.abort()`` behavior.
                    denied = False
                    for block in assistant_content:
                        if (
                            not isinstance(block, dict)
                            or block.get("type") != "tool_use"
                        ):
                            continue
                        if denied:
                            # Sibling abort: every remaining tool_use gets a
                            # synthetic is_error tool_result so the Anthropic
                            # API still sees one tool_result per tool_use.
                            tool_results.append(
                                {
                                    "type": "tool_result",
                                    "tool_use_id": block.get("id", ""),
                                    "content": (
                                        "Tool use aborted: a sibling tool "
                                        "call was denied by the user."
                                    ),
                                    "is_error": True,
                                }
                            )
                            continue

                        inp = block.get("input", {})
                        tool_name = block.get("name", "") or ""

                        if tool_name == "bash":
                            cmd_preview = inp.get("command", "")
                            if inp.get("restart"):
                                status_label = "Restarting bash…"
                            else:
                                if isinstance(cmd_preview, str):
                                    prev = cmd_preview.replace("\n", " ")
                                    if len(prev) > 56:
                                        prev = prev[:53] + "…"
                                else:
                                    prev = "bash"
                                status_label = "Bash: {0}".format(prev)
                            sublime.set_timeout(
                                lambda s=status_label: update_status(s), 0
                            )
                            if bash_session is None:
                                result = {
                                    "type": "tool_result",
                                    "tool_use_id": block.get("id", ""),
                                    "content": (
                                        "Error: Bash tool session is not "
                                        "available."
                                    ),
                                    "is_error": True,
                                }
                            else:
                                try:
                                    result = run_bash_tool(
                                        block.get("id", ""),
                                        inp,
                                        bash_session,
                                        chat_view=chat_view_for_status,
                                        chat_echo_seen=bash_chat_echo_seen,
                                    )
                                except ToolUseDeniedError as deny:
                                    denied = True
                                    tool_results.append(
                                        {
                                            "type": "tool_result",
                                            "tool_use_id": (
                                                deny.tool_use_id
                                                or block.get("id", "")
                                            ),
                                            "content": (
                                                deny.message
                                                or "Tool use denied by user."
                                            ),
                                            "is_error": True,
                                        }
                                    )
                                    continue
                            tool_results.append(result)
                            continue

                        if tool_name not in (
                            "str_replace_editor",
                            "str_replace_based_edit_tool",
                        ):
                            tool_results.append(
                                {
                                    "type": "tool_result",
                                    "tool_use_id": block.get("id", ""),
                                    "content": (
                                        "Error: Unknown tool '{0}'.".format(
                                            tool_name
                                        )
                                    ),
                                    "is_error": True,
                                }
                            )
                            continue

                        raw_path = inp.get("path", "") or "file"
                        cmd = inp.get("command", "view")
                        action = {
                            "view": "Reading",
                            "str_replace": "Editing",
                            "create": "Creating",
                            "insert": "Editing",
                        }.get(cmd, "Processing")
                        context_files = None
                        if view_for_api and hasattr(
                            view_for_api, "settings"
                        ):
                            context_files = view_for_api.settings().get(
                                "claudette_context_files"
                            )
                        allowed_roots = get_allowed_roots(window, settings)
                        resolved, _ = resolve_path(
                            raw_path,
                            allowed_roots,
                            context_files=context_files,
                            window=window,
                        )
                        if resolved and allowed_roots:
                            try:
                                for root in allowed_roots:
                                    if (
                                        os.path.commonpath(
                                            [root, resolved]
                                        )
                                        == root
                                    ):
                                        display_path = os.path.relpath(
                                            resolved, root
                                        )
                                        break
                                else:
                                    display_path = os.path.basename(
                                        resolved
                                    )
                            except ValueError:
                                display_path = os.path.basename(resolved)
                        else:
                            display_path = (
                                os.path.basename(raw_path) or "file"
                            )
                        status_label = "{0} {1}".format(
                            action, display_path
                        )
                        sublime.set_timeout(
                            lambda s=status_label: update_status(s), 0
                        )
                        try:
                            result = run_text_editor_tool(
                                block.get("id", ""),
                                tool_name,
                                inp,
                                window,
                                settings,
                                max_characters=max_chars,
                                context_files=context_files,
                                read_file_timestamps=read_file_timestamps,
                            )
                        except ToolUseDeniedError as deny:
                            denied = True
                            tool_results.append(
                                {
                                    "type": "tool_result",
                                    "tool_use_id": (
                                        deny.tool_use_id
                                        or block.get("id", "")
                                    ),
                                    "content": (
                                        deny.message
                                        or "Tool use denied by user."
                                    ),
                                    "is_error": True,
                                }
                            )
                            continue
                        tool_results.append(result)
                        # Check for cancellation after each tool execution
                        check_cancelled()

                    user_content = tool_results
                    current_messages.append(
                        {"role": "assistant", "content": assistant_content}
                    )
                    current_messages.append(
                        {"role": "user", "content": user_content}
                    )
                    continue

                if stop_reason == "end_turn":
                    self.spinner.stop()
                    if chat_view_for_status:
                        sublime.set_timeout(
                            lambda: chat_view_for_status.clear_tool_status(), 0
                        )

                    # Text has already been streamed live. Queue the
                    # sources block so it lands at the very end of the
                    # response (after all streamed text flushes), folded
                    # under ``## Sources`` for easy collapsing.
                    sources_text = format_search_results(acc_sources_lines)
                    if sources_text:
                        sublime.set_timeout(
                            lambda t=sources_text: chunk_callback(
                                t, is_done=False, defer_to_end=True
                            ),
                            0,
                        )

                    current_cost = session_stats.calculate_cost(
                        self.pricing,
                        self.model,
                        acc_input_tokens,
                        acc_output_tokens,
                        cache_read_tokens=acc_cache_read_tokens,
                        cache_write_tokens=acc_cache_write_tokens,
                    )
                    cost_per_search = self.settings.get(
                        "web_search_cost_per_search", 0.01
                    )
                    web_search_cost = (
                        acc_web_search_requests * cost_per_search
                    )
                    current_cost += web_search_cost
                    sess = update_session_stats(
                        view_for_api,
                        acc_input_tokens,
                        acc_output_tokens,
                        current_cost,
                        acc_web_search_requests,
                    )
                    if sess:
                        status_msg = format_status_message(
                            acc_input_tokens,
                            acc_output_tokens,
                            current_cost,
                            sess["cost"],
                            cache_read_tokens=acc_cache_read_tokens,
                            cache_write_tokens=acc_cache_write_tokens,
                            web_search_requests=acc_web_search_requests,
                        )
                        sublime.set_timeout(
                            lambda s=status_msg: sublime.status_message(s), 100
                        )
                    usage_info = {
                        "input_tokens": acc_input_tokens,
                        "output_tokens": acc_output_tokens,
                        "cost": current_cost,
                        "session_cost": sess["cost"] if sess else current_cost,
                        "web_search_requests": acc_web_search_requests,
                        "session_web_search_requests": (
                            sess["web_search_requests"]
                            if sess
                            else acc_web_search_requests
                        ),
                    }
                    sublime.set_timeout(
                        lambda u=usage_info: chunk_callback(
                            "", is_done=True, usage_info=u
                        ),
                        0,
                    )
                    return

                self.spinner.stop()
                if chat_view_for_status:
                    sublime.set_timeout(
                        lambda: chat_view_for_status.clear_tool_status(), 0
                    )
                handle_error(
                    "[Error] Unexpected stop_reason: {0}. "
                    "The API response may have a different structure.".format(
                        repr(stop_reason) if stop_reason else "(empty)"
                    )
                )
                return

        except CancelledException:
            self.spinner.stop()
            if chat_view_for_status:
                sublime.set_timeout(
                    lambda: chat_view_for_status.clear_tool_status(), 0
                )
            sublime.set_timeout(
                lambda: chunk_callback("", is_done=True, was_cancelled=True), 0
            )
        except Exception as e:
            self.spinner.stop()
            if chat_view_for_status:
                sublime.set_timeout(
                    lambda: chat_view_for_status.clear_tool_status(), 0
                )
            handle_error("[Error] {0}".format(str(e)))
        finally:
            if bash_session is not None:
                bash_session.close()
            if chat_view_for_status is not None:
                chat_view_for_status.active_spinner = None

    def stream_response(
        self, chunk_callback, messages, chat_view=None, cancellation_token=None
    ):
        """
        Stream a single-turn response from the API.

        Text deltas are forwarded live to the chat view via
        ``chunk_callback``. Web-search sources are accumulated and emitted
        as a deferred ``## Sources`` block at the end of the response.

        If cancellation_token is provided, cancellation is polled during
        streaming and exits early if triggered.
        """

        def handle_error(error_msg):
            sublime.set_timeout(
                lambda: chunk_callback(error_msg, is_done=True), 0
            )

        if not messages or not any(
            self._message_has_content(msg) for msg in messages
        ):
            return

        if not self.api_key:
            handle_error(
                "[Error] The API key is not set. Please check your API key "
                "configuration."
            )
            return

        try:
            self.spinner.start("Fetching response")

            filtered_messages = [
                msg for msg in messages if self._message_has_content(msg)
            ]
            system_messages = self._build_system_messages(chat_view)

            self._maybe_offer_web_search_enable(chat_view)
            web_search_tool = build_web_search_tool_def(self.settings)
            tools_list = [web_search_tool] if web_search_tool else None

            try:
                (
                    _stop_reason,
                    _content,
                    usage,
                    sources_lines,
                ) = self._stream_one_turn(
                    chunk_callback,
                    filtered_messages,
                    system_messages,
                    tools_list,
                    cancellation_token=cancellation_token,
                    error_view=chat_view,
                )
            except CancelledException:
                sublime.set_timeout(
                    lambda: chunk_callback(
                        "", is_done=True, was_cancelled=True
                    ),
                    0,
                )
                return
            except urllib.error.HTTPError as e:
                error_type, error_message = parse_api_error(e)
                if is_model_not_found_error(
                    e.code, error_type, error_message
                ):
                    window = chat_view.window() if chat_view else None
                    handle_model_not_found(
                        error_message, window, self.settings, handle_error
                    )
                else:
                    handle_error("[Error] {0}".format(error_message))
                return
            except urllib.error.URLError as e:
                handle_error("[Error] {0}".format(str(e)))
                return

            input_tokens = usage.get("input_tokens", 0) or 0
            output_tokens = usage.get("output_tokens", 0) or 0
            cache_read_tokens = (
                usage.get("cache_read_input_tokens", 0) or 0
            )
            cache_write_tokens = (
                usage.get("cache_write_input_tokens", 0) or 0
            )
            stu = usage.get("server_tool_use") or {}
            web_search_requests = stu.get("web_search_requests", 0) or 0

            sources_text = format_search_results(sources_lines)
            if sources_text:
                sublime.set_timeout(
                    lambda t=sources_text: chunk_callback(
                        t, is_done=False, defer_to_end=True
                    ),
                    0,
                )

            current_cost = session_stats.calculate_cost(
                self.pricing,
                self.model,
                input_tokens,
                output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
            )
            cost_per_search = self.settings.get(
                "web_search_cost_per_search", 0.01
            )
            current_cost += web_search_requests * cost_per_search

            sess = update_session_stats(
                chat_view,
                input_tokens,
                output_tokens,
                current_cost,
                web_search_requests,
            )
            session_cost = sess["cost"] if sess else current_cost

            status_msg = format_status_message(
                input_tokens,
                output_tokens,
                current_cost,
                session_cost,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
                web_search_requests=web_search_requests,
            )
            sublime.set_timeout(
                lambda s=status_msg: sublime.status_message(s), 100
            )

            usage_info = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost": current_cost,
                "session_cost": session_cost,
                "web_search_requests": web_search_requests,
                "session_web_search_requests": (
                    sess["web_search_requests"]
                    if sess
                    else web_search_requests
                ),
            }
            sublime.set_timeout(
                lambda u=usage_info: chunk_callback(
                    "", is_done=True, usage_info=u
                ),
                0,
            )

        except Exception as e:
            handle_error("[Error] {0}".format(str(e)))
        finally:
            self.spinner.stop()

    def fetch_models(self):

        if not self.api_key:
            sublime.error_message(
                "The API key is undefined. Please check your API key "
                "configuration."
            )
            return []

        try:
            sublime.status_message("Fetching models")
            headers = {
                "x-api-key": self.api_key,
                "anthropic-version": ANTHROPIC_VERSION,
            }
            headers.update(self._get_custom_headers())

            req = urllib.request.Request(
                urllib.parse.urljoin(self.base_url, "models"),
                headers=headers,
                method="GET",
            )

            ssl_context = self._get_ssl_context()
            with urllib.request.urlopen(req, context=ssl_context) as response:
                data = json.loads(response.read().decode("utf-8"))
                model_ids = [item["id"] for item in data["data"]]
                sublime.status_message("")
                return model_ids

        except urllib.error.HTTPError as e:
            if e.code == 401:
                print("Claude API: {0}".format(str(e)))
                sublime.error_message(
                    "Authentication invalid when fetching the available "
                    "models from the Claude API."
                )
            else:
                print("Claude API: {0}".format(str(e)))
                sublime.error_message(
                    "An error occurred fetching the available models from "
                    "the Claude API."
                )
        except urllib.error.URLError as e:
            print("Claude API: {0}".format(str(e)))
            sublime.error_message(
                "An error occurred fetching the available models from the "
                "Claude API."
            )
        except Exception as e:
            print("Claude API: {0}".format(str(e)))
            sublime.error_message(
                "An error occurred fetching the available models from the "
                "Claude API."
            )
        finally:
            sublime.status_message("")

        return []
