"""API error parsing and model-not-found handling."""

import json


def parse_api_error(http_error):
    """Parse an API HTTP error response into error type and message.

    Args:
        http_error: A urllib.error.HTTPError instance.

    Returns:
        tuple: (error_type: str, error_message: str)
    """
    error_content = http_error.read().decode('utf-8')
    error_type = ''
    error_message = ''
    try:
        err_data = json.loads(error_content)
        error_type = err_data.get('error', {}).get('type', '')
        error_message = err_data.get('error', {}).get('message', '')
    except (json.JSONDecodeError, AttributeError, KeyError):
        pass

    if not error_message:
        error_message = str(http_error)

    return error_type, error_message


def is_model_not_found_error(http_code, error_type, error_message):
    """Check whether an API error indicates an unknown/invalid model.

    Args:
        http_code: The HTTP status code (e.g. 400, 404).
        error_type: The error type string from the API response.
        error_message: The error message string from the API response.

    Returns:
        bool
    """
    return (
        http_code in (400, 404)
        and error_type in ('invalid_request_error', 'not_found_error')
        and error_message.startswith('model:')
    )


def handle_model_not_found(error_message, window, settings, fallback_callback):
    """Display a model-not-found error with a 'Select Model' button in the chat view.

    If the window is available, displays the error in the chat view with an
    interactive button. Otherwise falls back to calling fallback_callback
    with a plain-text error string.

    Args:
        error_message: The raw error message from the API (starts with "model:").
        window: The Sublime Text window (may be None).
        settings: The plugin settings object.
        fallback_callback: Called with a plain error string if window is unavailable.
    """
    import sublime
    from ..utils import claudette_chat_status_message

    if error_message.startswith('model:'):
        error_model = error_message[len('model:'):].strip()
    else:
        error_model = error_message.strip()
    display_message = 'The "{0}" model does not exist.'.format(error_model)

    if window:
        from ..chat.chat_view import ClaudetteChatView

        message_end_position = claudette_chat_status_message(
            window,
            display_message,
            "⚠️"
        )
        if message_end_position >= 0:
            try:
                chat_view_instance = ClaudetteChatView.get_instance(window, settings)
                if chat_view_instance:
                    chat_view_instance.add_select_model_button(message_end_position)
            except Exception as ex:
                print("Error adding select model button: {0}".format(str(ex)))
    else:
        fallback_callback(
            '[Error] {0} Please update your model via '
            'Settings > Package Settings > Claudette > Select Model.'.format(display_message)
        )
