import json
from typing import List, Optional, Set

import sublime
import sublime_plugin

from ..api.cancellation import CancellationToken
from ..constants import PLUGIN_NAME, SPINNER_CHARS, SPINNER_INTERVAL_MS
from ..utils import claudette_cleanup_copy_path_phantoms_for_view
from .fenced_code import (
    ClaudetteCodeBlock,
    find_fenced_code_blocks,
    unclosed_fence_suffix_to_append,
)


class ClaudetteChatViewListener(sublime_plugin.ViewEventListener):
    """Event listener specifically for chat views."""

    @classmethod
    def is_applicable(cls, settings):
        """Only attach this listener to chat views."""
        return settings.get("claudette_is_chat_view", False)

    def on_text_command(self, command_name, args):
        """Handle enter key and escape key."""
        # Handle Enter key - send question
        if command_name == "insert" and args.get("characters") == "\n":
            try:
                window = self.view.window()
                if window:
                    window.run_command("claudette_ask_question")
                    return ("noop", None)
            except Exception as e:
                sublime.status_message(f"Claudette error: {str(e)}")
            return None

        # Handle Escape key - cancel active request
        if command_name == "single_selection":
            window = self.view.window()
            if window and window.id() in ClaudetteChatView._instances:
                chat_view = ClaudetteChatView._instances[window.id()]
                if chat_view.has_active_request():
                    chat_view.cancel_request()
                    return ("noop", None)

        return None

    def on_close(self):
        ClaudetteChatView.cleanup_for_closed_view(self.view)
        claudette_cleanup_copy_path_phantoms_for_view(self.view)


class ClaudetteChatView:
    """Manages chat views for the Claudette plugin."""

    _instances = {}

    @staticmethod
    def _manager_handles_view_id(mgr, view_id):
        if mgr.view is not None and mgr.view.id() == view_id:
            return True
        if view_id in mgr.phantom_sets:
            return True
        if view_id in mgr._tool_status_phantom_sets:
            return True
        if view_id in mgr.existing_button_positions:
            return True
        return False

    @staticmethod
    def _manager_is_orphaned(mgr):
        """True if no primary view and no per-view state left."""
        return (
            mgr.view is None
            and not mgr.phantom_sets
            and not mgr.existing_button_positions
            and not mgr._tool_status_phantom_sets
            and not mgr._tool_status_active
            and not mgr._tool_status_spinner_index
            and not mgr._tool_status_message
        )

    @classmethod
    def get_instance(cls, window=None, settings=None):
        """Get or create a chat view instance for the given window."""
        if window is None:
            raise ValueError("Window is required")

        window_id = window.id()

        if window_id not in cls._instances:
            if settings is None:
                raise ValueError("Settings are required for initial creation")
            cls._instances[window_id] = cls(window, settings)

        return cls._instances[window_id]

    @classmethod
    def cleanup_for_closed_view(cls, view):
        """Release per-view resources when a chat view is closed."""
        if not view.settings().get("claudette_is_chat_view", False):
            return
        view_id = view.id()
        window = view.window()
        if window:
            wid = window.id()
            if wid not in cls._instances:
                return
            mgr = cls._instances[wid]
            mgr._clear_view_resources(view_id)
            chat_views = [
                v
                for v in window.views()
                if v.settings().get("claudette_is_chat_view", False)
            ]
            closed_was_primary = (
                mgr.view is not None and mgr.view.id() == view_id
            )
            if not chat_views:
                mgr.view = None
                del cls._instances[wid]
                return
            if closed_was_primary:
                mgr.view = chat_views[0]
                mgr.view.settings().set("claudette_is_current_chat", True)
                for v in chat_views[1:]:
                    v.settings().set("claudette_is_current_chat", False)
            return
        for wid, mgr in list(cls._instances.items()):
            if not cls._manager_handles_view_id(mgr, view_id):
                continue
            mgr._clear_view_resources(view_id)
            if mgr.view is not None and mgr.view.id() == view_id:
                mgr.view = None
            if cls._manager_is_orphaned(mgr):
                del cls._instances[wid]
            break

    def __init__(self, window, settings):
        """Initialize the chat view manager."""
        self.window = window
        self.settings = settings
        self.view = None
        self.phantom_sets = {}  # Store phantom sets per view
        self.existing_button_positions = {}  # Store positions per view
        self._tool_status_phantom_sets = {}
        self._tool_status_active = {}
        self._tool_status_spinner_index = {}
        self._tool_status_message = {}
        self._active_cancellation_token: Optional[CancellationToken] = None

    def _configure_new_chat_view(self, view):
        """Apply chat view options from package settings."""
        chat_settings = self.settings.get("chat", {})
        line_numbers = chat_settings.get("line_numbers", False)
        rulers = chat_settings.get("rulers", False)
        set_scratch = chat_settings.get("set_scratch", True)

        view.set_name("Claude Chat")
        view.set_scratch(set_scratch)
        view.assign_syntax("Packages/Markdown/Markdown.sublime-syntax")
        view.set_read_only(True)
        view.settings().set("line_numbers", line_numbers)
        view.settings().set("rulers", rulers)
        view.settings().set("claudette_is_chat_view", True)
        view.settings().set("claudette_conversation", [])

    @staticmethod
    def _mark_only_current_chat(window, new_view):
        """Mark new_view as current chat; clear flag on other chats."""
        for v in window.views():
            if v != new_view and v.settings().get(
                "claudette_is_chat_view", False
            ):
                v.settings().set("claudette_is_current_chat", False)
        new_view.settings().set("claudette_is_current_chat", True)

    def create_or_get_view(self):
        """Create a new chat view or return an existing one."""
        try:
            # First check for current chat view in this window
            for view in self.window.views():
                if view.settings().get(
                    "claudette_is_chat_view", False
                ) and view.settings().get("claudette_is_current_chat", False):
                    self.view = view
                    return self.view

            # If no current chat view found, use the first chat view
            for view in self.window.views():
                if view.settings().get("claudette_is_chat_view", False):
                    self.view = view
                    # Set this view as current since none was marked as current
                    view.settings().set("claudette_is_current_chat", True)
                    return self.view

            # Create new chat view if none exists in this window
            self.view = self.window.new_file()
            if not self.view:
                print(f"{PLUGIN_NAME} Error: Could not create new file")
                sublime.error_message(
                    f"{PLUGIN_NAME} Error: Could not create new file"
                )
                return None

            self._configure_new_chat_view(self.view)
            self._mark_only_current_chat(self.window, self.view)

            return self.view

        except Exception as e:
            print(f"{PLUGIN_NAME} Error creating chat panel: {str(e)}")
            sublime.error_message(
                f"{PLUGIN_NAME} Error: Could not create chat panel"
            )
            return None

    def create_new_chat_view(self):
        """Create another chat tab and make it the current chat."""
        new_view = self.window.new_file()
        if not new_view:
            print(f"{PLUGIN_NAME} Error: Could not create new file")
            sublime.error_message(
                f"{PLUGIN_NAME} Error: Could not create new file"
            )
            return None
        self._configure_new_chat_view(new_view)
        self._mark_only_current_chat(self.window, new_view)
        self.view = new_view
        return new_view

    def get_phantom_set(self, view):
        """Get or create a phantom set for the specific view."""
        view_id = view.id()
        if view_id not in self.phantom_sets:
            self.phantom_sets[view_id] = sublime.PhantomSet(
                view, f"code_block_buttons_{view_id}"
            )
        return self.phantom_sets[view_id]

    def _get_tool_status_phantom_set(self, view):
        """Get or create the phantom set used for the tool status line."""
        view_id = view.id()
        if view_id not in self._tool_status_phantom_sets:
            self._tool_status_phantom_sets[view_id] = sublime.PhantomSet(
                view, "claudette_tool_status_{0}".format(view_id)
            )
        return self._tool_status_phantom_sets[view_id]

    def _tool_status_phantom_html(self, message, spinner_char):
        """Build HTML for the status line: message + spinner at end."""
        escaped_msg = self.escape_html(message)
        escaped_char = self.escape_html(spinner_char)
        return ("ℹ️ {0} {1}").format(escaped_msg, escaped_char)

    def set_tool_status(self, message):
        """
        Show or update tool status as a phantom (message + spinner).

        Call with message=None to clear. Spinner animates on a timer.
        """
        if not self.view:
            return
        view_id = self.view.id()
        if message is None:
            self._tool_status_active[view_id] = False
            phantom_set = self._get_tool_status_phantom_set(self.view)
            phantom_set.update([])
            return
        self._tool_status_active[view_id] = True
        self._tool_status_message[view_id] = message
        if view_id not in self._tool_status_spinner_index:
            self._tool_status_spinner_index[view_id] = 0
        idx = self._tool_status_spinner_index[view_id]
        char = SPINNER_CHARS[idx % len(SPINNER_CHARS)]
        phantom_set = self._get_tool_status_phantom_set(self.view)
        region = sublime.Region(self.view.size(), self.view.size())
        html = self._tool_status_phantom_html(message, char)
        phantom = sublime.Phantom(region, html, sublime.LAYOUT_INLINE, None)
        already_running = len(phantom_set.phantoms) > 0
        phantom_set.update([phantom])
        if not already_running:
            sublime.set_timeout(
                lambda: self._schedule_tool_status_spinner(),
                SPINNER_INTERVAL_MS,
            )

    def _schedule_tool_status_spinner(self):
        """Advance spinner frame and schedule the next tick."""
        if not self.view:
            return
        view_id = self.view.id()
        if not self._tool_status_active.get(view_id, False):
            return
        idx = self._tool_status_spinner_index.get(view_id, 0)
        idx = (idx + 1) % len(SPINNER_CHARS)
        self._tool_status_spinner_index[view_id] = idx
        char = SPINNER_CHARS[idx]
        message = self._tool_status_message.get(view_id, "")
        phantom_set = self._get_tool_status_phantom_set(self.view)
        region = sublime.Region(self.view.size(), self.view.size())
        html = self._tool_status_phantom_html(message, char)
        phantom = sublime.Phantom(region, html, sublime.LAYOUT_INLINE, None)
        phantom_set.update([phantom])
        sublime.set_timeout(
            lambda: self._schedule_tool_status_spinner(), SPINNER_INTERVAL_MS
        )

    def clear_tool_status(self):
        """Clear the tool status line from the chat."""
        self.set_tool_status(None)

    def get_button_positions(self, view):
        """Get or create a set of button positions for the specific view."""
        view_id = view.id()
        if view_id not in self.existing_button_positions:
            self.existing_button_positions[view_id] = set()
        return self.existing_button_positions[view_id]

    def get_conversation_history(self):
        """Get the conversation history from the current view's settings."""
        if not self.view:
            return []

        conversation_json = self.view.settings().get(
            "claudette_conversation_json", "[]"
        )
        try:
            return json.loads(conversation_json)
        except json.JSONDecodeError:
            print(
                f"{PLUGIN_NAME} Error: Could not decode conversation history"
            )
            return []

    def add_to_conversation(self, role: str, content):
        """Add a new message to the conversation history.

        content may be a string (normal message) or a list of content blocks
        (e.g. for assistant tool_use or user tool_result).
        """
        if not self.view:
            return

        conversation = self.get_conversation_history()
        conversation.append({"role": role, "content": content})

        try:
            conversation_json = json.dumps(conversation)
            self.view.settings().set(
                "claudette_conversation_json", conversation_json
            )
        except json.JSONEncodeError:
            print(
                f"{PLUGIN_NAME} Error: Could not encode conversation history"
            )

    def handle_question(self, question: str):
        """Record question and return full conversation context."""
        self.add_to_conversation("user", question)
        return self.get_conversation_history()

    def handle_response(self, response: str):
        """Append assistant response to conversation history."""
        self.add_to_conversation("assistant", response)

    def append_text(self, text, scroll_to_end=False):
        """Append text to the chat view."""
        if not self.view:
            return

        self.view.set_read_only(False)
        self.view.run_command(
            "append",
            {
                "characters": text,
                "force": True,
                "scroll_to_end": scroll_to_end,
            },
        )
        self.view.set_read_only(True)

    def focus(self):
        """Focus the chat view."""
        if self.view and self.view.window():
            self.view.window().focus_view(self.view)

    def get_size(self):
        """Return the size of the chat view content."""
        return self.view.size() if self.view else 0

    def clear(self):
        """Clear the chat view content and buttons."""
        if self.view:
            self.view.set_read_only(False)
            self.view.run_command("select_all")
            self.view.run_command("right_delete")
            self.view.set_read_only(True)
            self.view.settings().set("claudette_conversation_json", "[]")
            self.clear_buttons()

    def clear_buttons(self):
        """Clear all existing code block copy buttons for the current view."""
        if self.view:
            view_id = self.view.id()
            if view_id in self.phantom_sets:
                self.phantom_sets[view_id].update([])
            if view_id in self.existing_button_positions:
                self.existing_button_positions[view_id].clear()

    def on_streaming_complete(self) -> None:
        """Finalize code blocks and copy buttons after streaming."""
        if not self.view:
            return

        self.validate_and_fix_code_blocks()

        phantom_set = self.get_phantom_set(self.view)
        button_positions = self.get_button_positions(self.view)

        content = self.view.substr(sublime.Region(0, self.view.size()))
        code_blocks = self.find_code_blocks(content)

        phantoms = []
        new_positions: Set[int] = set()

        # Handle existing phantoms
        for phantom in phantom_set.phantoms:
            if phantom.region.end() in button_positions:
                phantoms.append(phantom)
                new_positions.add(phantom.region.end())

        # Add new phantoms
        for block in code_blocks:
            if block.end_pos not in new_positions:
                region = sublime.Region(block.end_pos, block.end_pos)
                escaped_code = self.escape_html(block.content)

                button_html = self.create_button_html(escaped_code)

                phantom = sublime.Phantom(
                    region,
                    button_html,
                    sublime.LAYOUT_BLOCK,
                    lambda href, code=block.content: self.handle_copy(code),
                )
                phantoms.append(phantom)
                new_positions.add(block.end_pos)

        # Update the button positions for this view
        self.existing_button_positions[self.view.id()] = new_positions
        if phantoms:
            phantom_set.update(phantoms)

    def handle_copy(self, code):
        """Copy code to clipboard when button is clicked."""
        try:
            sublime.set_clipboard(code)
            sublime.status_message("Code copied to clipboard")
        except Exception as e:
            print(f"{PLUGIN_NAME} Error copying to clipboard: {str(e)}")
            sublime.status_message("Error copying code to clipboard")

    def add_select_model_button(self, position):
        """Add a button that opens the select model panel."""
        if not self.view:
            return

        phantom_set = self.get_phantom_set(self.view)
        button_positions = self.get_button_positions(self.view)
        button_html = (
            '<div class="code-block-button">'
            '<a class="copy-button" href="select_model">Select Model</a>'
            "</div>"
        )

        region = sublime.Region(position, position)
        phantom = sublime.Phantom(
            region,
            button_html,
            sublime.LAYOUT_BLOCK,
            lambda href: self.handle_select_model(),
        )

        # Get existing phantoms and add the new one
        existing_phantoms = list(phantom_set.phantoms)
        existing_phantoms.append(phantom)
        phantom_set.update(existing_phantoms)

        # Track position so it survives code-block phantom updates
        button_positions.add(position)

        # Move cursor after the button. Phantoms don't take space; append
        # newline and place the caret there.
        self.view.set_read_only(False)
        self.view.run_command(
            "append",
            {"characters": "\n", "force": True, "scroll_to_end": True},
        )
        end_point = self.view.size()
        self.view.sel().clear()
        self.view.sel().add(sublime.Region(end_point, end_point))
        self.view.set_read_only(True)

    def handle_select_model(self):
        """Open the select model panel when button is clicked."""
        try:
            if self.window:
                self.window.run_command("claudette_select_model_panel")
        except Exception as e:
            print(f"{PLUGIN_NAME} Error opening select model panel: {str(e)}")
            sublime.status_message("Error opening select model panel")

    def find_code_blocks(self, content: str) -> List[ClaudetteCodeBlock]:
        """Find all fenced code blocks in the content (stateful Markdown
        parser).
        """
        return find_fenced_code_blocks(content)

    def validate_and_fix_code_blocks(self) -> None:
        """Append closing fence lines for any still-open fenced block (e.g.
        truncated stream).

        Matches find_fenced_code_blocks rules: backtick and tilde fences,
        correct close length. Orphan closing fences are left unchanged
        (harmless for the parser).
        """
        if not self.view:
            return

        content = self.view.substr(sublime.Region(0, self.view.size()))
        suffix = unclosed_fence_suffix_to_append(content)
        if not suffix:
            return

        self.view.set_read_only(False)
        self.view.run_command(
            "append",
            {
                "characters": suffix,
                "force": True,
                "scroll_to_end": True,
            },
        )
        self.view.set_read_only(True)

    @staticmethod
    def escape_html(text: str) -> str:
        """Safely escape HTML special characters."""
        return (
            text.replace("&", "&amp;")
            .replace('"', "&quot;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    def create_button_html(self, code: str) -> str:
        """Create HTML for the copy button with optional language indicator."""
        return (
            '<div class="code-block-button">'
            f'<a class="copy-button" href="copy:{code}">Copy</a></div>'
        )

    def _clear_view_resources(self, view_id):
        """Clear phantoms and per-view state for a single view id."""
        if view_id in self.phantom_sets:
            self.phantom_sets[view_id].update([])
            del self.phantom_sets[view_id]
        if view_id in self.existing_button_positions:
            del self.existing_button_positions[view_id]
        if view_id in self._tool_status_phantom_sets:
            self._tool_status_phantom_sets[view_id].update([])
            del self._tool_status_phantom_sets[view_id]
        self._tool_status_active.pop(view_id, None)
        self._tool_status_spinner_index.pop(view_id, None)
        self._tool_status_message.pop(view_id, None)

    def start_request(self) -> CancellationToken:
        """Create and store a new cancellation token for the active request."""
        self._active_cancellation_token = CancellationToken()
        return self._active_cancellation_token

    def cancel_request(self) -> bool:
        """Cancel the active request if one exists. Returns True if cancelled."""
        if self._active_cancellation_token:
            self._active_cancellation_token.cancel()
            return True
        return False

    def clear_request(self):
        """Clear the active cancellation token when request completes."""
        self._active_cancellation_token = None

    def has_active_request(self) -> bool:
        """Check if there is an active request that can be cancelled."""
        return (
            self._active_cancellation_token is not None
            and not self._active_cancellation_token.is_cancelled()
        )

    def destroy(self):
        """Clean up the chat view and associated resources."""
        # Cancel any active request before cleanup
        self.cancel_request()
        self.clear_request()

        if self.view:
            self._clear_view_resources(self.view.id())

        if self.window:
            window_id = self.window.id()
            if window_id in self._instances:
                del self._instances[window_id]

        self.view = None
