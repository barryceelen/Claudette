ANTHROPIC_VERSION = "2023-06-01"

# OAuth configuration
OAUTH_CREDENTIALS_PATH = "~/.claude/.credentials.json"
OAUTH_TOKEN_REFRESH_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_REFRESH_BUFFER_MS = 5 * 60 * 1000  # Refresh 5 min before expiry
OAUTH_BETA_HEADER = "oauth-2025-04-20"

# Auth modes
AUTH_MODE_API_KEY = "api_key"
AUTH_MODE_OAUTH = "oauth"
DEFAULT_AUTH_MODE = AUTH_MODE_API_KEY
DEFAULT_MODEL = "claude-sonnet-4-5"
DEFAULT_BASE_URL = "https://api.anthropic.com/v1/"
MAX_TOKENS = 8192
PLUGIN_NAME = "Claudette"
SETTINGS_FILE = "Claudette.sublime-settings"
DEFAULT_VERIFY_SSL = True
SPINNER_CHARS = ['·', '✢', '✳', '✻', '✽'] # Probably also uses '∗', but makes the animation jumpy.
SPINNER_INTERVAL_MS = 250
