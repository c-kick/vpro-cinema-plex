"""
Shared constants, enums, and configuration for VPRO Cinema Provider.

This module centralizes all magic strings/numbers and provides type-safe
enums for media types and cache status.
"""

import os
from enum import Enum
from typing import Final


def _get_bool_env(key: str, default: bool = False) -> bool:
    """Get boolean from environment variable."""
    value = os.environ.get(key, "").lower()
    if value in ("true", "1", "yes", "on"):
        return True
    if value in ("false", "0", "no", "off"):
        return False
    return default

# =============================================================================
# Enums
# =============================================================================

class MediaType(str, Enum):
    """
    Media type enumeration.

    Inherits from str for JSON serialization compatibility.
    """
    FILM = "film"
    SERIES = "series"
    ALL = "all"

    @classmethod
    def from_plex_type(cls, plex_type: int) -> "MediaType":
        """Convert Plex type ID to MediaType."""
        mapping = {
            1: cls.FILM,    # Movie
            2: cls.SERIES,  # TV Show
            3: cls.SERIES,  # Season
            4: cls.SERIES,  # Episode
        }
        return mapping.get(plex_type, cls.FILM)

    def to_plex_type_str(self) -> str:
        """Convert to Plex type string."""
        return "show" if self == MediaType.SERIES else "movie"

    def to_type_char(self) -> str:
        """Convert to single char for rating key encoding."""
        return "s" if self == MediaType.SERIES else "m"

    @classmethod
    def from_type_char(cls, char: str) -> "MediaType":
        """Convert from rating key char to MediaType."""
        return cls.SERIES if char == "s" else cls.FILM


class CacheStatus(str, Enum):
    """Cache entry status."""
    FOUND = "found"
    NOT_FOUND = "not_found"
    EXPIRED = "expired"


# =============================================================================
# Provider Configuration
# =============================================================================

PROVIDER_IDENTIFIER: Final = "tv.plex.agents.custom.vpro.cinema"
PROVIDER_IDENTIFIER_TV: Final = "tv.plex.agents.custom.vpro.cinema.tv"
PROVIDER_TITLE: Final = "VPRO Cinema (Dutch Summaries) - Movies"
PROVIDER_TITLE_TV: Final = "VPRO Cinema (Dutch Summaries) - Series"
PROVIDER_VERSION: Final = "3.3.0"


# =============================================================================
# Cache Settings
# =============================================================================

DEFAULT_CACHE_TTL_FOUND: Final = 30 * 24 * 60 * 60  # 30 days for found entries
DEFAULT_CACHE_TTL_NOT_FOUND: Final = 7 * 24 * 60 * 60  # 7 days for not-found entries
MAX_CACHE_SIZE_MB: Final = 500  # Maximum cache size in MB
MAX_CACHE_ENTRIES: Final = 10000  # Maximum number of cached items


# =============================================================================
# Rate Limits (requests per second)
# =============================================================================

RATE_LIMIT_POMS: Final = 5.0
RATE_LIMIT_TMDB: Final = 4.0  # TMDB allows 40/10s
RATE_LIMIT_WEB_SEARCH: Final = 0.5  # Be nice to search engines
RATE_LIMIT_VPRO: Final = 2.0  # Be nice to vprogids.nl


# =============================================================================
# Retry Settings
# =============================================================================

MAX_RETRIES: Final = 3
RETRY_BACKOFF_BASE: Final = 2.0  # Exponential backoff base (seconds)
CREDENTIAL_REFRESH_COOLDOWN: Final = 60.0  # Minimum seconds between refresh attempts


# =============================================================================
# Title Matching
# =============================================================================

TITLE_SIMILARITY_THRESHOLD: Final = 0.3
YEAR_TOLERANCE: Final = 2
MAX_TITLE_LENGTH: Final = 50


# =============================================================================
# External URLs
# =============================================================================

VPRO_CREDENTIAL_URL: Final = "https://www.vprogids.nl/cinema/zoek.html"
POMS_API_BASE: Final = "https://rs.poms.omroep.nl/v1/api"
POMS_ORIGIN: Final = "https://www.vprogids.nl"
POMS_PROFILE: Final = "vprocinema"
TMDB_API_BASE: Final = "https://api.themoviedb.org/3"


# =============================================================================
# Default Credentials (fallback only)
# =============================================================================

DEFAULT_POMS_API_KEY: Final = "ione7ahfij"
DEFAULT_POMS_API_SECRET: Final = "aag9veesei"


# =============================================================================
# Feature Flags (configurable via environment)
# =============================================================================
# These control which metadata fields are returned to Plex.
# By default, summary and contentRating are returned, allowing
# secondary agents (Plex Movie/Series) to provide artwork and ratings.

# Return VPRO Dutch summary/description to Plex (default: true)
# This is the primary feature of this provider
VPRO_RETURN_SUMMARY: bool = _get_bool_env("VPRO_RETURN_SUMMARY", True)

# Return Kijkwijzer content rating (AL, 6, 9, 12, 14, 16, 18) to Plex (default: true)
# Dutch age classification system similar to MPAA ratings
VPRO_RETURN_CONTENT_RATING: bool = _get_bool_env("VPRO_RETURN_CONTENT_RATING", True)

# Return VPRO images (posters) to Plex (default: false)
# WARNING: May override images from secondary agents like Plex Movie
VPRO_RETURN_IMAGES: bool = _get_bool_env("VPRO_RETURN_IMAGES", False)

# Return VPRO appreciation rating (1-10) as audienceRating field (default: false)
# NOTE: Plex may store this value but displays icons based on library "Ratings Source"
# setting, not the provider. Custom ratingImage schemes are not supported.
# See: https://forums.plex.tv/c/dev-api-corner/ for updates on this limitation.
VPRO_RETURN_RATING: bool = _get_bool_env("VPRO_RETURN_RATING", False)
