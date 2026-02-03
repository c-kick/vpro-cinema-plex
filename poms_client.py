"""
NPO POMS API and TMDB API Clients

API-based lookup clients for VPRO Cinema metadata:
- POMSAPIClient: NPO POMS REST API with HMAC-SHA256 authentication
- TMDBClient: TMDB API for alternate title lookup
"""

import base64
import hashlib
import hmac
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from urllib.parse import urlencode

from constants import (
    POMS_API_BASE,
    POMS_ORIGIN,
    POMS_PROFILE,
    TMDB_API_BASE,
    TITLE_SIMILARITY_THRESHOLD,
    YEAR_TOLERANCE,
)
from credentials import get_credential_manager, CredentialManager
from http_client import RateLimitedSession, create_session, SessionAwareComponent
from metrics import metrics
from models import VPROFilm
from text_utils import (
    sanitize_description,
    is_valid_description,
    titles_match,
    title_similarity,
    build_unique_list,
)

# Lazy imports to avoid circular dependency
# vpro_scraper imports from this module, so we import from it inside functions

logger = logging.getLogger(__name__)

# Environment variables
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")


# =============================================================================
# TMDB API Client
# =============================================================================

class TMDBClient(SessionAwareComponent):
    """
    Client for TMDB API to fetch alternate titles.

    Note: This client only supports movies. TV series support has been removed.
    """

    def __init__(self, api_key: str = None, session: RateLimitedSession = None):
        """
        Initialize TMDB client.

        Args:
            api_key: TMDB API key. Defaults to TMDB_API_KEY env var.
            session: Optional shared session for connection pooling.
        """
        self.api_key = api_key or TMDB_API_KEY
        self.init_session(session, timeout=10)

    # Preferred countries for alternate titles (relevant for VPRO/Dutch searches)
    PREFERRED_COUNTRIES = ["FR", "NL", "BE", "DE"]

    def _build_prioritized_titles(self, tmdb_id: int) -> List[str]:
        """
        Build prioritized title list from TMDB with deduplication.

        Priority order:
        1. Original title
        2. Titles from preferred countries (FR, NL, BE, DE)
        3. All other alternate titles

        Args:
            tmdb_id: TMDB ID of the movie

        Returns:
            List of unique titles in priority order
        """
        details = self._get(f"/movie/{tmdb_id}")
        alt_data = self._get(f"/movie/{tmdb_id}/alternative_titles")

        titles, add_title = build_unique_list(str.lower)

        # Priority 1: Original title
        if details:
            add_title(details.get("original_title"))

        # Priority 2: Preferred country titles
        alt_titles = alt_data.get("titles", []) if alt_data else []
        for country in self.PREFERRED_COUNTRIES:
            for t in alt_titles:
                if t.get("iso_3166_1") == country:
                    add_title(t.get("title"))

        # Priority 3: All remaining titles
        for t in alt_titles:
            add_title(t.get("title"))

        return titles

    def _get(self, endpoint: str, params: dict = None) -> Optional[dict]:
        """Make authenticated GET request to TMDB API."""
        if not self.api_key:
            return None

        params = params or {}
        params["api_key"] = self.api_key

        try:
            url = f"{TMDB_API_BASE}{endpoint}"
            response = self.session.get(url, params=params)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.warning(f"TMDB API error: {e}")
            return None

    def find_by_imdb(self, imdb_id: str, media_type: str = "film") -> tuple[Optional[int], str]:
        """
        Find TMDB ID from IMDB ID.

        Args:
            imdb_id: The IMDB ID to look up
            media_type: Ignored (kept for backward compatibility). Only movies are supported.

        Returns:
            Tuple of (tmdb_id, "film")
        """
        data = self._get(f"/find/{imdb_id}", {"external_source": "imdb_id"})
        if not data:
            return None, "film"

        # Only check movies (TV series support removed)
        if data.get("movie_results"):
            return data["movie_results"][0].get("id"), "film"

        return None, "film"

    def search_by_title(
        self,
        title: str,
        year: Optional[int] = None,
        media_type: str = "film"
    ) -> tuple[Optional[str], List[str]]:
        """
        Search TMDB by title and year to find IMDB ID and alternate titles.

        This enables reverse lookup: given an English title, find the original
        title and other alternates.

        Args:
            title: Title to search for
            year: Optional release year
            media_type: Ignored (kept for backward compatibility). Only movies are supported.

        Returns:
            Tuple of (imdb_id, list of alternate titles including original)
        """
        if not self.api_key:
            return None, []

        imdb_id = None
        tmdb_id = None
        titles = []

        # Search movies only (TV series support removed)
        params = {"query": title}
        if year:
            params["year"] = year
        data = self._get("/search/movie", params)
        if data and data.get("results"):
            # Find best match (prefer exact year match)
            for result in data["results"]:
                release_year = None
                if result.get("release_date"):
                    try:
                        release_year = int(result["release_date"][:4])
                    except (ValueError, IndexError):
                        pass
                if year and release_year == year:
                    tmdb_id = result.get("id")
                    break
            if not tmdb_id and data["results"]:
                tmdb_id = data["results"][0].get("id")

        if not tmdb_id:
            return None, []

        # Get external IDs (IMDB)
        ext_data = self._get(f"/movie/{tmdb_id}/external_ids")
        if ext_data:
            imdb_id = ext_data.get("imdb_id")

        # Get prioritized titles
        titles = self._build_prioritized_titles(tmdb_id)

        if titles:
            logger.info(f"TMDB search '{title}' ({year}): imdb={imdb_id}, titles={titles[:3]}...")

        return imdb_id, titles

    def get_alternate_titles(self, imdb_id: str, media_type: str = "film") -> List[str]:
        """
        Get alternate titles for a movie by IMDB ID.

        Prioritizes French, Dutch, Belgian, and German titles for VPRO searches.

        Args:
            imdb_id: The IMDB ID to look up
            media_type: Ignored (kept for backward compatibility). Only movies are supported.

        Returns:
            List of alternate titles, prioritized by relevance
        """
        if not self.api_key:
            logger.debug("TMDB API key not configured, skipping alternate titles")
            return []

        tmdb_id, _ = self.find_by_imdb(imdb_id)
        if not tmdb_id:
            logger.debug(f"Could not find TMDB ID for {imdb_id}")
            return []

        # Get prioritized titles for movie
        titles = self._build_prioritized_titles(tmdb_id)

        logger.info(
            f"TMDB alternate titles for {imdb_id}: "
            f"{titles[:5]}{'...' if len(titles) > 5 else ''}"
        )
        return titles


# =============================================================================
# NPO POMS API Client
# =============================================================================

class POMSAPIClient(SessionAwareComponent):
    """
    NPO POMS REST API client for VPRO Cinema.

    Uses HMAC-SHA256 authentication with auto-refreshing credentials.
    If authentication fails, automatically fetches fresh credentials
    from vprogids.nl and retries.
    """

    def __init__(
        self,
        session: RateLimitedSession = None,
        credential_manager: CredentialManager = None,
    ):
        """
        Initialize POMS API client.

        Args:
            session: Optional shared session for connection pooling.
            credential_manager: Optional credential manager instance.
        """
        self.creds = credential_manager or get_credential_manager()
        self.init_session(session, timeout=30)

    def _get_npo_date(self) -> str:
        """Get current timestamp in NPO API format."""
        return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")

    def _get_parameters_string(self, params: Dict[str, str]) -> str:
        """Build sorted parameter string for HMAC signature."""
        if not params:
            return ""
        sorted_keys = sorted(params.keys())
        parts = []
        for key in sorted_keys:
            if key != "iecomp":
                parts.append(f",{key}:{params[key]}")
        return "".join(parts)

    def _get_credentials(
        self,
        headers: Dict[str, str],
        path: str,
        params: Dict[str, str] = None
    ) -> str:
        """
        Generate HMAC-SHA256 signature for API authentication.

        Args:
            headers: Request headers (needs x-npo-date)
            path: API endpoint path
            params: Query parameters

        Returns:
            Base64-encoded HMAC signature
        """
        message_parts = [f"origin:{POMS_ORIGIN}"]

        if "x-npo-date" in headers:
            message_parts.append(f"x-npo-date:{headers['x-npo-date']}")

        clean_path = path.split("?")[0]
        uri_part = f"uri:/v1/api/{clean_path}"

        if params:
            uri_part += self._get_parameters_string(params)

        message_parts.append(uri_part)
        message = ",".join(message_parts)

        signature = hmac.new(
            self.creds.api_secret.encode('utf-8'),
            message.encode('utf-8'),
            hashlib.sha256
        )

        return base64.b64encode(signature.digest()).decode('utf-8')

    def _get_headers(self, path: str, params: Dict[str, str] = None) -> Dict[str, str]:
        """Build authenticated request headers."""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": POMS_ORIGIN,
            "x-npo-date": self._get_npo_date(),
        }

        credentials = self._get_credentials(headers, path, params)
        headers["Authorization"] = f"NPO {self.creds.api_key}:{credentials}"

        return headers

    def _do_search(
        self,
        query: str,
        max_results: int,
        media_type: str = "film"
    ) -> tuple:
        """
        Execute search request.

        Note: Only movies are supported. The media_type parameter is kept for
        backward compatibility but always filters for MOVIE type.

        Args:
            query: Search query string
            max_results: Maximum number of results
            media_type: Ignored (kept for backward compatibility)

        Returns:
            Tuple of (response, path, params) for potential retry
        """
        path = "pages/"
        params = {"profile": POMS_PROFILE, "max": str(max_results)}

        body = {
            "highlight": True,
            "searches": {"text": query},
            # Always filter for movies only (TV series support removed)
            "facets": {"types": {"include": "MOVIE"}},
        }

        headers = self._get_headers(path, params)
        url = f"{POMS_API_BASE}/{path}?{urlencode(params)}"

        response = self.session.post(url, headers=headers, json=body)
        return response, path, params

    def search(
        self,
        query: str,
        max_results: int = 10,
        media_type: str = "all"
    ) -> List[Dict[str, Any]]:
        """
        Search VPRO Cinema database.

        Automatically refreshes credentials on 401/403 and retries once.

        Args:
            query: Search query string
            max_results: Maximum number of results
            media_type: "film", "series", or "all"

        Returns:
            List of search result items
        """
        try:
            with metrics.timer("poms_search_duration_ms"):
                response, path, params = self._do_search(query, max_results, media_type)

            # Check for auth failure
            if response.status_code in (401, 403):
                logger.warning(
                    f"POMS API auth failed ({response.status_code}) - refreshing credentials..."
                )
                metrics.inc("poms_auth_failures")

                # Invalidate and fetch fresh credentials
                if self.creds.invalidate_and_refresh():
                    logger.info("Retrying with fresh credentials...")
                    response, _, _ = self._do_search(query, max_results, media_type)

                    if response.status_code in (401, 403):
                        logger.error("POMS API auth still failing after credential refresh")
                        return []
                else:
                    logger.error("Failed to refresh credentials")
                    return []

            if response.status_code != 200:
                logger.error(f"POMS API error {response.status_code}: {response.text[:200]}")
                return []

            data = response.json()
            items = data.get("items", [])
            logger.debug(f"POMS API returned {len(items)} results for '{query}'")
            metrics.inc("poms_searches", labels={"status": "success"})
            return items

        except Exception as e:
            logger.error(f"POMS API request failed: {e}")
            metrics.inc("poms_searches", labels={"status": "error"})
            return []

    def parse_item(self, item: Dict[str, Any]) -> Optional[VPROFilm]:
        """
        Parse API response item into VPROFilm object.

        Note: Only movies are supported. Series items are skipped.

        Args:
            item: Raw API response item

        Returns:
            VPROFilm if parseable movie, None otherwise
        """
        # Import here to avoid circular import at module level
        from vpro_scraper import VPROPageScraper

        result = item.get("result", {})

        item_type = result.get("type")
        if item_type != "MOVIE":
            # Skip series and other types (TV series support removed)
            return None

        year = None
        directors = []
        vpro_rating = None
        content_rating = None

        for rel in result.get("relations", []):
            rel_type = rel.get("type", "")
            value = rel.get("value", "")

            if rel_type == "CINEMA_YEAR" and value:
                try:
                    year = int(value)
                except ValueError:
                    pass
            elif rel_type == "CINEMA_DIRECTOR" and value:
                directors.append(value)
            elif rel_type == "CINEMA_APPRECIATION" and value:
                try:
                    vpro_rating = int(value)
                except ValueError:
                    pass
            elif rel_type == "CINEMA_AGERATING" and value and not content_rating:
                # Kijkwijzer age rating (e.g., "_16" -> "16", "AL")
                content_rating = str(value).lstrip('_')

        genres = [
            g.get("displayName", "")
            for g in result.get("genres", [])
            if g.get("displayName")
        ]

        # Extract images (posters, etc.)
        images = []
        for img in result.get("images", []):
            img_url = img.get("url")
            img_type = img.get("type", "PICTURE")
            if img_url:
                images.append({
                    "type": img_type,
                    "url": img_url,
                    "title": img.get("title", ""),
                })

        description = None
        url = result.get("url", "")

        # First try to get description from API paragraphs
        paragraphs = result.get("paragraphs", [])
        if paragraphs:
            raw_desc = paragraphs[0].get("body", "")
            sanitized = sanitize_description(raw_desc)
            # Validate description is actual content, not login/error page
            if is_valid_description(sanitized):
                description = sanitized
            else:
                logger.warning(f"POMS: Invalid API description for '{result.get('title', 'unknown')}' (len={len(raw_desc)})")

        # Scrape cinema.nl page for description and images
        # POMS API returns vprogids.nl image URLs which are now dead (410 Gone),
        # so we prefer images from cinema.nl scraping (images.vpro.nl URLs work)
        scrape_url = None
        if url and 'cinema.nl' in url:
            scrape_url = url
        elif url and 'vprogids.nl' in url:
            # Convert vprogids.nl URL to cinema.nl URL
            # vprogids.nl: https://www.vprogids.nl/cinema/films/film~16092390~the-penguin-lessons~.html
            # cinema.nl:   https://www.cinema.nl/db/16092390-the-penguin-lessons
            match = re.search(r'(?:film|serie)~(\d+)~([^~]+)~', url)
            if match:
                vpro_id_from_url = match.group(1)
                slug = match.group(2)
                scrape_url = f"https://www.cinema.nl/db/{vpro_id_from_url}-{slug}"
                logger.debug(f"POMS: Converted vprogids.nl URL to cinema.nl: {scrape_url}")

        if scrape_url:
            logger.info(f"POMS: Scraping cinema.nl for '{result.get('title', 'unknown')}' - {scrape_url}")
            try:
                scraper = VPROPageScraper(session=self.session)
                scraped = scraper.scrape(scrape_url)
                if scraped:
                    # Use scraped description if we don't have one from API
                    if not description and scraped.description:
                        description = scraped.description
                        logger.info(f"POMS: Got description from page scrape for '{result.get('title', 'unknown')}'")
                    # Always prefer scraped images (POMS API returns dead vprogids.nl URLs)
                    if scraped.images:
                        images = scraped.images
                        logger.info(f"POMS: Got {len(images)} images from page scrape for '{result.get('title', 'unknown')}'")
                else:
                    logger.debug(f"POMS: Page scrape returned no data for '{scrape_url}'")
            except Exception as e:
                logger.warning(f"POMS: Page scrape failed for '{scrape_url}': {e}")

        vpro_id = None

        if url:
            match = re.search(r'(?:film|serie)~(\d+)~', url)
            if match:
                vpro_id = match.group(1)

        return VPROFilm(
            title=result.get("title", ""),
            year=year,
            director=directors[0] if directors else None,
            description=description,
            url=url,
            imdb_id=None,
            vpro_id=vpro_id,
            genres=genres,
            vpro_rating=vpro_rating,
            content_rating=content_rating,
            images=images,
            media_type="film",
        )

    # Backward compatibility alias
    parse_film = parse_item


# =============================================================================
# POMS Search Function
# =============================================================================

def search_poms_api(
    title: str,
    year: Optional[int] = None,
    director: Optional[str] = None,
    media_type: str = "film",
    session: RateLimitedSession = None,
    imdb_id: Optional[str] = None,
) -> Optional[VPROFilm]:
    """
    Search VPRO using POMS API only.

    Note: Only movies are supported. The media_type parameter is kept for
    backward compatibility but is ignored (always searches for movies).

    Args:
        title: Title to search for
        year: Optional release year for validation
        director: Optional director for disambiguation
        media_type: Ignored (kept for backward compatibility)
        session: Optional shared session
        imdb_id: Optional IMDB ID - when provided, requires stricter matching
                 (disables fuzzy "top result" fallback)

    Returns:
        VPROFilm if found, None otherwise
    """
    poms = POMSAPIClient(session=session)

    try:
        # Always search for movies only
        items = poms.search(title, max_results=10, media_type="film")

        if not items:
            logger.debug(f"POMS: No results for '{title}'")
            return None

        logger.debug(f"POMS API returned {len(items)} results for '{title}'")

        films = [poms.parse_item(item) for item in items]
        # Filter to only films that exist AND have valid descriptions
        films = [f for f in films if f and f.description]

        if not films:
            logger.debug(f"POMS: All {len(items)} results lacked valid descriptions")
            return None

        # Exact title + year match
        if year:
            for film in films:
                if film.year == year and titles_match(film.title, title):
                    logger.info(f"POMS: Exact match - {film.title} ({film.year})")
                    metrics.inc("poms_matches", labels={"type": "exact"})
                    return film

        # Title match with year validation
        for film in films:
            if titles_match(film.title, title):
                if year and film.year and abs(film.year - year) > YEAR_TOLERANCE:
                    logger.debug(
                        f"POMS: Rejecting '{film.title}' ({film.year}) - "
                        f"year diff {abs(film.year - year)}"
                    )
                    continue
                logger.info(f"POMS: Title match - {film.title} ({film.year})")
                metrics.inc("poms_matches", labels={"type": "title"})
                return film

        # Validate top result by similarity
        # When IMDB ID is provided, we know exactly what we're looking for,
        # so skip the fuzzy "top result" fallback to avoid mismatches
        if imdb_id:
            logger.debug(
                f"POMS: Skipping fuzzy fallback - IMDB ID provided, "
                f"no exact match for '{title}'"
            )
        else:
            best = films[0]
            similarity = title_similarity(title, best.title)
            year_diff = abs(best.year - year) if (best.year and year) else 0

            if year and year_diff > YEAR_TOLERANCE:
                logger.debug(
                    f"POMS: Rejecting '{best.title}' ({best.year}) - year diff {year_diff}"
                )
            elif similarity < TITLE_SIMILARITY_THRESHOLD:
                logger.debug(
                    f"POMS: Rejecting '{best.title}' - low similarity {similarity:.0%}"
                )
            else:
                logger.info(f"POMS: Using top result - {best.title} ({best.year})")
                metrics.inc("poms_matches", labels={"type": "fuzzy"})
                return best

    except Exception as e:
        logger.error(f"POMS API error: {e}")

    return None


def search_poms_multiple(
    title: str,
    year: Optional[int] = None,
    media_type: str = "film",
    max_results: int = 10,
    session: RateLimitedSession = None,
) -> List[VPROFilm]:
    """
    Search VPRO and return MULTIPLE results for Fix Match.

    Unlike search_poms_api() which returns only the best match,
    this returns all valid matches with descriptions for user selection.

    Note: Only movies are supported. The media_type parameter is kept for
    backward compatibility but is ignored (always searches for movies).

    Search strategy:
    1. Search POMS API for matches
    2. Search cinema.nl for additional matches (fallback)
    3. Combine and deduplicate results

    Args:
        title: Title to search for
        year: Optional year (used for cinema.nl search ranking)
        media_type: Ignored (kept for backward compatibility)
        max_results: Maximum number of results to return (default 10)
        session: Optional shared session

    Returns:
        List of VPROFilm objects with valid descriptions
    """
    # Lazy import to avoid circular dependency
    from vpro_scraper import CinemaSearcher, VPROPageScraper

    poms = POMSAPIClient(session=session)
    films = []
    seen_vpro_ids = set()

    # Step 1: Search POMS API (movies only)
    try:
        items = poms.search(title, max_results=max_results, media_type="film")

        if items:
            logger.debug(f"POMS multiple: {len(items)} results for '{title}'")

            for item in items:
                film = poms.parse_item(item)
                if not film or not film.description:
                    continue

                # Deduplicate by VPRO ID
                if film.vpro_id:
                    if film.vpro_id in seen_vpro_ids:
                        continue
                    seen_vpro_ids.add(film.vpro_id)

                film.lookup_method = "poms"
                films.append(film)
        else:
            logger.debug(f"POMS multiple: No results for '{title}'")

    except Exception as e:
        logger.error(f"POMS multiple search error: {e}")

    # Step 2: Search cinema.nl for additional results
    # Always search cinema.nl to ensure we find items not in POMS
    try:
        logger.debug(f"Cinema.nl multiple: Searching for '{title}'")
        searcher = CinemaSearcher(session=session)
        scraper = VPROPageScraper(session=session)

        # Search cinema.nl (include year for better ranking)
        candidates = searcher.search(title, year)

        if candidates:
            logger.debug(f"Cinema.nl multiple: {len(candidates)} candidates for '{title}'")

            for candidate in candidates:
                # Skip if we already have this VPRO ID from POMS
                # Extract VPRO ID from URL pattern /db/{id}-{slug}
                vpro_id_match = re.search(r'/db/(\d+)-', candidate.url)
                if vpro_id_match:
                    vpro_id = vpro_id_match.group(1)
                    if vpro_id in seen_vpro_ids:
                        logger.debug(f"Cinema.nl: Skipping duplicate {vpro_id}")
                        continue

                # Scrape the detail page
                film = scraper.scrape(candidate.url)
                if not film or not film.description:
                    continue

                # Only include movies (skip series)
                if film.media_type != "film":
                    continue

                # Add to results
                if film.vpro_id:
                    seen_vpro_ids.add(film.vpro_id)

                film.lookup_method = "cinema_search"
                films.append(film)

                logger.debug(f"Cinema.nl: Added {film.title} ({film.year})")

    except Exception as e:
        logger.error(f"Cinema.nl multiple search error: {e}")

    logger.info(f"POMS multiple: Returning {len(films)} matches for '{title}'")
    return films[:max_results]


__all__ = [
    'TMDBClient',
    'POMSAPIClient',
    'search_poms_api',
    'search_poms_multiple',
    'TMDB_API_KEY',
]
