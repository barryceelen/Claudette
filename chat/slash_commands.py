import sublime
from typing import Dict, Callable, Optional, Any, List, Tuple

class SlashCommandHandler:
    """Handles slash commands in the chat prompt."""
    
    def __init__(self):
        self.commands: Dict[str, Callable] = {}
        
    def register(self, command: str, handler: Callable):
        """Register a new slash command with its handler function."""
        self.commands[command] = handler
        
    def handle(self, command: str, chat_view, args: List[str] = None) -> Tuple[bool, Optional[str]]:
        """
        Process a slash command.
        
        Args:
            command: The command name without the slash
            chat_view: The chat view instance
            args: Optional list of command arguments
            
        Returns:
            Tuple of (was_handled, response_text)
        """
        if not args:
            args = []
            
        if command in self.commands:
            try:
                result = self.commands[command](chat_view, args)
                return True, result
            except Exception as e:
                return True, f"Error executing command /{command}: {str(e)}"
        
        return False, None
        
    def get_commands(self) -> List[str]:
        """Return a list of all registered commands."""
        return list(self.commands.keys())

# Create a singleton instance
slash_commands = SlashCommandHandler()

def register_command(command: str):
    """Decorator to register a function as a slash command handler."""
    def decorator(func):
        slash_commands.register(command, func)
        return func
    return decorator

# Register built-in commands
@register_command("cost")
def cmd_cost(chat_view, args):
    """Display the current session cost information."""
    if not chat_view or not chat_view.view:
        return "No active chat session."
    
    settings = chat_view.view.settings()
    session_stats = settings.get('claudette_session_stats', {
        'input_tokens': 0,
        'output_tokens': 0,
        'cost': 0.0
    })
    
    input_tokens = session_stats.get('input_tokens', 0)
    output_tokens = session_stats.get('output_tokens', 0)
    cost = session_stats.get('cost', 0.0)
    
    return (
        f"### Session Statistics\n\n"
        f"- Input tokens: {input_tokens:,}\n"
        f"- Output tokens: {output_tokens:,}\n"
        f"- Total cost: ${cost:.4f}\n"
    )

@register_command("help")
def cmd_help(chat_view, args):
    """Display available slash commands."""
    commands = slash_commands.get_commands()
    commands.sort()
    
    result = "### Available Commands\n\n"
    for cmd in commands:
        handler = slash_commands.commands.get(cmd)
        doc = handler.__doc__ or "No description available"
        first_line = doc.strip().split('\n')[0]
        result += f"- /{cmd}: {first_line}\n"
    
    return result
