"""Track title and artist normalization for fuzzy duplicate detection."""

import re


# Patterns to strip from track titles (order matters — applied sequentially)
_TITLE_STRIP_PATTERNS = [
    # Remastered variants
    r"\s*[-–—]\s*(?:\d{4}\s+)?remaster(?:ed)?\s*(?:\d{4})?\s*$",
    r"\s*\(\s*(?:\d{4}\s+)?remaster(?:ed)?\s*(?:\d{4})?\s*\)",
    # Live variants
    r"\s*[-–—]\s*live\b.*$",
    r"\s*\(\s*live\b[^)]*\)",
    # Featuring variants
    r"\s*\(\s*(?:feat\.?|ft\.?|with)\s+[^)]+\)",
    r"\s*[-–—]\s*(?:feat\.?|ft\.?)\s+.*$",
    # Edition/version tags
    r"\s*\(\s*(?:deluxe|bonus\s+track|expanded|anniversary|special|radio)\s*(?:edition|version|mix)?\s*\)",
    r"\s*[-–—]\s*(?:deluxe|bonus\s+track|expanded|anniversary|special|radio)\s*(?:edition|version|mix)?\s*$",
    # Remix (but keep it — remix is a different track)
    # Acoustic/unplugged variants
    r"\s*\(\s*(?:acoustic|unplugged)\s*(?:version)?\s*\)",
    r"\s*[-–—]\s*(?:acoustic|unplugged)\s*(?:version)?\s*$",
    # Generic version/edit tags
    r"\s*\(\s*(?:original\s+)?(?:album\s+)?version\s*\)",
    r"\s*[-–—]\s*(?:original\s+)?(?:album\s+)?version\s*$",
    # Mono/stereo
    r"\s*\(\s*(?:mono|stereo)\s*(?:mix)?\s*\)",
    r"\s*[-–—]\s*(?:mono|stereo)\s*(?:mix)?\s*$",
    # Trailing whitespace/dashes
    r"\s*[-–—]\s*$",
]

_TITLE_COMPILED = [re.compile(p, re.IGNORECASE) for p in _TITLE_STRIP_PATTERNS]

# Version markers — modified versions of an original track (siblings, not duplicates)
# Used by base_title()/has_version_marker() for "alarm but not block" detection.
_VERSION_KEYWORDS = (
    r"remix|sped[\s\-]*up|spedup|slowed(?:[\s\-]*(?:and|&)[\s\-]*reverb)?|reverb"
    r"|nightcore|vip(?:[\s\-]*mix)?|edit|bootleg|bass[\s\-]*boost(?:ed)?|cover"
    r"|rework|flip|mash[\s\-]*up|mashup|chopped[\s\-]*and[\s\-]*screwed|tiktok"
    r"|extended[\s\-]*mix|club[\s\-]*mix|radio[\s\-]*mix|dub[\s\-]*mix|instrumental"
    r"|karaoke|piano[\s\-]*version|orchestral"
)

_VERSION_PATTERNS = [
    # Anything in parens that mentions a version keyword (incl. "X's Remix")
    rf"\s*\(\s*[^)]*\b(?:{_VERSION_KEYWORDS})\b[^)]*\)",
    # Dash-separated suffix containing a version keyword
    rf"\s*[-–—]\s*[^-–—]*\b(?:{_VERSION_KEYWORDS})\b[^-–—]*$",
]
_VERSION_COMPILED = [re.compile(p, re.IGNORECASE) for p in _VERSION_PATTERNS]
# Detection only counts when the keyword appears in a version-tag context
# (parens or dash suffix) — not as a bare word. Avoids false positives like
# "Cover Me", "The Edit", "Karaoke Night".
_VERSION_DETECT = re.compile(
    rf"(?:\([^)]*\b(?:{_VERSION_KEYWORDS})\b[^)]*\)|[-–—][^-–—]*\b(?:{_VERSION_KEYWORDS})\b[^-–—]*$)",
    re.IGNORECASE,
)

# Artist name normalization patterns
_ARTIST_STRIP_PATTERNS = [
    r"\s*&\s*",       # "A & B" → "a b"
    r"\s*,\s*",       # "A, B" → "a b"
    r"\s*;\s*",       # "A; B" → "a b"
    r"\s+feat\.?\s+", # "A feat. B" → "a b"
    r"\s+ft\.?\s+",   # "A ft B" → "a b"
    r"\s+and\s+",     # "A and B" → "a b"
    # Note: "x" as separator skipped — conflicts with "Lil Nas X"
]


def normalize_title(title: str) -> str:
    """Normalize track title for fuzzy comparison.

    Strips remastered tags, live markers, feat., edition info.
    Returns lowercase with collapsed whitespace.
    """
    result = title.strip()
    for pattern in _TITLE_COMPILED:
        result = pattern.sub("", result)
    # Collapse whitespace and lowercase
    result = re.sub(r"\s+", " ", result).strip().lower()
    return result


def normalize_artist(artist: str) -> str:
    """Normalize artist name for matching.

    Lowercases, replaces separators (& , ; feat ft and x) with space,
    collapses whitespace, sorts words for order-independent matching.
    """
    result = artist.strip().lower()
    for pattern in _ARTIST_STRIP_PATTERNS:
        result = re.sub(pattern, " ", result, flags=re.IGNORECASE)
    # Collapse whitespace and sort words for order-independent matching
    words = re.sub(r"\s+", " ", result).strip().split()
    words.sort()
    return " ".join(words)


def has_version_marker(title: str) -> bool:
    """True if title contains a version keyword (remix, sped up, slowed, etc).

    Used to flag "modified version" alerts separately from regular duplicates.
    """
    return bool(_VERSION_DETECT.search(title))


def base_title(title: str) -> str:
    """Strip version markers + regular tags + lowercase. Used for sibling matching.

    'Song (Sped Up)' and 'Song - VIP Mix' both → 'song'.
    Different from normalize_title which preserves the version distinction.
    """
    result = title.strip()
    for pattern in _VERSION_COMPILED:
        result = pattern.sub("", result)
    return normalize_title(result)


def title_words(normalized: str) -> set[str]:
    """Split normalized title into word set for containment checks."""
    return set(normalized.split())


def is_fuzzy_match(title_a: str, title_b: str, artist_a: str, artist_b: str) -> str | None:
    """Check if two tracks are fuzzy duplicates.

    Returns match type: 'fuzzy_exact', 'fuzzy_contains', 'fuzzy_levenshtein', or None.
    Assumes inputs are already normalized.
    """
    # Different artists → not a duplicate
    if artist_a != artist_b:
        return None

    # Identical normalized titles
    if title_a == title_b:
        return "fuzzy_exact"

    # Word containment: all words of shorter title appear in longer
    words_a = title_words(title_a)
    words_b = title_words(title_b)
    if words_a and words_b:
        shorter, longer = (words_a, words_b) if len(words_a) <= len(words_b) else (words_b, words_a)
        if shorter.issubset(longer) and len(shorter) >= 2 and len(shorter) / len(longer) >= 0.5:
            return "fuzzy_contains"

    # Levenshtein distance for short titles
    if len(title_a) <= 30 and len(title_b) <= 30:
        dist = _levenshtein(title_a, title_b)
        max_len = max(len(title_a), len(title_b))
        if max_len > 0 and dist <= min(3, max_len // 4):
            return "fuzzy_levenshtein"

    return None


def _levenshtein(s: str, t: str) -> int:
    """Compute Levenshtein distance between two strings."""
    if len(s) < len(t):
        return _levenshtein(t, s)
    if len(t) == 0:
        return len(s)

    prev = list(range(len(t) + 1))
    for i, c1 in enumerate(s):
        curr = [i + 1]
        for j, c2 in enumerate(t):
            cost = 0 if c1 == c2 else 1
            curr.append(min(curr[j] + 1, prev[j + 1] + 1, prev[j] + cost))
        prev = curr

    return prev[-1]
