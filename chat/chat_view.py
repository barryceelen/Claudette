import json
import sublime
import sublime_plugin
import re
from typing import List, Set
from dataclasses import dataclass
from ..constants import PLUGIN_NAME

@dataclass
class ClaudetteCodeBlock:
    """Represents a code block found in the chat content."""
    content: str
    start_pos: int
    end_pos: int
    language: str

class ClaudetteChatViewListener(sublime_plugin.ViewEventListener):
    """Event listener specifically for chat views."""

    @classmethod
    def is_applicable(cls, settings):
        """Only attach this listener to chat views."""
        return settings.get('claudette_is_chat_view', False)

    def on_text_command(self, command_name, args):
        """Handle enter key."""
        if command_name == "insert" and args.get("characters") == "\n":
            try:
                window = self.view.window()
                if window:
                    window.run_command('claudette_ask_question')
                    return ('noop', None)
            except Exception as e:
                sublime.status_message(f"Claudette error: {str(e)}")
        return None

class ClaudetteChatView:
    """Manages chat views for the Claudette plugin."""

    _instances = {}

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

    def __init__(self, window, settings):
        """Initialize the chat view manager."""
        self.window = window
        self.settings = settings
        self.view = None
        self.phantom_sets = {}  # Store phantom sets per view
        self.existing_button_positions = {}  # Store positions per view

    def create_or_get_view(self):
        """Create a new chat view or return an existing one."""
        try:
            # First check for current chat view in this window
            for view in self.window.views():
                if (view.settings().get('claudette_is_chat_view', False) and
                    view.settings().get('claudette_is_current_chat', False)):
                    self.view = view
                    return self.view

            # If no current chat view found, use the first chat view
            for view in self.window.views():
                if view.settings().get('claudette_is_chat_view', False):
                    self.view = view
                    # Set this view as current since none was marked as current
                    view.settings().set('claudette_is_current_chat', True)
                    return self.view

            # Create new chat view if none exists in this window
            self.view = self.window.new_file()
            if not self.view:
                print(f"{PLUGIN_NAME} Error: Could not create new file")
                sublime.error_message(f"{PLUGIN_NAME} Error: Could not create new file")
                return None

            chat_settings = self.settings.get('chat', {})
            line_numbers = chat_settings.get('line_numbers', False)
            rulers = chat_settings.get('rulers', False)
            set_scratch = chat_settings.get('set_scratch', True)

            self.view.set_name("Claude Chat")
            self.view.set_scratch(set_scratch)
            self.view.assign_syntax('Packages/Markdown/Markdown.sublime-syntax')
            self.view.set_read_only(True)
            self.view.settings().set("line_numbers", line_numbers)
            self.view.settings().set("rulers", rulers)
            self.view.settings().set("claudette_is_chat_view", True)
            self.view.settings().set("claudette_is_current_chat", True)
            self.view.settings().set("claudette_conversation", [])

            return self.view

        except Exception as e:
            print(f"{PLUGIN_NAME} Error creating chat panel: {str(e)}")
            sublime.error_message(f"{PLUGIN_NAME} Error: Could not create chat panel")
            return None

    def get_phantom_set(self, view):
        """Get or create a phantom set for the specific view."""
        view_id = view.id()
        if view_id not in self.phantom_sets:
            self.phantom_sets[view_id] = sublime.PhantomSet(view, f"code_block_buttons_{view_id}")
        return self.phantom_sets[view_id]

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

        conversation_json = self.view.settings().get('claudette_conversation_json', '[]')
        try:
            return json.loads(conversation_json)
        except json.JSONDecodeError:
            print(f"{PLUGIN_NAME} Error: Could not decode conversation history")
            return []

    def add_to_conversation(self, role: str, content: str):
        """Add a new message to the conversation history."""
        if not self.view:
            return

        conversation = self.get_conversation_history()
        conversation.append({
            "role": role,
            "content": content
        })

        try:
            conversation_json = json.dumps(conversation)
            self.view.settings().set('claudette_conversation_json', conversation_json)
        except json.JSONEncodeError:
            print(f"{PLUGIN_NAME} Error: Could not encode conversation history")

    def handle_question(self, question: str):
        """Handle a new question and return the complete conversation context."""
        self.add_to_conversation("user", question)
        return self.get_conversation_history()

    def handle_response(self, response: str):
        """Handle the Claude response by adding it to the conversation history."""
        self.add_to_conversation("assistant", response)

    def append_text(self, text, scroll_to_end=True):
        """Append text to the chat view."""
        if not self.view:
            return

        self.view.set_read_only(False)
        self.view.run_command('append', {
            'characters': text,
            'force': True,
            'scroll_to_end': scroll_to_end
        })
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
            self.view.run_command('select_all')
            self.view.run_command('right_delete')
            self.view.set_read_only(True)
            self.view.settings().set('claudette_conversation_json', '[]')
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
        """Handle code blocks and phantom buttons when streaming is complete."""
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
                    lambda href, code=block.content: self.handle_copy(code)
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

    def find_code_blocks(self, content: str) -> List[ClaudetteCodeBlock]:
        """Find all code blocks in the content."""
        blocks = []
        pattern = r"```([\w+]*)\n(.*?)\n```"

        for match in re.finditer(pattern, content, re.DOTALL):
            language = match.group(1).strip()
            content = match.group(2).strip()
            blocks.append(ClaudetteCodeBlock(
                content=content,
                start_pos=match.start(),
                end_pos=match.end(),
                language=language
            ))
        return blocks

    def validate_and_fix_code_blocks(self) -> None:
        """Validate and fix unclosed code blocks."""
        if not self.view:
            return

        content = self.view.substr(sublime.Region(0, self.view.size()))
        lines = content.split('\n')
        stack = []
        fixes_needed = []

        for i, line in enumerate(lines):
            stripped = line.strip()

            if stripped.startswith('```'):
                if len(stripped) > 3:  # Opening block with language
                    stack.append((i, stripped[3:].strip()))
                elif stripped == '```':
                    if stack:  # Proper closing
                        stack.pop()
                    else:  # Orphaned closing marker
                        fixes_needed.append((i, 'remove'))

        # Handle unclosed blocks
        if stack:
            self.view.set_read_only(False)
            for _, language in stack:
                self.view.run_command('append', {
                    'characters': '\n```',
                    'force': True,
                    'scroll_to_end': True
                })
            self.view.set_read_only(True)

    @staticmethod
    def escape_html(text: str) -> str:
        """Safely escape HTML special characters."""
        return (text
                .replace('&', '&amp;')
                .replace('"', '&quot;')
                .replace('<', '&lt;')
                .replace('>', '&gt;'))

    def create_button_html(self, code: str) -> str:
        """Create HTML for the copy button with optional language indicator."""
        return f'''<div class="code-block-button"><a class="copy-button" href="copy:{code}">Copy</a></div>'''

    def destroy(self):
        """Clean up the chat view and associated resources."""
        if self.view:
            view_id = self.view.id()
            if view_id in self.phantom_sets:
                self.phantom_sets[view_id].update([])
                del self.phantom_sets[view_id]
            if view_id in self.existing_button_positions:
                del self.existing_button_positions[view_id]

        if self.window:
            window_id = self.window.id()
            if window_id in self._instances:
                del self._instances[window_id]

        self.view = None
