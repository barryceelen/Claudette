import sublime
import os
from pathlib import Path
from .constants import SETTINGS_FILE

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
        message = f"\n\n{prefix + ' ' if prefix else ''}{message}\n"
    else:
        message = f"{prefix + ' ' if prefix else ''}{message}\n"

    current_chat_view.set_read_only(False)
    current_chat_view.run_command('append', {
        'characters': message,
        'force': True,
        'scroll_to_end': True
    })

    # Move cursor to the end of the view
    end_point = current_chat_view.size()
    current_chat_view.sel().clear()
    current_chat_view.sel().add(sublime.Region(end_point, end_point))

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

def claudette_get_valid_api_keys():
    """
    Get a list of valid API keys from settings.

    Returns:
        list: List of dictionaries with 'api_key' and 'name' keys
    """
    settings = sublime.load_settings(SETTINGS_FILE)

    api_keys = settings.get('api_keys', [])
    valid_api_keys = []
    untitled_count = 0

    for api_key in api_keys:
        if isinstance(api_key, dict) and api_key.get('api_key'):
            name = api_key.get('name')
            if not name:
                if untitled_count == 0:
                    name = "Untitled"
                else:
                    name = f"Untitled {untitled_count}"
                untitled_count += 1

            valid_api_keys.append({
                'api_key': api_key,
                'name': name
            })

    # If no valid keys found, check for the legacy api_key setting
    if not valid_api_keys and settings.has('api_key'):
        legacy_key = settings.get('api_key')
        if legacy_key:
            new_api_key = {
                'name': 'Default',
                'key': legacy_key
            }

            valid_api_keys.append({
                'api_key': new_api_key,
                'name': 'Default'
            })

    return valid_api_keys

def claudette_get_api_key():
    """
    Get the currently active API key.

    Returns:
        dict or None: The active API key as a dictionary with 'key' and 'name' fields,
                      or None if no valid key is available
    """

    settings = sublime.load_settings(SETTINGS_FILE)
    valid_api_keys = claudette_get_valid_api_keys()

    if not valid_api_keys:
        return None

    # Try to get the API key from the current index
    current_index = settings.get('default_api_key_index', 0)
    api_keys = settings.get('api_keys', [])

    # If there's a valid current index, find the corresponding key in our valid keys
    if api_keys and 0 <= current_index < len(api_keys):
        current_key = api_keys[current_index]
        for key_info in valid_api_keys:
            if key_info['api_key'] == current_key:
                return current_key

    # If we couldn't find the current key or the index is invalid,
    # return the first valid key
    return valid_api_keys[0]['api_key']
