import sublime

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-4-5"
DEFAULT_BASE_URL = "https://api.anthropic.com/v1/"
MAX_TOKENS = 8192
PLUGIN_NAME = "Claudette"
SETTINGS_FILE = "Claudette.sublime-settings"
DEFAULT_VERIFY_SSL = True
SPINNER_CHARS = (
	["·", "✢", "✳", "✻", "✽"]
	if sublime.platform() == "osx"
	else ["·", "✢", "*", "✻", "✽"]
)
SPINNER_INTERVAL_MS = 250
TOOL_STATUS_MESSAGES = [
	'Accomplishing',
	'Actioning',
	'Actualizing',
	'Baking',
	'Brewing',
	'Calculating',
	'Cerebrating',
	'Churning',
	'Clauding',
	'Coalescing',
	'Cogitating',
	'Computing',
	'Conjuring',
	'Considering',
	'Cooking',
	'Crafting',
	'Creating',
	'Crunching',
	'Deliberating',
	'Determining',
	'Doing',
	'Effecting',
	'Finagling',
	'Forging',
	'Forming',
	'Generating',
	'Hatching',
	'Herding',
	'Honking',
	'Hustling',
	'Ideating',
	'Inferring',
	'Manifesting',
	'Marinating',
	'Moseying',
	'Mulling',
	'Mustering',
	'Musing',
	'Noodling',
	'Percolating',
	'Pondering',
	'Processing',
	'Puttering',
	'Reticulating',
	'Ruminating',
	'Schlepping',
	'Shucking',
	'Simmering',
	'Smooshing',
	'Spinning',
	'Stewing',
	'Synthesizing',
	'Thinking',
	'Transmuting',
	'Vibing',
	'Working',
]
