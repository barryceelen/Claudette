"""
Inline confirmation prompts rendered into the chat view as a single phantom.

Worker threads (bash tool, pre-request web search gate) call
``ConfirmationManager.request`` to ask the user a Yes/No-style question without
blocking on a Sublime modal. Rendering and navigation happen on the main thread
via window commands bound in ``Default.sublime-keymap``; the worker blocks on a
``threading.Event`` until the user picks an option or the view is closed.

The whole prompt — title, body, question, options, and the key-hint line — is
one ``sublime.LAYOUT_BLOCK`` phantom anchored at the end of the chat view.
"""

import re
import threading
from dataclasses import dataclass
from typing import List, Optional

import sublime
import sublime_plugin

from ..constants import PLUGIN_NAME


RESULT_CANCELLED = "__cancelled__"


@dataclass
class ConfirmationOption:
    """A single selectable option in a confirmation prompt.

    ``id`` is returned to the caller; ``label`` is what the user sees.
    """

    id: str
    label: str


@dataclass
class ConfirmationRequest:
    """Payload describing one confirmation prompt.

    ``icon`` and ``title`` render as the heading; ``message_markdown`` is the
    pre-question body (may contain fenced code blocks — they render as a
    monospace block inside the phantom). ``cancel_index`` selects the option
    that Esc maps to — default is the last option (the "no" path).
    """

    title: str
    icon: str
    message_markdown: str
    question: str
    options: List[ConfirmationOption]
    initial_index: int = 0
    cancel_index: Optional[int] = None


_FENCED_CODE_RE = re.compile(r"```([a-zA-Z0-9_+\-]*)\n(.*?)\n```", re.DOTALL)


def _escape_html(s: str) -> str:
    """Minimal HTML escaper for minihtml text nodes."""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _paragraphs_to_minihtml(text: str) -> str:
    """Convert a plain-text chunk into one ``<div>`` per non-empty paragraph.

    Paragraphs are separated by blank lines; single newlines inside a paragraph
    become ``<br>`` so multi-line prose keeps its wrapping intent.
    """
    if not text or not text.strip():
        return ""
    parts = []
    for para in text.split("\n\n"):
        para = para.strip("\n")
        if not para.strip():
            continue
        escaped = _escape_html(para).replace("\n", "<br>")
        parts.append('<div class="body">{0}</div>'.format(escaped))
    return "".join(parts)


def _markdown_to_minihtml(md: str) -> str:
    """Convert the subset of markdown we emit into minihtml.

    We only need to handle two shapes: plain paragraphs and fenced ``` code
    blocks. The language hint (e.g. ```bash) is accepted but ignored — we
    don't do syntax highlighting in the phantom.
    """
    if not md:
        return ""
    html_parts = []
    last_end = 0
    for match in _FENCED_CODE_RE.finditer(md):
        html_parts.append(_paragraphs_to_minihtml(md[last_end : match.start()]))
        code = match.group(2)
        escaped = _escape_html(code).replace("\n", "<br>")
        html_parts.append('<div class="code">{0}</div>'.format(escaped))
        last_end = match.end()
    html_parts.append(_paragraphs_to_minihtml(md[last_end:]))
    return "".join(p for p in html_parts if p)


def _options_to_minihtml(options, current_index: int) -> str:
    """Render the option list, marking the current selection.

    Each option is rendered as a clickable minihtml link whose ``href`` is
    ``select:<index>``; the phantom's ``on_navigate`` handler (see
    ``ConfirmationManager._on_phantom_navigate``) confirms the matching
    option when clicked.
    """
    parts = []
    for i, opt in enumerate(options):
        is_current = i == current_index
        cls = "option current" if is_current else "option"
        marker = "›" if is_current else "&nbsp;"
        parts.append(
            '<div class="{0}">{1} '
            '<a class="option-link" href="select:{2}">{3}. {4}</a>'
            "</div>".format(
                cls, marker, i, i + 1, _escape_html(opt.label)
            )
        )
    return "".join(parts)


# Phantom stylesheet. Colors use minihtml's ``color()`` and ``var()``
# functions so the prompt picks up the user's color scheme instead of
# hard-coding light/dark values.
_PHANTOM_STYLES = """
<style>
    body#claudette-confirmation {
        margin: 0;
        padding: 0.4rem;
    }
    .title {
        font-weight: bold;
        font-size: 1.05rem;
        margin-bottom: 0.4rem;
    }
    .body {
        margin: 0.2rem 0;
    }
    .code {
        background-color: color(var(--background) blend(var(--foreground) 85%));
        padding: 0.35rem 0.55rem;
        margin: 0.35rem 0;
        border-radius: 3px;
        font-family: monospace;
    }
    .question {
        margin: 0.5rem 0 0.35rem 0;
    }
    .option {
        margin: 0.1rem 0;
    }
    .option.current {
        font-weight: bold;
        color: var(--accent);
    }
    a.option-link {
        text-decoration: none;
        color: var(--foreground);
    }
    .option.current a.option-link {
        color: var(--accent);
    }
    .hint {
        margin-top: 0.6rem;
        color: color(var(--foreground) alpha(0.55));
        font-size: 0.9rem;
    }
</style>
"""


def _build_phantom_html(request: ConfirmationRequest, current_index: int) -> str:
    """Assemble the minihtml document for the prompt phantom."""
    title = "{0} {1}".format(
        _escape_html(request.icon), _escape_html(request.title)
    )
    body_html = _markdown_to_minihtml(request.message_markdown)
    question_html = (
        '<div class="question">{0}</div>'.format(
            _escape_html(request.question.strip())
        )
        if request.question and request.question.strip()
        else ""
    )
    options_html = _options_to_minihtml(request.options, current_index)
    hint_html = (
        '<div class="hint">Esc to cancel - Enter to confirm - '
        "↑/↓ to select option</div>"
    )
    return (
        '<body id="claudette-confirmation">'
        + _PHANTOM_STYLES
        + '<div class="title">{0}</div>'.format(title)
        + body_html
        + question_html
        + options_html
        + hint_html
        + "</body>"
    )


class ConfirmationManager:
    """Per-window state for active inline confirmations.

    One manager lives on each ``ClaudetteChatView`` and serializes prompts per
    chat view id. Only one confirmation can be pending per view at a time; a
    second request while one is active is rejected (returns cancelled).
    """

    def __init__(self, window):
        self.window = window
        self._states = {}

    def has_active(self, view) -> bool:
        """True if ``view`` currently has a pending confirmation prompt."""
        if view is None:
            return False
        return view.id() in self._states

    def request(
        self,
        view,
        request: ConfirmationRequest,
        timeout_seconds: float = 3600,
        spinner=None,
    ) -> str:
        """Render the prompt on the main thread and block for a result.

        Returns the ``id`` of the selected option, or ``RESULT_CANCELLED`` if
        the view closed or the timeout fired before the user answered.

        If ``spinner`` is provided, the Sublime status-bar animation is
        paused for the duration of the prompt (so the user doesn't see
        "Fetching response ⣾" or "Running tools… ⣿" while answering) and
        resumed with its previous message afterwards.
        """
        if view is None:
            return RESULT_CANCELLED
        if not request.options:
            return RESULT_CANCELLED

        event = threading.Event()
        holder = {"result": RESULT_CANCELLED}

        saved_spinner_message = None
        if spinner is not None and getattr(spinner, "active", False):
            saved_spinner_message = spinner.message
            sublime.set_timeout(spinner.stop, 0)

        def on_main():
            try:
                self._render_on_main(view, request, holder, event)
            except Exception as e:
                print(
                    "{0} confirmation render error: {1}".format(
                        PLUGIN_NAME, e
                    )
                )
                holder["result"] = RESULT_CANCELLED
                event.set()

        sublime.set_timeout(on_main, 0)
        try:
            if not event.wait(timeout=timeout_seconds):
                sublime.set_timeout(
                    lambda: self._force_cancel(view, "Timed out"), 0
                )
                return RESULT_CANCELLED
            return holder["result"]
        finally:
            if spinner is not None and saved_spinner_message is not None:
                msg = saved_spinner_message
                sublime.set_timeout(lambda: spinner.start(msg), 0)

    def cancel_for_view(self, view) -> None:
        """Cancel a pending confirmation because the view is closing.

        Safe to call from any thread. State is torn down on the main thread
        and the blocked worker is released with ``RESULT_CANCELLED``.
        """
        if view is None:
            return
        sublime.set_timeout(
            lambda: self._force_cancel(view, "View closed"), 0
        )

    def _force_cancel(self, view, reason: str) -> None:
        """Main-thread finalize for cancellation paths (timeout, view close)."""
        if view is None:
            return
        vid = view.id()
        state = self._states.get(vid)
        if state is None:
            return
        state["holder"]["result"] = RESULT_CANCELLED
        self._finalize(view, state, summary_label="Cancelled ({0})".format(reason))

    def _render_on_main(self, view, request, holder, event) -> None:
        vid = view.id()
        if vid in self._states:
            print(
                "{0}: confirmation already pending for view {1}; "
                "dropping duplicate request".format(PLUGIN_NAME, vid)
            )
            holder["result"] = RESULT_CANCELLED
            event.set()
            return

        initial_index = max(
            0, min(request.initial_index, len(request.options) - 1)
        )
        cancel_index = request.cancel_index
        if cancel_index is None:
            cancel_index = len(request.options) - 1
        cancel_index = max(0, min(cancel_index, len(request.options) - 1))

        phantom_set = sublime.PhantomSet(
            view, "claudette_confirmation_{0}".format(vid)
        )
        anchor = sublime.Region(view.size(), view.size())
        html = _build_phantom_html(request, initial_index)
        on_navigate = self._make_navigate_handler(vid)
        phantom_set.update([
            sublime.Phantom(anchor, html, sublime.LAYOUT_BLOCK, on_navigate)
        ])
        # Make sure the phantom is visible; without this the user may need
        # to scroll down to find the prompt in a long chat.
        view.show(anchor, show_surrounds=False)

        state = {
            "request": request,
            "current_index": initial_index,
            "cancel_index": cancel_index,
            "phantom_set": phantom_set,
            "holder": holder,
            "event": event,
        }
        self._states[vid] = state
        view.settings().set("claudette_awaiting_confirmation", True)

    def cycle(self, view, delta: int) -> bool:
        """Move selection by ``delta`` (wraps). Returns True if a prompt is active."""
        if view is None:
            return False
        state = self._states.get(view.id())
        if state is None:
            return False
        options = state["request"].options
        new_index = (state["current_index"] + delta) % len(options)
        self._update_selection(view, state, new_index)
        return True

    def select_by_number(self, view, number: int) -> bool:
        """Select option ``number`` (1-based) and confirm immediately.

        Returns True if a prompt was active and the number was in range.
        """
        if view is None:
            return False
        state = self._states.get(view.id())
        if state is None:
            return False
        idx = number - 1
        if idx < 0 or idx >= len(state["request"].options):
            return False
        self._update_selection(view, state, idx)
        self._complete(view, state, idx)
        return True

    def confirm(self, view) -> bool:
        """Finalize with the currently selected option."""
        if view is None:
            return False
        state = self._states.get(view.id())
        if state is None:
            return False
        self._complete(view, state, state["current_index"])
        return True

    def cancel(self, view) -> bool:
        """Esc path: finalize with the request's cancel option."""
        if view is None:
            return False
        state = self._states.get(view.id())
        if state is None:
            return False
        self._complete(view, state, state["cancel_index"])
        return True

    def _update_selection(self, view, state, new_index: int) -> None:
        html = _build_phantom_html(state["request"], new_index)
        anchor = sublime.Region(view.size(), view.size())
        on_navigate = self._make_navigate_handler(view.id())
        state["phantom_set"].update([
            sublime.Phantom(anchor, html, sublime.LAYOUT_BLOCK, on_navigate)
        ])
        state["current_index"] = new_index

    def _make_navigate_handler(self, vid: int):
        """Build the ``on_navigate`` callback for a view's confirmation phantom.

        The phantom encodes each option as ``href="select:<index>"``. Clicking
        it selects that option and finalizes the prompt (same behaviour as
        pressing the matching number key).
        """

        def on_navigate(href: str) -> None:
            if not href.startswith("select:"):
                return
            try:
                idx = int(href[len("select:"):])
            except ValueError:
                return
            state = self._states.get(vid)
            if state is None:
                return
            view = None
            if self.window is not None:
                for v in self.window.views():
                    if v.id() == vid:
                        view = v
                        break
            if view is None:
                return
            options = state["request"].options
            if idx < 0 or idx >= len(options):
                return
            self._update_selection(view, state, idx)
            self._complete(view, state, idx)

        return on_navigate

    def _complete(self, view, state, index: int) -> None:
        option = state["request"].options[index]
        state["holder"]["result"] = option.id
        self._finalize(view, state, summary_label=option.label)

    def _finalize(self, view, state, summary_label: str) -> None:
        """Clear the phantom and release the blocked worker.

        ``summary_label`` is intentionally unused — the prompt is transient
        UI, not part of the conversation log — but kept in the signature so
        callers can surface it for diagnostics if we re-enable summary
        printing in the future.
        """
        vid = view.id()
        del summary_label
        try:
            state["phantom_set"].update([])
        except Exception as e:
            print(
                "{0} confirmation finalize error: {1}".format(
                    PLUGIN_NAME, e
                )
            )
        view.settings().set("claudette_awaiting_confirmation", False)
        self._states.pop(vid, None)
        try:
            state["event"].set()
        except Exception:
            pass


def _find_active_confirmation_view(window):
    """Return the currently focused chat view if it has a pending confirmation."""
    if window is None:
        return None, None
    active = window.active_view()
    if (
        active is not None
        and active.settings().get("claudette_awaiting_confirmation", False)
    ):
        from .chat_view import ClaudetteChatView

        mgr = ClaudetteChatView._instances.get(window.id())
        if mgr is None or getattr(mgr, "confirmation", None) is None:
            return None, None
        if mgr.confirmation.has_active(active):
            return mgr.confirmation, active
    return None, None


class ClaudetteConfirmationNextCommand(sublime_plugin.WindowCommand):
    """Down arrow: move selection to the next option (wraps)."""

    def run(self):
        manager, view = _find_active_confirmation_view(self.window)
        if manager and view:
            manager.cycle(view, 1)

    def is_enabled(self):
        manager, _ = _find_active_confirmation_view(self.window)
        return manager is not None


class ClaudetteConfirmationPrevCommand(sublime_plugin.WindowCommand):
    """Up arrow: move selection to the previous option (wraps)."""

    def run(self):
        manager, view = _find_active_confirmation_view(self.window)
        if manager and view:
            manager.cycle(view, -1)

    def is_enabled(self):
        manager, _ = _find_active_confirmation_view(self.window)
        return manager is not None


class ClaudetteConfirmationSelectNumberCommand(sublime_plugin.WindowCommand):
    """Number keys 1-9: jump to and confirm the matching option."""

    def run(self, number: int):
        manager, view = _find_active_confirmation_view(self.window)
        if manager and view:
            manager.select_by_number(view, int(number))

    def is_enabled(self):
        manager, _ = _find_active_confirmation_view(self.window)
        return manager is not None


class ClaudetteConfirmationConfirmCommand(sublime_plugin.WindowCommand):
    """Enter: finalize with the currently selected option."""

    def run(self):
        manager, view = _find_active_confirmation_view(self.window)
        if manager and view:
            manager.confirm(view)

    def is_enabled(self):
        manager, _ = _find_active_confirmation_view(self.window)
        return manager is not None


class ClaudetteConfirmationCancelCommand(sublime_plugin.WindowCommand):
    """Esc: finalize with the request's cancel option (= the No path)."""

    def run(self):
        manager, view = _find_active_confirmation_view(self.window)
        if manager and view:
            manager.cancel(view)

    def is_enabled(self):
        manager, _ = _find_active_confirmation_view(self.window)
        return manager is not None
