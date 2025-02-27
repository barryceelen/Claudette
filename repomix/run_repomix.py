import sublime
import sublime_plugin
import os
import subprocess
import re
import json
import tempfile
from ..constants import PLUGIN_NAME, SETTINGS_FILE
from ..utils import claudette_chat_status_message
from ..chat.ask_question import ClaudetteAskQuestionCommand

class ClaudetteRunRepomixCommand(sublime_plugin.WindowCommand):
    def is_visible(self):
        # Show if there's either a folder open or files selected in sidebar
        if hasattr(self, 'paths'):
            return bool(self.paths)
        return bool(self.window.folders())

    def is_enabled(self):
        return self.is_visible()

    def get_active_chat_view(self):
        """Get the current chat view or create a new one"""
        # First try to find an existing active chat view
        existing_view = None
        for view in self.window.views():
            if (view.settings().get('claudette_is_chat_view', False) and
                view.settings().get('claudette_is_current_chat', False)):
                existing_view = view
                return existing_view, False

        # If no active chat view found, create a new one
        ask_cmd = ClaudetteAskQuestionCommand(self.window.active_view())
        ask_cmd.load_settings()
        new_view = ask_cmd.create_chat_panel(force_new=True)
        return new_view, True

    def extract_token_count(self, output):
        """Extract token count from repomix output using regex."""
        # Look for patterns like "Total Tokens: 12,868 tokens"
        # First capture the number including any commas
        match = re.search(r'(?:token count|tokens):\s*([\d,]+)', output, re.IGNORECASE)
        if match:
            # Remove commas and convert to int
            token_count = match.group(1).replace(',', '')
            return int(token_count)
        return None

    def format_include_paths(self, paths, base_dir):
        """Format file paths into repomix include pattern."""
        if not paths:
            return None

        # Convert absolute paths to relative paths from base_dir
        relative_paths = []
        for path in paths:
            if os.path.isfile(path):
                rel_path = os.path.relpath(path, base_dir)
                # Replace backslashes with forward slashes for consistency
                rel_path = rel_path.replace('\\', '/')
                relative_paths.append(rel_path)

        if relative_paths:
            # Join all paths with commas
            return ','.join(relative_paths)
        return None

    def run(self, paths=None):
        # Get or create chat view first, and track if it's new
        chat_view, is_new_view = self.get_active_chat_view()
        if not chat_view:
            sublime.error_message("Could not create chat view")
            return

        self.paths = paths
        settings = sublime.load_settings(SETTINGS_FILE)
        repomix_settings = settings.get('repomix', {})
        executable = repomix_settings.get('executable', 'repomix')

        # Use selected paths if provided, otherwise use first folder
        target_paths = paths if paths else [self.window.folders()[0]]
        if not target_paths:
            sublime.error_message("Please select a folder or files in the sidebar")
            return

        # Get the working directory (use first folder if files selected)
        if paths and all(os.path.isfile(p) for p in paths):
            # All selected items are files, use their common parent directory
            target_folder = os.path.dirname(paths[0])
        else:
            # At least one folder selected, use the first folder
            target_folder = next((p for p in target_paths if os.path.isdir(p)), None)
            if not target_folder:
                sublime.error_message("No valid folder found")
                return

        # Look for config file in project root
        config_file = None
        project_root = self._find_project_root(target_folder)

        if project_root:
            potential_config = os.path.join(project_root, 'repomix.config.json')
            if os.path.exists(potential_config):
                config_file = potential_config

        # Create temporary output file in Sublime's cache directory
        cache_dir = os.path.join(sublime.cache_path(), PLUGIN_NAME)
        if not os.path.exists(cache_dir):
            os.makedirs(cache_dir)

        output_file = os.path.join(cache_dir, f'repomix-output-{os.urandom(4).hex()}.xml')

        try:
            cmd = [executable]

            if config_file:
                cmd.extend(['--config', config_file])

            # Add output file flag
            cmd.extend(['--output', output_file])

            # Add include pattern for selected files
            if paths and any(os.path.isfile(p) for p in paths):
                include_pattern = self.format_include_paths(paths, target_folder)
                if include_pattern:
                    cmd.extend(['--include', include_pattern])

            # Print command being run
            print(f"\n{PLUGIN_NAME}: Running command:", ' '.join(cmd))
            print(f"{PLUGIN_NAME}: Working directory:", target_folder)

            # Run repomix
            process = subprocess.Popen(
                cmd,
                cwd=target_folder,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=True if os.name == 'nt' else False,
                text=True
            )

            stdout, stderr = process.communicate()

            if process.returncode != 0:
                print(f"Repomix Error: {stderr}")
                sublime.status_message('Repomix error.')
                return

            # Read the output file
            if not os.path.exists(output_file):
                raise FileNotFoundError(f"Output file not found: {output_file}")

            with open(output_file, 'r', encoding='utf-8') as f:
                file_content = f.read()

            # Store the file content in the chat view's settings
            chat_view.settings().set('claudette_repomix', file_content)

            # Extract token count from stdout and create status message
            token_count = self.extract_token_count(stdout)
            status_msg = "Repomix completed successfully"
            if token_count is not None:
                status_msg += f" ({token_count:,} tokens)"
                chat_view.settings().set('claudette_repomix_tokens', token_count)

            # Show success message in chat view
            claudette_chat_status_message(self.window, status_msg, prefix="✅")

            # Print success message to console
            print(f"\n{PLUGIN_NAME}: {status_msg}")
            print("\nOutput stored in chat view settings")

            sublime.status_message(f"{PLUGIN_NAME}: {status_msg}")

            # Only open input panel if this is a new view
            if is_new_view:
                def show_input_panel():
                    self.window.run_command('claudette_ask_question')

                # Slight delay to ensure status messages are visible
                sublime.set_timeout(show_input_panel, 100)

        except FileNotFoundError as e:
            if 'repomix' in str(e):
                error_msg = (
                    f"Could not find repomix executable. Please ensure repomix is installed and "
                    f"configured correctly in Package Settings > Claudette > Settings"
                )
            else:
                error_msg = str(e)
            print(f"\n{PLUGIN_NAME} Error:", error_msg)
            sublime.error_message(error_msg)
        except Exception as e:
            print(f"\n{PLUGIN_NAME} Error:", str(e))
            sublime.error_message(f"Error running repomix: {str(e)}")
        finally:
            # Clean up output file
            if os.path.exists(output_file):
                try:
                    os.remove(output_file)
                except Exception as e:
                    print(f"{PLUGIN_NAME} Warning: Could not remove output file: {str(e)}")

    def _find_project_root(self, start_path):
        """Walk up directories looking for repomix config files"""
        current = start_path
        while True:
            if os.path.exists(os.path.join(current, 'repomix.config.json')):
                return current

            parent = os.path.dirname(current)
            if parent == current:
                return None
            current = parent
