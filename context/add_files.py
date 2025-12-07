import sublime
import sublime_plugin
import os
from pathlib import Path
from .file_handler import ClaudetteFileHandler
from ..utils import claudette_chat_status_message
from typing import List, Set

class ClaudetteGitignoreParser:
    def __init__(self, root_path: str):
        self.root_path = Path(root_path)
        self.ignore_patterns: Set[str] = {
            '.git/',           # Always ignore .git directory
            '.gitignore',      # Always ignore .gitignore files
            '.git',            # For when .git is referenced without trailing slash
        }
        self.load_gitignore()

    def load_gitignore(self):
        """Load .gitignore patterns from the root directory and parent directories."""
        current_dir = self.root_path
        while current_dir.parent != current_dir:  # Stop at root directory
            gitignore_path = current_dir / '.gitignore'
            if gitignore_path.is_file():
                with open(gitignore_path, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#'):
                            self.ignore_patterns.add(line)
            current_dir = current_dir.parent

    def should_ignore(self, path: str, allow_git_files: bool = False) -> bool:
        """
        Check if a file should be ignored based on .gitignore patterns.

        Args:
            path: The path to check
            allow_git_files: If True, git-related files won't be automatically ignored
        """
        try:
            rel_path = str(Path(path).relative_to(self.root_path))

            # Check if path contains .git directory
            if not allow_git_files and '.git' in Path(rel_path).parts:
                return True

            for pattern in self.ignore_patterns:
                # Skip git-related patterns if allowing git files
                if allow_git_files and pattern in {'.git/', '.gitignore', '.git'}:
                    continue

                # Handle patterns with leading slash
                if pattern.startswith('/'):
                    # Remove leading slash and compare from root
                    clean_pattern = pattern[1:]
                    if rel_path == clean_pattern or rel_path.startswith(f"{clean_pattern}/"):
                        return True
                    continue

                # Handle directory patterns
                if pattern.endswith('/'):
                    if any(part == pattern[:-1] for part in Path(rel_path).parts):
                        return True
                    continue

                # Handle wildcards
                if '*' in pattern:
                    import fnmatch
                    if fnmatch.fnmatch(rel_path, pattern):
                        return True
                    # Also check with leading slash for root-level matches
                    if fnmatch.fnmatch('/' + rel_path, pattern):
                        return True
                    continue

                # Handle exact matches (both with and without leading slash)
                if (rel_path == pattern or
                    rel_path.startswith(f"{pattern}/") or
                    rel_path == pattern.lstrip('/') or
                    rel_path.startswith(f"{pattern.lstrip('/')}/")
                ):
                    return True

            return False
        except ValueError:
            # Handle case where path is not relative to root_path
            return False

class ClaudetteContextAddFilesCommand(sublime_plugin.WindowCommand):
    def run(self, paths=None):
        if not paths:
            return

        chat_view = self.get_chat_view()
        if not chat_view:
            return

        file_handler = ClaudetteFileHandler()
        file_handler.files = chat_view.settings().get('claudette_context_files', {})

        if isinstance(paths, str):
            paths = [paths]

        dirs_count = 0
        files_count = 0
        ignored_count = 0

        expanded_paths: List[str] = []
        for path in paths:
            if os.path.isdir(path):
                dirs_count += 1
                gitignore = ClaudetteGitignoreParser(path)

                for root, dirs, files in os.walk(path):
                    # Skip .git directories
                    if '.git' in dirs:
                        dirs.remove('.git')
                        ignored_count += 1

                    for file in files:
                        full_path = os.path.join(root, file)
                        if gitignore.should_ignore(full_path, allow_git_files=False):
                            ignored_count += 1
                            continue
                        expanded_paths.append(full_path)
            else:
                files_count += 1
                # For individual files, always allow git-related files
                parent_dir = Path(path).parent
                gitignore = ClaudetteGitignoreParser(str(parent_dir))

                if not gitignore.should_ignore(path, allow_git_files=True):
                    expanded_paths.append(path)
                else:
                    ignored_count += 1

        result = file_handler.process_paths(expanded_paths)

        chat_view.settings().set('claudette_context_files', result['files'])

        message_parts = []
        if dirs_count > 0:
            message_parts.append(f"{dirs_count} {'directory' if dirs_count == 1 else 'directories'}")
        if files_count > 0:
            message_parts.append(f"{files_count} {'file' if files_count == 1 else 'files'}")

        message = f"Included {' and '.join(message_parts)}"

        if result['processed_files'] > 0:
            message += f" ({result['processed_files']} total files processed)"
        if result['skipped_files'] > 0:
            message += f", skipped {result['skipped_files']} files"
        if ignored_count > 0:
            message += f", ignored {ignored_count} files (gitignore)"

        claudette_chat_status_message(self.window, message, "✅")
        sublime.status_message(message)

    def get_chat_view(self):
        for view in self.window.views():
            if (view.settings().get('claudette_is_chat_view', False) and
                view.settings().get('claudette_is_current_chat', False)):
                return view
        return None

    def is_visible(self, paths=None):
        """Controls whether the command appears in the context menu"""
        return True

    def is_enabled(self, paths=None):
        """Controls whether the command is greyed out"""
        return bool(self.get_chat_view() and paths)
