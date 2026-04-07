"""Stateful Markdown fenced code block detection (``` and ~~~).

Implements a line-oriented state machine aligned with CommonMark-style rules:
matching opener/closer fence character, closing fence length >= opening length.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

# Opening fence: optional indent, 3+ backticks or tildes, optional info string.
# Info cannot contain backticks or tildes (same as CommonMark).
_FENCE_OPEN_RE = re.compile(r"^(?:\s*)(`{3,}|~{3,})([^`~]*?)\s*$")


@dataclass
class ClaudetteCodeBlock:
    """Represents a code block found in the chat content."""

    content: str
    start_pos: int
    end_pos: int
    language: str


def _line_start_offsets(lines: List[str]) -> List[int]:
    """Start index of each line when lines == content.split('\\n')."""
    starts: List[int] = []
    offset = 0
    for i, line in enumerate(lines):
        starts.append(offset)
        if i < len(lines) - 1:
            offset += len(line) + 1
    return starts


def _closing_fence_match(
    line: str, fence_char: str, min_len: int
) -> bool:
    """True if line is a valid closing fence (same char, length >= min_len)."""
    stripped = line.lstrip(" \t")
    if not stripped:
        return False
    run = 0
    for ch in stripped:
        if ch == fence_char:
            run += 1
        else:
            break
    if run < min_len:
        return False
    remainder = stripped[run:]
    return remainder.strip() == ""


def _language_from_info(info: str) -> str:
    if not info or not info.strip():
        return ""
    return info.strip().split(None, 1)[0]


def find_fenced_code_blocks(content: str) -> List[ClaudetteCodeBlock]:
    """Find all closed fenced code blocks; positions match full fence span."""
    if not content:
        return []

    lines = content.split("\n")
    line_starts = _line_start_offsets(lines)
    blocks: List[ClaudetteCodeBlock] = []

    i = 0
    n = len(lines)
    while i < n:
        m = _FENCE_OPEN_RE.match(lines[i])
        if not m:
            i += 1
            continue

        marker, info_rest = m.group(1), m.group(2)
        fence_char = marker[0]
        fence_len = len(marker)
        language = _language_from_info(info_rest)

        open_line_idx = i
        start_pos = line_starts[open_line_idx]
        body_lines: List[str] = []
        i += 1
        closed = False

        while i < n:
            line = lines[i]
            if _closing_fence_match(line, fence_char, fence_len):
                body = "\n".join(body_lines).strip()
                close_start = line_starts[i]
                close_end = close_start + len(lines[i])
                end_pos = close_end
                blocks.append(
                    ClaudetteCodeBlock(
                        content=body,
                        start_pos=start_pos,
                        end_pos=end_pos,
                        language=language,
                    )
                )
                closed = True
                i += 1
                break
            body_lines.append(line)
            i += 1

        if not closed:
            break

    return blocks


def unclosed_fence_suffix_to_append(content: str) -> str:
    """Return text to append so all open fences are closed (e.g. after streaming).

    Uses the same open/close rules as find_fenced_code_blocks. While inside a
    fence, lines are treated as body (no nested fence opens), which matches
    typical chat output.

    Returns:
        Suffix such as "\\n```" or "\\n~~~~", or "" if nothing is unclosed.
    """
    if not content:
        return ""

    lines = content.split("\n")
    stack: List[tuple[str, int]] = []

    for line in lines:
        if stack:
            fence_char, fence_len = stack[-1]
            if _closing_fence_match(line, fence_char, fence_len):
                stack.pop()
            continue

        m = _FENCE_OPEN_RE.match(line)
        if m:
            marker = m.group(1)
            stack.append((marker[0], len(marker)))

    if not stack:
        return ""

    parts: List[str] = []
    for fence_char, fence_len in reversed(stack):
        parts.append("\n" + fence_char * fence_len)
    return "".join(parts)
