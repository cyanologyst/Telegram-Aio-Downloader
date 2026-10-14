"""Dean Edwards ``eval(function(p,a,c,k,e,d)...)`` unpacker used by video page resolvers."""

from __future__ import annotations

import ast
import re

PACKED_JS_RE = re.compile(
    r"eval\(function\(p,a,c,k,e,d\).*?\(\s*"
    r"'(?P<payload>(?:\\'|[^'])*)'\s*,\s*"
    r"(?P<radix>\d+)\s*,\s*"
    r"(?P<count>\d+)\s*,\s*"
    r"'(?P<symbols>(?:\\'|[^'])*)'\.split\('\|'\)",
    re.S,
)


def decode_packed_javascript(html: str) -> tuple[str, ...]:
    """Unpack every packed script on the page that mentions an ``.m3u8``/``.mp4`` URL."""

    decoded: list[str] = []
    for match in PACKED_JS_RE.finditer(html):
        try:
            payload = ast.literal_eval(f"'{match.group('payload')}'")
            symbols = tuple(ast.literal_eval(f"'{match.group('symbols')}'").split("|"))
        except (SyntaxError, ValueError):
            continue
        radix = int(match.group("radix"))

        unpacked = _unpack_packed_payload(payload, radix, symbols)
        if ".m3u8" in unpacked or ".mp4" in unpacked:
            decoded.append(unpacked)
    return tuple(decoded)


def _unpack_packed_payload(payload: str, radix: int, symbols: tuple[str, ...]) -> str:
    def replace_word(word_match: re.Match[str]) -> str:
        word = word_match.group(0)
        index = _base_to_int(word, radix)
        if index is None or index >= len(symbols) or not symbols[index]:
            return word
        return symbols[index]

    return re.sub(r"\b\w+\b", replace_word, payload)


def _base_to_int(value: str, radix: int) -> int | None:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    number = 0
    for char in value.lower():
        digit = alphabet.find(char)
        if digit < 0 or digit >= radix:
            return None
        number = number * radix + digit
    return number
