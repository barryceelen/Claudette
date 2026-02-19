import sublime
import os
import json
import time
import urllib.request
import urllib.error
from pathlib import Path
from .constants import (
    SETTINGS_FILE,
    OAUTH_CREDENTIALS_PATH,
    OAUTH_TOKEN_REFRESH_URL,
    OAUTH_CLIENT_ID,
    OAUTH_REFRESH_BUFFER_MS,
    AUTH_MODE_API_KEY,
    AUTH_MODE_OAUTH,
    DEFAULT_AUTH_MODE
)

def claudette_chat_status_message(window, message: str, prefix: str = "ℹ️", copy_path: str = None) -> int:
    """
    Display a status message in the active chat view.

    Args:
        window: The Sublime Text window
        message (str): The status message to display
        prefix (str, optional): Icon or text prefix for the message. Defaults to "ℹ️"
        copy_path (str, optional): If provided, adds a "Copy Path" button that copies this path to clipboard

    Returns:
        int: The end position of the message in the view, or -1 if no view was found
    """
    if not window:
        return -1

    # Find the active chat view
    current_chat_view = None
    for view in window.views():
        if (view.settings().get('claudette_is_chat_view', False) and
            view.settings().get('claudette_is_current_chat', False)):
            current_chat_view = view
            break

    if not current_chat_view:
        return -1

    if current_chat_view.size() > 0:
        view_size = current_chat_view.size()
        last_chars = current_chat_view.substr(sublime.Region(max(0, view_size - 2), view_size))
        if last_chars == '\n\n':
            # Content already ends with two newlines, don't add any newline before message
            formatted_message = f"{prefix + ' ' if prefix else ''}{message}"
        elif last_chars.endswith('\n'):
            # Content ends with one newline, add one more for spacing
            formatted_message = f"\n{prefix + ' ' if prefix else ''}{message}"
        else:
            # Content doesn't end with newline, add two for spacing
            formatted_message = f"\n\n{prefix + ' ' if prefix else ''}{message}"
    else:
        formatted_message = f"{prefix + ' ' if prefix else ''}{message}"

    current_chat_view.set_read_only(False)
    current_chat_view.run_command('append', {
        'characters': formatted_message,
        'force': True,
        'scroll_to_end': True
    })

    # Add "Copy Path" button as phantom if path is provided
    if copy_path:
        button_position = current_chat_view.size()
        _add_copy_path_phantom(current_chat_view, button_position, copy_path)

    # Add trailing newline
    current_chat_view.run_command('append', {
        'characters': '\n',
        'force': True,
        'scroll_to_end': True
    })

    end_point = current_chat_view.size()

    current_chat_view.sel().clear()
    current_chat_view.sel().add(sublime.Region(end_point, end_point))

    current_chat_view.set_read_only(True)

    return end_point


# Store phantom sets for copy path buttons per view
_copy_path_phantom_sets = {}


def _add_copy_path_phantom(view, position: int, path: str):
    """
    Add a "Copy Path" phantom button at the specified position.

    Args:
        view: The Sublime Text view
        position: The position in the view to add the phantom
        path: The path to copy when the button is clicked
    """
    view_id = view.id()

    if view_id not in _copy_path_phantom_sets:
        _copy_path_phantom_sets[view_id] = sublime.PhantomSet(view, f"copy_path_buttons_{view_id}")

    phantom_set = _copy_path_phantom_sets[view_id]

    # Escape the path for use in HTML
    escaped_path = (path
        .replace('&', '&amp;')
        .replace('"', '&quot;')
        .replace('<', '&lt;')
        .replace('>', '&gt;'))

    button_html = f''' <span class="copy-path-button" style="padding-left: 8px"><a href="copy:{escaped_path}">Copy Path</a></span>'''

    def on_navigate(href):
        if href.startswith('copy:'):
            path_to_copy = href[5:]
            sublime.set_clipboard(path_to_copy)
            sublime.status_message(f"File path copied to clipboard")

    region = sublime.Region(position, position)
    phantom = sublime.Phantom(
        region,
        button_html,
        sublime.LAYOUT_INLINE,
        on_navigate
    )

    # Get existing phantoms and add the new one
    existing_phantoms = list(phantom_set.phantoms)
    existing_phantoms.append(phantom)
    phantom_set.update(existing_phantoms)

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

def claudette_get_api_key():
    """
    Get the currently active API key.

    Returns:
        dict or None: The active API key as a dictionary with 'key' and 'name' fields,
                      or None if no valid key is available
    """
    settings = sublime.load_settings(SETTINGS_FILE)
    api_key = settings.get('api_key')

    # For string API key, return a dict format
    if isinstance(api_key, str) and api_key.strip():
        return {'key': api_key, 'name': 'Default'}

    # For dict with multiple keys, get the current one
    elif isinstance(api_key, dict) and api_key.get('keys') and isinstance(api_key['keys'], list):
        keys = api_key['keys']
        current_index = api_key.get('active_key', 0)

        # If there's a valid current index, return that key
        if isinstance(current_index, int) and 0 <= current_index < len(keys):
            key_entry = keys[current_index]
            if isinstance(key_entry, dict) and key_entry.get('key'):
                return key_entry

        # Otherwise return the first valid key
        for key_entry in keys:
            if isinstance(key_entry, dict) and key_entry.get('key'):
                return key_entry

    return None

def claudette_get_api_key_value():
    """
    Extract the API key value from the API key dictionary.

    Returns:
        str: The API key value, or an empty string if not available
    """
    api_key = claudette_get_api_key()

    if isinstance(api_key, dict):
        return api_key.get('key', '')

    return ''

def claudette_get_api_key_name():
    """
    Get the API key name from the API key dictionary.

    Returns:
        str: The API key name, 'Default' if the key has no name, or 'Undefined'
    """
    api_key = claudette_get_api_key()

    if isinstance(api_key, dict):
        return api_key.get('name', 'Default')

    return 'Undefined'


# OAuth authentication functions

def claudette_get_auth_mode():
    """Get the current authentication mode from settings. Returns 'api_key' or 'oauth'."""
    settings = sublime.load_settings(SETTINGS_FILE)
    return settings.get('auth_mode', DEFAULT_AUTH_MODE)


def claudette_get_oauth_credentials_path():
    """Get the expanded path to the OAuth credentials file."""
    return Path(os.path.expanduser(OAUTH_CREDENTIALS_PATH))


def claudette_read_oauth_credentials():
    """
    Read OAuth credentials from the credentials file.
    Returns the claudeAiOauth dict, or None if not found/invalid.
    """
    credentials_path = claudette_get_oauth_credentials_path()
    try:
        if not credentials_path.exists():
            return None
        with open(credentials_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        oauth_data = data.get('claudeAiOauth')
        if not oauth_data:
            return None
        for field in ['accessToken', 'refreshToken', 'expiresAt']:
            if field not in oauth_data:
                print("Claudette: OAuth credentials missing required field: {0}".format(field))
                return None
        return oauth_data
    except json.JSONDecodeError as e:
        print("Claudette: Error parsing OAuth credentials file: {0}".format(e))
        return None
    except IOError as e:
        print("Claudette: Error reading OAuth credentials file: {0}".format(e))
        return None


def claudette_is_oauth_token_expired(oauth_data):
    """Return True if the OAuth token is expired or expiring within the buffer period."""
    if not oauth_data or 'expiresAt' not in oauth_data:
        return True
    expires_at_ms = oauth_data['expiresAt']
    current_time_ms = int(time.time() * 1000)
    return current_time_ms >= (expires_at_ms - OAUTH_REFRESH_BUFFER_MS)


def claudette_save_oauth_credentials(oauth_data):
    """Save updated OAuth credentials back to the credentials file."""
    credentials_path = claudette_get_oauth_credentials_path()
    try:
        existing_data = {}
        if credentials_path.exists():
            with open(credentials_path, 'r', encoding='utf-8') as f:
                existing_data = json.load(f)
        existing_data['claudeAiOauth'] = oauth_data
        credentials_path.parent.mkdir(parents=True, exist_ok=True)
        with open(credentials_path, 'w', encoding='utf-8') as f:
            json.dump(existing_data, f, indent=2)
    except Exception as e:
        print("Claudette: Error saving OAuth credentials: {0}".format(e))


def claudette_refresh_oauth_token(oauth_data):
    """
    Refresh the OAuth access token using the refresh token.
    Returns updated oauth_data dict, or None on failure.
    """
    if not oauth_data or 'refreshToken' not in oauth_data:
        print("Claudette: Cannot refresh token - no refresh token available")
        return None
    try:
        refresh_data = {
            'grant_type': 'refresh_token',
            'refresh_token': oauth_data['refreshToken'],
            'client_id': OAUTH_CLIENT_ID
        }
        request_body = json.dumps(refresh_data).encode('utf-8')
        req = urllib.request.Request(
            OAUTH_TOKEN_REFRESH_URL,
            data=request_body,
            headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            response_data = json.loads(response.read().decode('utf-8'))
        new_oauth_data = oauth_data.copy()
        new_oauth_data['accessToken'] = response_data.get('access_token', oauth_data['accessToken'])
        if 'refresh_token' in response_data:
            new_oauth_data['refreshToken'] = response_data['refresh_token']
        if 'expires_in' in response_data:
            new_oauth_data['expiresAt'] = int(time.time() * 1000) + response_data['expires_in'] * 1000
        elif 'expires_at' in response_data:
            new_oauth_data['expiresAt'] = response_data['expires_at']
        claudette_save_oauth_credentials(new_oauth_data)
        print("Claudette: OAuth token refreshed successfully")
        return new_oauth_data
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8') if e.fp else ''
        print("Claudette: HTTP error refreshing OAuth token: {0} - {1}".format(e.code, error_body))
        return None
    except urllib.error.URLError as e:
        print("Claudette: Network error refreshing OAuth token: {0}".format(e))
        return None
    except Exception as e:
        print("Claudette: Error refreshing OAuth token: {0}".format(e))
        return None


def claudette_get_oauth_access_token():
    """
    Get a valid OAuth access token.
    Resolution order:
      1. CLAUDE_CODE_OAUTH_TOKEN environment variable (long-lived, cross-platform)
      2. ~/.claude/.credentials.json (short-lived, auto-refreshed, Linux only)
    Returns the access token string, or None if unavailable.
    """
    env_token = os.environ.get('CLAUDE_CODE_OAUTH_TOKEN')
    if env_token and env_token.strip():
        return env_token.strip()
    oauth_data = claudette_read_oauth_credentials()
    if not oauth_data:
        return None
    if claudette_is_oauth_token_expired(oauth_data):
        print("Claudette: OAuth token expired or expiring soon, refreshing...")
        oauth_data = claudette_refresh_oauth_token(oauth_data)
        if not oauth_data:
            return None
    return oauth_data.get('accessToken')


def claudette_get_auth_header():
    """
    Get the appropriate authentication header based on the current auth mode.
    Returns (header_name, header_value), or (None, None) if unavailable.
    """
    auth_mode = claudette_get_auth_mode()
    if auth_mode == AUTH_MODE_OAUTH:
        access_token = claudette_get_oauth_access_token()
        if access_token:
            return ('Authorization', 'Bearer {0}'.format(access_token))
        return (None, None)
    else:
        api_key = claudette_get_api_key_value()
        if api_key:
            return ('x-api-key', api_key)
        return (None, None)


def claudette_validate_oauth_setup():
    """
    Validate that OAuth is properly set up.
    Returns (is_valid, error_message).
    """
    env_token = os.environ.get('CLAUDE_CODE_OAUTH_TOKEN')
    if env_token and env_token.strip():
        return (True, None)
    credentials_path = claudette_get_oauth_credentials_path()
    if not credentials_path.exists():
        return (False, "OAuth token not found. Set the CLAUDE_CODE_OAUTH_TOKEN environment variable, or run 'claude login' in Claude Code CLI.")
    oauth_data = claudette_read_oauth_credentials()
    if not oauth_data:
        return (False, "Invalid OAuth credentials format. Set the CLAUDE_CODE_OAUTH_TOKEN environment variable, or run 'claude login' in Claude Code CLI.")
    if not oauth_data.get('accessToken'):
        return (False, "No access token found in OAuth credentials.")
    if not oauth_data.get('refreshToken'):
        return (False, "No refresh token found in OAuth credentials. Please run 'claude login' again.")
    return (True, None)
