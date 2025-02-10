import os
from pathlib import Path

def claudette_chat_status_message(window, message: str, prefix: str = "ℹ️") -> None:
    """
    Display a status message in the active chat view.

    Args:
        window: The Sublime Text window
        message (str): The status message to display
        prefix (str, optional): Icon or text prefix for the message. Defaults to "ℹ️"
    """
    if not window:
        return

    # Find the active chat view
    current_chat_view = None
    for view in window.views():
        if (view.settings().get('claudette_is_chat_view', False) and
            view.settings().get('claudette_is_current_chat', False)):
            current_chat_view = view
            break

    if not current_chat_view:
        return

    if current_chat_view.size() > 0:
        message = f"\n\n{prefix} {message}\n"
    else:
        message = f"{prefix} {message}\n"

    current_chat_view.set_read_only(False)
    current_chat_view.run_command('append', {
        'characters': message,
        'force': True,
        'scroll_to_end': True
    })
    current_chat_view.set_read_only(True)

def claudette_estimate_api_tokens(text):
    """Estimate Claude API tokens based on character count (rough approximation)."""
    return len(text) // 4

def claudette_detect_encoding(sample):
    """
    Detect file encoding using BOMs and content analysis.
    Similar to how Sublime Text handles encodings.
    """
    # Check for BOMs
    if sample.startswith(b'\xEF\xBB\xBF'):
        return 'utf-8-sig'
    elif sample.startswith(b'\xFE\xFF'):
        return 'utf-16be'
    elif sample.startswith(b'\xFF\xFE'):
        return 'utf-16le'
    elif sample.startswith(b'\x00\x00\xFE\xFF'):
        return 'utf-32be'
    elif sample.startswith(b'\xFF\xFE\x00\x00'):
        return 'utf-32le'

    # Try UTF-8
    try:
        sample.decode('utf-8')
        return 'utf-8'
    except UnicodeDecodeError:
        return 'latin-1'  # Fallback encoding

def claudette_is_text_file(file_path, sample_size=4096, max_size=1024*1024*10):
    """
    More complete implementation of Sublime Text's text file detection.

    Args:
        file_path: Path to the file to check
        sample_size: Number of bytes to sample
        max_size: Maximum file size to consider (10MB default)

    Returns:
        tuple: (is_text, encoding, reason)
    """
    try:
        file_size = os.path.getsize(file_path)

        # Size check
        if file_size > max_size:
            return False, None, "File too large"

        # Empty file check
        if file_size == 0:
            return True, 'utf-8', "Empty file"

        with open(file_path, 'rb') as f:
            sample = f.read(min(sample_size, file_size))

        # Binary check
        if b'\x00' in sample:
            null_percentage = sample.count(b'\x00') / len(sample)
            if null_percentage > 0.01:  # More than 1% nulls
                return False, None, "Binary file (contains NULL bytes)"

        # Encoding detection
        encoding = claudette_detect_encoding(sample)

        # Verification check
        try:
            with open(file_path, 'r', encoding=encoding) as f:
                f.read(sample_size)
            return True, encoding, "Valid text file"
        except UnicodeDecodeError:
            return False, None, "Unable to decode with detected encoding"

    except IOError as e:
        return False, None, f"IO Error: {str(e)}"
