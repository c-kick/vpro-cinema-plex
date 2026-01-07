"""
Text utilities for normalization, validation, and sanitization.

Handles Unicode normalization, title matching, and input validation
to prevent edge cases and security issues.
"""

import re
import html
import hashlib
import unicodedata
from typing import Optional

from constants import MAX_TITLE_LENGTH


# =============================================================================
# Unicode Normalization
# =============================================================================

def normalize_unicode(text: str) -> str:
    """
    Normalize Unicode text for comparison.

    - NFKC normalization (compatibility decomposition + canonical composition)
    - Converts full-width to half-width characters
    - Normalizes different dash types to simple hyphen
    - Normalizes various quote styles

    Args:
        text: Input text to normalize

    Returns:
        Normalized text
    """
    if not text:
        return ""

    # NFKC handles full-width -> half-width, ligatures, etc.
    text = unicodedata.normalize('NFKC', text)

    # Normalize various dash types to simple hyphen
    dashes = '\u2010\u2011\u2012\u2013\u2014\u2015\u2212\uFE58\uFE63\uFF0D'
    for dash in dashes:
        text = text.replace(dash, '-')

    # Normalize quotes
    text = text.replace('"', '"').replace('"', '"')
    text = text.replace(''', "'").replace(''', "'")
    text = text.replace('«', '"').replace('»', '"')

    return text


def normalize_for_comparison(text: str) -> str:
    """
    Normalize text for fuzzy comparison.

    - Lowercase
    - Remove accents (café -> cafe)
    - Remove punctuation
    - Collapse whitespace

    Args:
        text: Input text to normalize

    Returns:
        Normalized text suitable for comparison
    """
    if not text:
        return ""

    text = normalize_unicode(text).lower()

    # Remove accents but keep base characters
    # NFD decomposes, then we strip combining marks
    text = unicodedata.normalize('NFD', text)
    text = ''.join(c for c in text if unicodedata.category(c) != 'Mn')

    # Remove punctuation except spaces
    text = re.sub(r'[^\w\s]', '', text)

    # Collapse whitespace
    text = ' '.join(text.split())

    return text


def normalize_for_cache_key(text: str, max_length: int = MAX_TITLE_LENGTH) -> str:
    """
    Normalize text for use in cache keys/filenames.

    - Lowercase
    - Only alphanumeric and hyphens
    - Truncate to max_length
    - Add hash suffix for collision resistance

    Args:
        text: Input text to normalize
        max_length: Maximum length for the result

    Returns:
        Safe string for use as cache key
    """
    if not text:
        return "unknown"

    normalized = normalize_for_comparison(text)

    # Convert spaces to hyphens, keep only safe chars
    safe = re.sub(r'[^a-z0-9\s-]', '', normalized)
    safe = re.sub(r'\s+', '-', safe)
    safe = re.sub(r'-+', '-', safe).strip('-')

    if not safe:
        safe = "unknown"

    # Truncate but add hash for uniqueness if needed
    if len(safe) > max_length - 9:  # Leave room for hash suffix
        text_hash = hashlib.sha256(text.encode()).hexdigest()[:8]
        safe = f"{safe[:max_length - 9]}-{text_hash}"

    return safe[:max_length]


# =============================================================================
# Title Matching
# =============================================================================

def titles_match(title1: str, title2: str) -> bool:
    """
    Check if two titles match after normalization.

    Args:
        title1: First title
        title2: Second title

    Returns:
        True if titles match after normalization
    """
    return normalize_for_comparison(title1) == normalize_for_comparison(title2)


def title_similarity(title1: str, title2: str) -> float:
    """
    Calculate Jaccard similarity between normalized titles.

    Args:
        title1: First title
        title2: Second title

    Returns:
        Similarity score between 0.0 and 1.0
    """
    words1 = set(normalize_for_comparison(title1).split())
    words2 = set(normalize_for_comparison(title2).split())

    if not words1 or not words2:
        return 0.0

    intersection = words1 & words2
    union = words1 | words2

    return len(intersection) / len(union)


# =============================================================================
# Sanitization
# =============================================================================

def sanitize_description(text: str) -> str:
    """
    Sanitize description text for safe display.

    - Strip HTML tags
    - Decode HTML entities
    - Normalize whitespace
    - Remove control characters

    Args:
        text: Raw description text

    Returns:
        Sanitized description
    """
    if not text:
        return ""

    # Decode HTML entities
    text = html.unescape(text)

    # Remove HTML tags
    text = re.sub(r'<[^>]+>', '', text)

    # Remove control characters (except newlines and tabs)
    text = ''.join(
        c for c in text
        if c in '\n\t' or unicodedata.category(c)[0] != 'C'
    )

    # Normalize whitespace (but preserve paragraph breaks)
    lines = text.split('\n')
    lines = [' '.join(line.split()) for line in lines]
    text = '\n'.join(line for line in lines if line)

    return text.strip()


# =============================================================================
# Validation
# =============================================================================

def is_valid_description(description: str, min_length: int = 50) -> bool:
    """
    Validate that a description contains actual content, not login/error pages.

    Detects common patterns that indicate scraped content is invalid:
    - Login page text
    - Error messages
    - Access denied messages
    - Very short content

    Args:
        description: Description text to validate
        min_length: Minimum acceptable length (default: 50 chars)

    Returns:
        True if description appears to be valid content
    """
    if not description or len(description.strip()) < min_length:
        return False

    desc_lower = description.lower()

    # Dutch login/error indicators
    invalid_patterns_nl = [
        'log in met',
        'inloggen',
        'gebruikersnaam en wachtwoord',
        'u moet ingelogd zijn',
        'toegang geweigerd',
        'geen toegang',
        'pagina niet gevonden',
        'sessie verlopen',
        'deze pagina is niet beschikbaar',
    ]

    # English login/error indicators
    invalid_patterns_en = [
        'please log in',
        'sign in to',
        'login required',
        'access denied',
        'page not found',
        'session expired',
        'unauthorized',
        '403 forbidden',
        '401 unauthorized',
        '404 not found',
    ]

    all_patterns = invalid_patterns_nl + invalid_patterns_en

    for pattern in all_patterns:
        if pattern in desc_lower:
            return False

    # If description is mostly a single short sentence, it's probably not content
    words = description.split()
    if len(words) < 10:
        return False

    return True


def validate_rating_key(key: str) -> bool:
    """
    Validate rating key format to prevent path traversal and injection.

    Args:
        key: Rating key to validate

    Returns:
        True if key is valid and safe
    """
    if not key:
        return False

    # Must start with vpro-
    if not key.startswith('vpro-'):
        return False

    # No path separators or traversal attempts
    dangerous_patterns = ['/', '\\', '..', '\x00', '\n', '\r']
    if any(p in key for p in dangerous_patterns):
        return False

    # Reasonable length
    if len(key) > 200:
        return False

    # Only safe characters (alphanumeric, hyphens)
    if not re.match(r'^vpro-[a-z0-9\-]+$', key):
        return False

    return True


def validate_imdb_id(imdb_id: str) -> bool:
    """
    Validate IMDB ID format.

    Args:
        imdb_id: IMDB ID to validate (e.g., "tt1234567")

    Returns:
        True if valid IMDB ID format
    """
    if not imdb_id:
        return False

    # IMDB IDs are tt followed by 7-8 digits
    return bool(re.match(r'^tt\d{7,8}$', imdb_id.lower()))


def extract_imdb_from_text(text: str) -> Optional[str]:
    """
    Extract IMDB ID from text (filename, guid, etc.).

    Args:
        text: Text that may contain an IMDB ID

    Returns:
        Extracted IMDB ID or None
    """
    if not text:
        return None

    patterns = [
        r'imdb-(tt\d{7,8})',
        r'\{imdb-(tt\d{7,8})\}',
        r'\[(tt\d{7,8})\]',
        r'(?<![a-z])(tt\d{7,8})(?![0-9])',
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).lower()

    return None


def extract_year_from_text(text: str) -> Optional[int]:
    """
    Extract release year from text (filename, etc.).

    Args:
        text: Text that may contain a year

    Returns:
        Extracted year or None
    """
    if not text:
        return None

    # Look for year in parentheses first (most common)
    match = re.search(r'\((\d{4})\)', text)
    if match:
        year = int(match.group(1))
        if 1888 <= year <= 2100:  # First film was 1888
            return year

    # Fallback: any 4-digit year
    match = re.search(r'\b(19\d{2}|20\d{2})\b', text)
    if match:
        return int(match.group(1))

    return None
