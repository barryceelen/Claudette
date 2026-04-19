"""
Claudette: Init CLAUDE.md — port of Claude Code's ``/init`` slash command.

Opens a fresh chat view and asks Claude to inspect the project and
generate (or improve) a root-level ``CLAUDE.md``. Requires the text
editor tool so Claude can actually write the file; otherwise, the
command surfaces an error explaining how to enable it.
"""

import sublime
import sublime_plugin

from ..constants import PLUGIN_NAME, SETTINGS_FILE
from .ask_question import ClaudetteAskQuestionCommand


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
        if not settings.get("text_editor_tool", False):
            sublime.error_message(
                "{0}: Init CLAUDE.md needs the text editor tool so Claude "
                "can write the file. Enable \"text_editor_tool\": true in "
                "Preferences > Package Settings > Claudette > Settings, "
                "then run this command again.".format(PLUGIN_NAME)
            )
            return

        ask_command = ClaudetteAskQuestionCommand(window)
        ask_command.load_settings()

        if not ask_command.create_chat_panel(force_new=True):
            return

        ask_command.send_to_claude("", _INIT_PROMPT)
