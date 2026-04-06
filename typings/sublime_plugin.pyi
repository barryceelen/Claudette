"""Minimal stubs for Sublime Text's embedded sublime_plugin module."""

from typing import Any

class ApplicationCommand:
    """Base class for application commands."""

    def __init__(self) -> None: ...

class WindowCommand:
    """Base class for window commands."""

    window: Any

    def __init__(self, window: Any) -> None: ...

class TextCommand:
    """Base class for text commands."""

    view: Any

    def __init__(self, view: Any) -> None: ...

class EventListener:
    """Base class for event listeners."""

    def on_activated(self, view: Any) -> None: ...
    def on_load(self, view: Any) -> None: ...

class ViewEventListener:
    """Base class for view event listeners."""

    view: Any

    def __init__(self, view: Any) -> None: ...
    def on_close(self) -> None: ...
