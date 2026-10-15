"""Small text helpers shared by matching and ingestion.

`tokens` splits text into lowercase word tokens that keep tech names whole ("c++", "c#", "node.js").
"""
import re

_TOKEN = re.compile(r"[a-z0-9+#.]+")
_SPACES = re.compile(r"\s+")


def tokens(text: str) -> set[str]:
    """{'senior', 'c++', 'node.js'} for 'Senior C++ / Node.js.': lowercased, outer dots stripped."""
    return {t.strip(".") for t in _TOKEN.findall(text.lower()) if t.strip(".")}


def squash_spaces(text: str) -> str:
    """Runs of whitespace become one space, and the ends are trimmed."""
    return _SPACES.sub(" ", text).strip()
