"""
Lua comment cleaner.

The parser removes Lua line/block comments while preserving quoted strings and
long-bracket strings. It is intentionally conservative so a literal "--" inside
Lua code strings is not treated as a comment.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class LuaCleanResult:
    data: bytes
    changed: bool
    encoding: str


def _match_long_bracket_open(text: str, pos: int) -> tuple[str, int] | None:
    if pos >= len(text) or text[pos] != "[":
        return None

    idx = pos + 1
    while idx < len(text) and text[idx] == "=":
        idx += 1

    if idx < len(text) and text[idx] == "[":
        equals = text[pos + 1 : idx]
        return f"]{equals}]", idx + 1
    return None


def _copy_quoted_string(text: str, pos: int, out: list[str]) -> int:
    quote = text[pos]
    out.append(quote)
    pos += 1

    while pos < len(text):
        char = text[pos]
        out.append(char)
        pos += 1

        if char == "\\" and pos < len(text):
            out.append(text[pos])
            pos += 1
            continue

        if char == quote:
            break

    return pos


def _copy_long_bracket(text: str, pos: int, out: list[str]) -> int:
    opener = _match_long_bracket_open(text, pos)
    if not opener:
        out.append(text[pos])
        return pos + 1

    close_token, content_start = opener
    end = text.find(close_token, content_start)
    if end == -1:
        out.append(text[pos:])
        return len(text)

    end += len(close_token)
    out.append(text[pos:end])
    return end


def strip_lua_comments(source: str) -> str:
    out: list[str] = []
    pos = 0
    length = len(source)

    while pos < length:
        char = source[pos]

        if source.startswith("--", pos):
            block = _match_long_bracket_open(source, pos + 2)
            if block:
                close_token, content_start = block
                end = source.find(close_token, content_start)
                if end == -1:
                    removed = source[pos:]
                    pos = length
                else:
                    end += len(close_token)
                    removed = source[pos:end]
                    pos = end
                out.append(" ")
                out.append("\n" * removed.count("\n"))
                continue

            newline = source.find("\n", pos + 2)
            if newline == -1:
                pos = length
            else:
                pos = newline
            continue

        if char in {"'", '"'}:
            pos = _copy_quoted_string(source, pos, out)
            continue

        if char == "[" and _match_long_bracket_open(source, pos):
            pos = _copy_long_bracket(source, pos, out)
            continue

        out.append(char)
        pos += 1

    return "".join(out)


def clean_lua_bytes(data: bytes) -> LuaCleanResult:
    last_error: Exception | None = None
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            source = data.decode(encoding)
            cleaned = strip_lua_comments(source)
            if cleaned == source:
                return LuaCleanResult(data=data, changed=False, encoding=encoding)
            return LuaCleanResult(data=cleaned.encode(encoding), changed=True, encoding=encoding)
        except UnicodeError as exc:
            last_error = exc
            continue

    raise UnicodeError(f"Unable to decode Lua file: {last_error}")
