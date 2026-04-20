"""Shared exceptions for tool-confirmation deny paths.

Raised by tool handlers (currently ``run_bash_tool``) when the user rejects a
confirmation prompt. Caught by ``api.run_with_text_editor_loop`` so the denied
call is recorded as an ``is_error=True`` tool_result and every remaining
``tool_use`` block in the same assistant message is marked aborted, matching
Claude Code's ``abortController.abort()`` behavior at
``src/hooks/useCanUseTool.ts`` — all pending tool calls cancel, but the loop
continues so Claude can respond to the denial.
"""


class ToolUseDeniedError(Exception):
    """User denied a tool_use call; sibling tool calls must also be aborted.

    Args:
        message: User-facing reason returned as the denied block's tool_result
            content (e.g. ``"Command execution denied by user."``).
        tool_use_id: The Anthropic ``tool_use`` id of the denied block. Used so
            the caller pairs the denial with the correct block without needing
            to thread the id through by reference.
    """

    def __init__(self, message: str, tool_use_id: str = ""):
        super().__init__(message)
        self.message = message
        self.tool_use_id = tool_use_id
