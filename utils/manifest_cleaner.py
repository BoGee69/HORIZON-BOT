"""
Conservative text-manifest comment cleaner.

Used for text manifest formats such as .acf/.vdf/.manifest. Binary manifests
are left untouched.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ManifestCleanResult:
    data: bytes
    changed: bool
    encoding: str = ""
    skipped_reason: str = ""


def _looks_like_text(data: bytes) -> bool:
    if not data:
        return True
    if b"\x00" in data[:8192]:
        return False

    sample = data[:8192]
    allowed = {9, 10, 13}
    bad = sum(1 for byte in sample if byte < 32 and byte not in allowed)
    return (bad / max(1, len(sample))) < 0.01


def _decode_text(data: bytes) -> tuple[str, str] | None:
    if not _looks_like_text(data):
        return None

    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return data.decode(encoding), encoding
        except UnicodeError:
            continue
    return None


def strip_manifest_comments(source: str) -> str:
    out: list[str] = []
    pos = 0
    length = len(source)
    in_quote = False
    line_has_code = False

    while pos < length:
        char = source[pos]
        nxt = source[pos + 1] if pos + 1 < length else ""

        if in_quote:
            out.append(char)
            pos += 1
            if char == "\\" and pos < length:
                out.append(source[pos])
                pos += 1
                continue
            if char == '"':
                in_quote = False
            if char == "\n":
                line_has_code = False
            continue

        if char == '"':
            in_quote = True
            line_has_code = True
            out.append(char)
            pos += 1
            continue

        if char == "\n":
            line_has_code = False
            out.append(char)
            pos += 1
            continue

        if char in " \t\r":
            out.append(char)
            pos += 1
            continue

        if char == "/" and nxt == "*":
            end = source.find("*/", pos + 2)
            removed = source[pos:] if end == -1 else source[pos : end + 2]
            out.append("\n" * removed.count("\n"))
            pos = length if end == -1 else end + 2
            line_has_code = False if removed.endswith("\n") else line_has_code
            continue

        starts_line_comment = (
            (char == "/" and nxt == "/" and not line_has_code)
            or (char in {"#", ";"} and not line_has_code)
            or (
                char == "/"
                and nxt == "/"
                and pos > 0
                and source[pos - 1].isspace()
            )
        )
        if starts_line_comment:
            newline = source.find("\n", pos)
            if newline == -1:
                pos = length
            else:
                pos = newline
            continue

        line_has_code = True
        out.append(char)
        pos += 1

    return "".join(out)


def clean_manifest_bytes(data: bytes) -> ManifestCleanResult:
    decoded = _decode_text(data)
    if not decoded:
        return ManifestCleanResult(data=data, changed=False, skipped_reason="not text")

    source, encoding = decoded
    cleaned = strip_manifest_comments(source)
    if cleaned == source:
        return ManifestCleanResult(data=data, changed=False, encoding=encoding)
    return ManifestCleanResult(data=cleaned.encode(encoding), changed=True, encoding=encoding)
