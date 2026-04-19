"""
Claudette: Init CLAUDE.md — port of Claude Code's ``/init`` slash command.

Opens a fresh chat view and asks Claude to inspect the project and
generate (or improve) a root-level ``CLAUDE.md``. Requires the text
editor tool so Claude can actually write the file; if the tool is
disabled, Claudette offers an inline confirmation in the new chat view
that flips the setting on with the user's consent before continuing.
"""

import threading

import sublime
import sublime_plugin

from ..constants import PLUGIN_NAME, SETTINGS_FILE
from .ask_question import ClaudetteAskQuestionCommand
from .chat_view import ClaudetteChatView
from .confirmation import ConfirmationOption, ConfirmationRequest


# Ported verbatim (minus trailing newline handling) from
# ``claude-code-sourcemap/src/commands/init.ts`` so the generated
# CLAUDE.md looks familiar to users coming from Claude Code.
_INIT_PROMPT = (
    "Please analyze this codebase and create a CLAUDE.md file "
    "containing:\n"
    "1. Build/lint/test commands - especially for running a single "
    "test\n"
    "2. Code style guidelines including imports, formatting, types, "
    "naming conventions, error handling, etc.\n\n"
    "The file you create will be given to agentic coding agents "
    "(such as yourself) that operate in this repository. Make it "
    "about 20 lines long.\n"
    "If there's already a CLAUDE.md, improve it.\n"
    "If there are Cursor rules (in .cursor/rules/ or .cursorrules) "
    "or Copilot rules (in .github/copilot-instructions.md), make "
    "sure to include them."
)


class ClaudetteInitClaudeMdCommand(sublime_plugin.WindowCommand):
    """Ask Claude to write or refresh the project's CLAUDE.md."""

    def is_enabled(self):
        return True

    def is_visible(self):
        return True

    def run(self):
        window = self.window or sublime.active_window()
        if not window:
            sublime.error_message(
                "{0} Error: No active window found".format(PLUGIN_NAME)
            )
            return

        settings = sublime.load_settings(SETTINGS_FILE)

        ask_command = ClaudetteAskQuestionCommand(window)
        ask_command.load_settings()

        if not ask_command.create_chat_panel(force_new=True):
            return

        if settings.get("text_editor_tool", False):
            ask_command.send_to_claude("", _INIT_PROMPT)
            return

        # ``ConfirmationManager.request`` blocks on a ``threading.Event`` and
        # must not run on the main thread. Hop to a worker, prompt the user
        # inline, and — if they accept — flip the setting and bounce the
        # actual send back to the main thread where ``send_to_claude``
        # expects to live.
        threading.Thread(
            target=self._confirm_text_editor_and_send,
            args=(window, ask_command, settings),
            daemon=True,
        ).start()

    def _confirm_text_editor_and_send(self, window, ask_command, settings):
        chat_mgr = ClaudetteChatView._instances.get(window.id())
        if chat_mgr is None or chat_mgr.confirmation is None:
            return
        target_view = chat_mgr.view
        if target_view is None:
            return

        request = ConfirmationRequest(
            title="Enable Text Editor Tool?",
            icon="ℹ️",
            message_markdown=(
                "To create or update the CLAUDE.md file the text editor tool"
                "needs to be enabled so that Claude can write the file to disk.\n\n"
                "- With the tool enabled, Claude can view and edit "
                "files on your computer.\n"
                "- You can turn it off at any time via "
                "`Preferences > Package Settings > Claudette > "
                "Settings` (`text_editor_tool`)."
            ),
            question="Enable the text editor tool and continue?",
            options=[
                ConfirmationOption(
                    id="yes", label="Enable text editor tool"
                ),
                ConfirmationOption(id="no", label="Cancel"),
            ],
            cancel_index=1,
        )
        result = chat_mgr.request_confirmation(
            request, view_id=target_view.id()
        )

        # ``request_confirmation`` returns the chosen option id, or
        # ``RESULT_CANCELLED`` if the view closed / the prompt timed out.
        # Either way, anything that isn't an explicit "yes" means abort.
        if result != "yes":
            return

        settings.set("text_editor_tool", True)
        sublime.save_settings(SETTINGS_FILE)

        sublime.set_timeout(
            lambda: ask_command.send_to_claude("", _INIT_PROMPT), 0
        )
