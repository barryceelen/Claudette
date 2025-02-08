import mimetypes
from pathlib import Path

def estimate_api_tokens(text):
    """Estimate Claude API tokens based on character count (rough approximation)."""
    return len(text) // 4

def is_text_file(file_path):
    """Check if a file is a text file using mimetype."""
    mime_type, _ = mimetypes.guess_type(file_path)
    if mime_type is None:
        text_extensions = {'.txt', '.py', '.js', '.html', '.css', '.json', '.md',
                         '.csv', '.xml', '.yml', '.yaml', '.ini', '.conf', '.sh',
                         '.rb', '.php', '.java', '.cpp', '.h', '.c', '.rs', '.go'}
        return Path(file_path).suffix.lower() in text_extensions
    return mime_type.startswith('text/')
