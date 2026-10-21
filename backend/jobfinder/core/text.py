"""Small text helpers shared by matching and ingestion.

`tokens` splits text into lowercase word tokens that keep tech names whole ("c++", "c#", "node.js").
`refuses_sponsorship` spots a posting that rules out visa sponsorship (matching filters on it; the
USAJOBS source adds its citizenship note unless the text already says so).
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


_NO_SPONSOR = re.compile(
    r"(?:unable|not able|cannot|can't|can not|do not|don't|does not|will not|won't|not)\s+(?:to\s+|be able to\s+)?"
    r"(?:provide|offer|sponsor|support)[^.\n]{0,50}(?:visa|sponsorship|work authori[sz]ation)"
    r"|(?:unable|not able|cannot|can't|can not|do not|don't|does not|will not|won't)\s+(?:to\s+)?sponsor\b(?!\s+(?:events?|conferences?|relocation|meetups?))"
    r"|sponsorship\s+(?:(?:is|are|will)\s+not\s+(?:be\s+|currently\s+)?|(?:isn't|aren't)\s+(?:currently\s+)?|(?:is\s+)?un)(?:available|provided|offered|supported)"
    r"|no\s+(?:(?:work\s+)?visa\s+|h-?1-?b\s+|immigration\s+)?sponsorship"
    r"|without\s+(?:requiring\s+|the need for\s+)?(?:visa\s+)?sponsorship"
    r"|(?:u\.?s\.?|united states)\s+citizen(?:ship)?\s+(?:is\s+)?required"
    r"|must\s+be\s+(?:a\s+)?(?:u\.?s\.?|united states)\s+citizen"
    r"|security clearance|ts/sci",
    re.IGNORECASE,
)


def refuses_sponsorship(description: str) -> bool:
    return bool(_NO_SPONSOR.search(description or ""))
