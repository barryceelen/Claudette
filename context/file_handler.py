import os
from ..utils import claudette_estimate_api_tokens, claudette_is_text_file

class ClaudetteFileHandler:
    def __init__(self):
        self.files = {}

    def process_file(self, file_path, root_folder):
        """Process a single file and extract its symbols using Sublime's indexing."""
        try:

            is_text, encoding, reason = claudette_is_text_file(file_path)

            if not is_text:
                print(reason)
                return None

            relative_path = os.path.relpath(file_path, root_folder)

            if relative_path in self.files:
                print(f"Updating file in context: {file_path}")

            file_content = ''

            with open(file_path, 'r', encoding='utf-8') as f:
                file_content = f.read()

            self.files[relative_path] = {
                'content': file_content,
                'api_tokens': claudette_estimate_api_tokens(file_content),
                'absolute_path': file_path
            }

            return relative_path

        except Exception as e:
            print(f"Error processing file {file_path}: {str(e)}")
            return None

    def process_paths(self, paths):
        """Process multiple files or folders."""
        if len(paths) == 1:
            root_folder = os.path.dirname(paths[0]) if os.path.isfile(paths[0]) else paths[0]
        else:
            root_folder = os.path.commonpath(paths)

        processed_files = 0
        skipped_files = 0

        for path in paths:
            if os.path.isfile(path):
                if self.process_file(path, root_folder):
                    processed_files += 1
                else:
                    skipped_files += 1
            elif os.path.isdir(path):
                for root, _, files in os.walk(path):
                    for file in files:
                        file_path = os.path.join(root, file)
                        if self.process_file(file_path, root_folder):
                            processed_files += 1
                        else:
                            skipped_files += 1

        return {
            'files': self.files,
            'processed_files': processed_files,
            'skipped_files': skipped_files
        }
