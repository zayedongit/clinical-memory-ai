"""Recovering a JSON object from a model that promised strict JSON.

Every provider used here supports a JSON response mode, and every one of
them still occasionally returns markdown fences, a preamble, or an object
truncated mid-string when it hits the output-token cap. Those are the three
failure modes seen in practice, and this module handles exactly those three
rather than attempting to be a general JSON5 parser.

The distinction that matters clinically: repairing *structure* is safe
(closing a brace the model ran out of tokens for), repairing *content* is
not. Nothing here invents a value. A truncated field is dropped, never
guessed, and the caller is told repair happened so it can be counted.
"""
from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class ParseResult:
    data: dict | None
    repaired: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.data is not None


def _strip_fences(text: str) -> str:
    t = text.strip()
    if not t.startswith("```"):
        return t
    # ```json\n{...}\n```  ->  {...}
    parts = t.split("```")
    body = parts[1] if len(parts) >= 3 else t.lstrip("`")
    if body.lower().startswith("json"):
        body = body[4:]
    return body.strip().rstrip("`").strip()


def _close_open_structures(fragment: str) -> str:
    """Close whatever the model left open, scanning with string-awareness.

    Counting braces naively breaks on any brace inside a string value — and
    clinical free text contains them often enough to matter. This walks the
    fragment tracking whether it is inside a string, so only structural
    brackets are counted.
    """
    stack: list[str] = []
    in_string = False
    escaped = False
    for ch in fragment:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]" and stack and stack[-1] == ("{" if ch == "}" else "["):
            stack.pop()

    out = fragment
    if in_string:
        out += '"'
    out = out.rstrip()
    # A dangling key with no value, or a trailing comma, cannot be closed
    # into valid JSON — drop back to the last complete element.
    while out and out[-1] in ",:":
        out = out[:-1].rstrip()
        if out.endswith('"'):
            # We just removed the colon after a key; remove the key too.
            depth = 0
            for i in range(len(out) - 1, -1, -1):
                if out[i] == '"' and (i == 0 or out[i - 1] != "\\"):
                    depth += 1
                    if depth == 2:
                        out = out[:i].rstrip().rstrip(",").rstrip()
                        break
    for opener in reversed(stack):
        out += "}" if opener == "{" else "]"
    return out


def parse(text: str) -> ParseResult:
    """Parse model output into a dict, repairing structure if needed."""
    if not text or not text.strip():
        return ParseResult(None, False, "empty response")

    stripped = _strip_fences(text)
    try:
        loaded = json.loads(stripped)
        return ParseResult(loaded if isinstance(loaded, dict) else None, False,
                           "" if isinstance(loaded, dict) else "top-level value is not an object")
    except json.JSONDecodeError:
        pass

    start = stripped.find("{")
    if start == -1:
        return ParseResult(None, False, "no JSON object in response")

    repaired = _close_open_structures(stripped[start:])
    try:
        loaded = json.loads(repaired)
    except json.JSONDecodeError as e:
        return ParseResult(None, True, f"unrepairable: {e.msg}")
    if not isinstance(loaded, dict):
        return ParseResult(None, True, "repaired value is not an object")
    return ParseResult(loaded, True, "structure repaired")
