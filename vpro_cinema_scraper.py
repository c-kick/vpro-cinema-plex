#!/usr/bin/env python3
"""
VPRO Cinema Scraper v3.1.0
==========================

Searches VPRO Cinema database for Dutch film and TV series descriptions.

Search Strategy:
    1. PRIMARY: NPO POMS API (direct database query via authenticated REST API)
    2. FALLBACK: Web search (DuckDuckGo -> Startpage) + page scraping
    3. ALTERNATE TITLES: If no match and IMDB ID available, fetch alternate
       titles from TMDB and retry search

Supported Media Types:
    - Films (movies): /cinema/films/
    - TV Series: /cinema/series/

Credential Management:
    - Credentials are auto-extracted from vprogids.nl if API returns 401/403
    - Cached to credentials.json for persistence
    - Falls back to hardcoded defaults if extraction fails

Environment Variables:
    TMDB_API_KEY: API key for TMDB alternate titles lookup (optional but recommended)
    POMS_CACHE_FILE: Path to POMS credentials cache (default: ./cache/credentials.json)

Usage:
    from vpro_cinema_scraper import get_vpro_description

    # Search for a film
    film = get_vpro_description("The Matrix", year=1999)
    if film:
        print(film.description)

    # Search specifically for a series
    series = get_vpro_description("Adolescence", year=2025, media_type="series")
    if series:
        print(series.description)
"""

import hmac
import hashlib
import base64
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from urllib.parse import urlencode, quote_plus, parse_qs, urlparse, unquote

from bs4 import BeautifulSoup

from constants import (
    MediaType,
    POMS_API_BASE,
    POMS_ORIGIN,
    POMS_PROFILE,
    TMDB_API_BASE,
    TITLE_SIMILARITY_THRESHOLD,
    YEAR_TOLERANCE,
)
from credentials import get_credential_manager, CredentialManager
from http_client import RateLimitedSession, create_session
from text_utils import (
    normalize_for_comparison,
    titles_match,
    title_similarity,
    sanitize_description,
    is_valid_description,
)
from metrics import metrics

# Configure logging
logger = logging.getLogger(__name__)

# Environment variables
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class VPROFilm:
    """Represents a film or series with VPRO Cinema metadata."""
    title: str
    year: Optional[int] = None
    director: Optional[str] = None
    description: Optional[str] = None
    url: Optional[str] = None
    imdb_id: Optional[str] = None
    vpro_id: Optional[str] = None
    genres: List[str] = field(default_factory=list)
    vpro_rating: Optional[int] = None
    media_type: str = "film"  # "film" or "series"
    # Lookup diagnostics
    lookup_method: Optional[str] = None  # "poms", "tmdb_alt", "web"
    discovered_imdb: Optional[str] = None  # IMDB found via TMDB lookup

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            'title': self.title,
            'year': self.year,
            'director': self.director,
            'description': self.description,
            'url': self.url,
            'imdb_id': self.imdb_id,
            'vpro_id': self.vpro_id,
            'genres': self.genres,
            'vpro_rating': self.vpro_rating,
            'media_type': self.media_type,
            'lookup_method': self.lookup_method,
            'discovered_imdb': self.discovered_imdb,
        }


# =============================================================================
# TMDB API Client
# =============================================================================

class TMDBClient:
    """
    Client for TMDB API to fetch alternate titles.

    Supports both movies and TV series lookup.
    """

    def __init__(self, api_key: str = None, session: RateLimitedSession = None):
        """
        Initialize TMDB client.

        Args:
            api_key: TMDB API key. Defaults to TMDB_API_KEY env var.
            session: Optional shared session for connection pooling.
        """
        self.api_key = api_key or TMDB_API_KEY
        self.session = session or create_session(timeout=10)
        self._owns_session = session is None

    def close(self) -> None:
        """Close session if we own it."""
        if self._owns_session:
            self.session.close()

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

    def find_by_imdb(self, imdb_id: str, media_type: str = "all") -> tuple[Optional[int], str]:
        """
        Find TMDB ID from IMDB ID.

        Args:
            imdb_id: The IMDB ID to look up
            media_type: "film", "series", or "all" (checks both)

        Returns:
            Tuple of (tmdb_id, detected_media_type)
        """
        data = self._get(f"/find/{imdb_id}", {"external_source": "imdb_id"})
        if not data:
            return None, "film"

        # Check movies first (unless specifically looking for series)
        if media_type in ("film", "all") and data.get("movie_results"):
            return data["movie_results"][0].get("id"), "film"

        # Check TV series
        if media_type in ("series", "all") and data.get("tv_results"):
            return data["tv_results"][0].get("id"), "series"

        return None, "film"

    def search_by_title(
        self,
        title: str,
        year: Optional[int] = None,
        media_type: str = "all"
    ) -> tuple[Optional[str], List[str]]:
        """
        Search TMDB by title and year to find IMDB ID and alternate titles.

        This enables reverse lookup: given an English title, find the original
        title and other alternates.

        Args:
            title: Title to search for
            year: Optional release year
            media_type: "film", "series", or "all"

        Returns:
            Tuple of (imdb_id, list of alternate titles including original)
        """
        if not self.api_key:
            return None, []

        imdb_id = None
        tmdb_id = None
        detected_type = "film"
        titles = []

        # Search movies
        if media_type in ("film", "all"):
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
                        detected_type = "film"
                        break
                if not tmdb_id and data["results"]:
                    tmdb_id = data["results"][0].get("id")
                    detected_type = "film"

        # Search TV if no movie found or specifically looking for series
        if not tmdb_id and media_type in ("series", "all"):
            params = {"query": title}
            if year:
                params["first_air_date_year"] = year
            data = self._get("/search/tv", params)
            if data and data.get("results"):
                for result in data["results"]:
                    air_year = None
                    if result.get("first_air_date"):
                        try:
                            air_year = int(result["first_air_date"][:4])
                        except (ValueError, IndexError):
                            pass
                    if year and air_year == year:
                        tmdb_id = result.get("id")
                        detected_type = "series"
                        break
                if not tmdb_id and data["results"]:
                    tmdb_id = data["results"][0].get("id")
                    detected_type = "series"

        if not tmdb_id:
            return None, []

        # Get external IDs (IMDB)
        if detected_type == "series":
            ext_data = self._get(f"/tv/{tmdb_id}/external_ids")
        else:
            ext_data = self._get(f"/movie/{tmdb_id}/external_ids")

        if ext_data:
            imdb_id = ext_data.get("imdb_id")

        # Get details for original title
        if detected_type == "series":
            details = self._get(f"/tv/{tmdb_id}")
            alt_data = self._get(f"/tv/{tmdb_id}/alternative_titles")
            original_title_key = "original_name"
            alt_results_key = "results"
        else:
            details = self._get(f"/movie/{tmdb_id}")
            alt_data = self._get(f"/movie/{tmdb_id}/alternative_titles")
            original_title_key = "original_title"
            alt_results_key = "titles"

        seen = set()

        def add_title(t: str):
            if t and t.lower() not in seen:
                seen.add(t.lower())
                titles.append(t)

        # Add original title first (most important for VPRO)
        if details:
            add_title(details.get(original_title_key))

        # Add alternate titles, prioritizing FR/NL/BE/DE
        alt_titles = alt_data.get(alt_results_key, []) if alt_data else []
        preferred_countries = ["FR", "NL", "BE", "DE"]

        for country in preferred_countries:
            for t in alt_titles:
                if t.get("iso_3166_1") == country:
                    add_title(t.get("title"))

        for t in alt_titles:
            add_title(t.get("title"))

        if titles:
            logger.info(f"TMDB search '{title}' ({year}): imdb={imdb_id}, titles={titles[:3]}...")

        return imdb_id, titles

    def get_alternate_titles(self, imdb_id: str, media_type: str = "all") -> List[str]:
        """
        Get alternate titles for a movie or TV series by IMDB ID.

        Prioritizes French, Dutch, Belgian, and German titles for VPRO searches.

        Args:
            imdb_id: The IMDB ID to look up
            media_type: "film", "series", or "all" (auto-detect)

        Returns:
            List of alternate titles, prioritized by relevance
        """
        if not self.api_key:
            logger.debug("TMDB API key not configured, skipping alternate titles")
            return []

        tmdb_id, detected_type = self.find_by_imdb(imdb_id, media_type)
        if not tmdb_id:
            logger.debug(f"Could not find TMDB ID for {imdb_id}")
            return []

        # Use correct endpoint based on detected type
        if detected_type == "series":
            details = self._get(f"/tv/{tmdb_id}")
            alt_data = self._get(f"/tv/{tmdb_id}/alternative_titles")
            original_title_key = "original_name"
            alt_results_key = "results"
        else:
            details = self._get(f"/movie/{tmdb_id}")
            alt_data = self._get(f"/movie/{tmdb_id}/alternative_titles")
            original_title_key = "original_title"
            alt_results_key = "titles"

        titles = []
        seen = set()

        def add_title(t: str):
            if t and t.lower() not in seen:
                seen.add(t.lower())
                titles.append(t)

        # Priority 1: Original title
        if details:
            add_title(details.get(original_title_key))

        # Priority 2: Preferred language titles
        preferred_countries = ["FR", "NL", "BE", "DE"]
        alt_titles = alt_data.get(alt_results_key, []) if alt_data else []

        if alt_titles:
            # First add preferred countries
            for country in preferred_countries:
                for t in alt_titles:
                    if t.get("iso_3166_1") == country:
                        add_title(t.get("title"))

            # Then add rest
            for t in alt_titles:
                add_title(t.get("title"))

        logger.info(
            f"TMDB alternate titles for {imdb_id} [{detected_type}]: "
            f"{titles[:5]}{'...' if len(titles) > 5 else ''}"
        )
        return titles


# =============================================================================
# NPO POMS API Client
# =============================================================================

class POMSAPIClient:
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
        self.session = session or create_session(timeout=30)
        self._owns_session = session is None

    def close(self) -> None:
        """Close session if we own it."""
        if self._owns_session:
            self.session.close()

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
        media_type: str = "all"
    ) -> tuple:
        """
        Execute search request.

        Args:
            query: Search query string
            max_results: Maximum number of results
            media_type: "film", "series", or "all"

        Returns:
            Tuple of (response, path, params) for potential retry
        """
        path = "pages/"
        params = {"profile": POMS_PROFILE, "max": str(max_results)}

        body = {
            "highlight": True,
            "searches": {"text": query},
        }

        # Add facet filter only when searching for a specific type
        if media_type == "film":
            body["facets"] = {"types": {"include": "MOVIE"}}
        elif media_type == "series":
            body["facets"] = {"types": {"include": "SERIES"}}

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

        Args:
            item: Raw API response item

        Returns:
            VPROFilm if parseable, None otherwise
        """
        result = item.get("result", {})

        item_type = result.get("type")
        if item_type == "MOVIE":
            media_type = "film"
        elif item_type == "SERIES":
            media_type = "series"
        else:
            return None

        year = None
        directors = []
        vpro_rating = None

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

        genres = [
            g.get("displayName", "")
            for g in result.get("genres", [])
            if g.get("displayName")
        ]

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

        # If no valid description from API but we have a URL, try scraping the page directly
        if not description and url:
            logger.info(f"POMS: Scraping page for '{result.get('title', 'unknown')}' - {url}")
            try:
                scraper = VPROPageScraper(session=self.session)
                scraped = scraper.scrape(url)
                if scraped and scraped.description:
                    description = scraped.description
                    logger.info(f"POMS: Page scrape successful for '{result.get('title', 'unknown')}'")
                else:
                    logger.debug(f"POMS: Page scrape returned no description for '{url}'")
            except Exception as e:
                logger.warning(f"POMS: Page scrape failed for '{url}': {e}")

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
            media_type=media_type,
        )

    # Backward compatibility alias
    parse_film = parse_item


# =============================================================================
# Web Search Fallback
# =============================================================================

class WebSearcher:
    """
    Search VPRO Cinema via web search engines.

    Supports both films and series with fallback between
    DuckDuckGo and Startpage.
    """

    # URL patterns for matching VPRO Cinema URLs
    FILM_URL_PATTERN = r'vprogids\.nl/cinema/films/film~'
    SERIES_URL_PATTERN = r'vprogids\.nl/cinema/series/serie~'

    # CAPTCHA/bot detection indicators (specific elements, not just words)
    CAPTCHA_INDICATORS = [
        'id="captcha"',
        'class="captcha"',
        'name="captcha"',
        'g-recaptcha',
        'h-captcha',
        'cf-turnstile',
        'please verify you are human',
        'confirm you are not a robot',
        'complete the security check',
        'unusual traffic from your computer',
        '/captcha/',
        'data-sitekey=',
    ]

    def __init__(self, session: RateLimitedSession = None):
        """
        Initialize web searcher.

        Args:
            session: Optional shared session for connection pooling.
        """
        self.session = session or create_session(timeout=15)
        self._owns_session = session is None

    def close(self) -> None:
        """Close session if we own it."""
        if self._owns_session:
            self.session.close()

    def _is_captcha_page(self, html: str) -> bool:
        """
        Detect CAPTCHA/bot protection pages.

        Uses specific indicators to avoid false positives
        (e.g., movies about robots).

        Args:
            html: Page HTML content

        Returns:
            True if page appears to be a CAPTCHA challenge
        """
        html_lower = html.lower()

        # Check for specific CAPTCHA indicators
        for indicator in self.CAPTCHA_INDICATORS:
            if indicator in html_lower:
                return True

        # Additional check: very short response with block phrases
        # (real content pages are longer)
        if len(html) < 5000:
            block_phrases = [
                'are you a robot',
                'automated access',
                'access denied',
                'blocked',
            ]
            if any(phrase in html_lower for phrase in block_phrases):
                return True

        return False

    def _get_search_paths(self, media_type: str) -> List[str]:
        """Get VPRO Cinema paths based on media type."""
        if media_type == "film":
            return ["cinema/films"]
        elif media_type == "series":
            return ["cinema/series"]
        else:
            return ["cinema/films", "cinema/series"]

    def _matches_vpro_url(self, url: str, media_type: str) -> bool:
        """Check if URL matches VPRO Cinema pattern."""
        if media_type == "film":
            return bool(re.search(self.FILM_URL_PATTERN, url))
        elif media_type == "series":
            return bool(re.search(self.SERIES_URL_PATTERN, url))
        else:
            return bool(
                re.search(self.FILM_URL_PATTERN, url) or
                re.search(self.SERIES_URL_PATTERN, url)
            )

    def search_duckduckgo(
        self,
        title: str,
        year: Optional[int] = None,
        media_type: str = "all"
    ) -> List[str]:
        """
        Search using DuckDuckGo HTML.

        Args:
            title: Title to search for
            year: Optional release year
            media_type: "film", "series", or "all"

        Returns:
            List of matching VPRO URLs
        """
        all_urls = []

        for path in self._get_search_paths(media_type):
            query = f'site:vprogids.nl/{path} "{title}"'
            if year:
                query += f' {year}'

            search_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
            logger.debug(f"DuckDuckGo query: {query}")

            try:
                response = self.session.get(search_url)
                response.raise_for_status()

                if self._is_captcha_page(response.text):
                    logger.warning("DuckDuckGo showing CAPTCHA - skipping")
                    continue

                soup = BeautifulSoup(response.text, 'html.parser')

                for link in soup.find_all('a', href=True):
                    href = link['href']

                    # DuckDuckGo wraps URLs in redirects
                    if 'uddg=' in href:
                        parsed = urlparse(href)
                        params = parse_qs(parsed.query)
                        if 'uddg' in params:
                            actual_url = unquote(params['uddg'][0])
                            if self._matches_vpro_url(actual_url, media_type):
                                all_urls.append(actual_url)
                    elif self._matches_vpro_url(href, media_type):
                        all_urls.append(href)

            except Exception as e:
                logger.warning(f"DuckDuckGo search failed: {e}")

        # Dedupe while preserving order
        seen = set()
        unique_urls = []
        for url in all_urls:
            if url not in seen:
                seen.add(url)
                unique_urls.append(url)

        logger.info(f"DuckDuckGo: found {len(unique_urls)} URLs for '{title}'")
        return unique_urls[:5]

    def search_startpage(
        self,
        title: str,
        year: Optional[int] = None,
        media_type: str = "all"
    ) -> List[str]:
        """
        Search using Startpage.

        Args:
            title: Title to search for
            year: Optional release year
            media_type: "film", "series", or "all"

        Returns:
            List of matching VPRO URLs
        """
        all_urls = []

        for path in self._get_search_paths(media_type):
            query = f'site:vprogids.nl/{path} "{title}"'
            if year:
                query += f' {year}'

            search_url = (
                f"https://www.startpage.com/sp/search?"
                f"query={quote_plus(query)}&cat=web&language=dutch"
            )
            logger.debug(f"Startpage query: {query}")

            try:
                response = self.session.get(search_url)
                response.raise_for_status()

                if self._is_captcha_page(response.text):
                    logger.warning("Startpage showing CAPTCHA - skipping")
                    continue

                # Extract URLs with regex
                if media_type == "film":
                    pattern = r'https?://(?:www\.)?vprogids\.nl/cinema/films/film~[^"\'&\s<>]+'
                elif media_type == "series":
                    pattern = r'https?://(?:www\.)?vprogids\.nl/cinema/series/serie~[^"\'&\s<>]+'
                else:
                    pattern = (
                        r'https?://(?:www\.)?vprogids\.nl/cinema/'
                        r'(?:films/film|series/serie)~[^"\'&\s<>]+'
                    )

                matches = re.findall(pattern, response.text)

                for url in matches:
                    url = re.sub(r'[&;].*$', '', url).rstrip('.')
                    all_urls.append(url)

            except Exception as e:
                logger.warning(f"Startpage search failed: {e}")

        # Dedupe while preserving order
        seen = set()
        unique_urls = []
        for url in all_urls:
            if url not in seen:
                seen.add(url)
                unique_urls.append(url)

        logger.info(f"Startpage: found {len(unique_urls)} URLs for '{title}'")
        return unique_urls[:5]

    def search(
        self,
        title: str,
        year: Optional[int] = None,
        media_type: str = "all"
    ) -> List[str]:
        """
        Search using multiple engines with fallback.

        Args:
            title: Title to search for
            year: Optional release year
            media_type: "film", "series", or "all"

        Returns:
            List of matching VPRO URLs
        """
        urls = self.search_duckduckgo(title, year, media_type)
        if urls:
            return urls

        urls = self.search_startpage(title, year, media_type)
        if urls:
            return urls

        return []


# Alias for backward compatibility
StartpageSearcher = WebSearcher


# =============================================================================
# VPRO Page Scraper
# =============================================================================

class VPROPageScraper:
    """Scrapes film and series details from VPRO Cinema pages."""

    def __init__(self, session: RateLimitedSession = None):
        """
        Initialize page scraper.

        Args:
            session: Optional shared session for connection pooling.
        """
        self.session = session or create_session(timeout=15)
        self._owns_session = session is None

    def close(self) -> None:
        """Close session if we own it."""
        if self._owns_session:
            self.session.close()

    def scrape(self, url: str) -> Optional[VPROFilm]:
        """
        Scrape film or series details from a VPRO Cinema page.

        Args:
            url: URL of the VPRO Cinema page

        Returns:
            VPROFilm if scraping succeeded, None otherwise
        """
        try:
            response = self.session.get(url)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')

            # Extract title
            title = None
            title_el = soup.find('h1')
            if title_el:
                title = title_el.get_text(strip=True)

            if not title:
                return None

            # Extract description - try multiple sources
            description = None

            # Source 1: Article paragraphs
            article = soup.find('article')
            if article:
                for p in article.find_all('p'):
                    text = p.get_text(strip=True)
                    if len(text) > 100:
                        sanitized = sanitize_description(text)
                        if is_valid_description(sanitized):
                            description = sanitized
                            break

            # Source 2: Intro/description class
            if not description:
                intro = soup.find(class_=re.compile(r'intro|description|body'))
                if intro:
                    sanitized = sanitize_description(intro.get_text(strip=True))
                    if is_valid_description(sanitized):
                        description = sanitized

            # Source 3: Meta description tag (reliable for VPRO pages)
            if not description:
                meta_desc = soup.find('meta', attrs={'name': 'description'})
                if meta_desc and meta_desc.get('content'):
                    sanitized = sanitize_description(meta_desc['content'])
                    if is_valid_description(sanitized):
                        description = sanitized
                        logger.debug(f"Using meta description for {url}")

            # Source 4: OpenGraph description
            if not description:
                og_desc = soup.find('meta', attrs={'property': 'og:description'})
                if og_desc and og_desc.get('content'):
                    sanitized = sanitize_description(og_desc['content'])
                    if is_valid_description(sanitized):
                        description = sanitized
                        logger.debug(f"Using og:description for {url}")

            # Extract year
            year = None
            year_match = re.search(r'\b(19|20)\d{2}\b', soup.get_text())
            if year_match:
                year = int(year_match.group())

            # Detect media type and extract ID from URL
            vpro_id = None
            media_type = "film"

            if 'serie~' in url:
                media_type = "series"
                id_match = re.search(r'serie~(\d+)~', url)
                if id_match:
                    vpro_id = id_match.group(1)
            else:
                id_match = re.search(r'film~(\d+)~', url)
                if id_match:
                    vpro_id = id_match.group(1)

            # Extract director (simple heuristic)
            director = None
            director_match = re.search(
                r'(?:van|by|regie[:\s]+)([A-Z][a-z]+ [A-Z][a-z]+)',
                soup.get_text()
            )
            if director_match:
                director = director_match.group(1)

            return VPROFilm(
                title=title,
                year=year,
                director=director,
                description=description,
                url=url,
                vpro_id=vpro_id,
                media_type=media_type,
            )

        except Exception as e:
            logger.warning(f"Failed to scrape {url}: {e}")
            return None


# =============================================================================
# Core Search Functions
# =============================================================================

def _search_poms_api(
    title: str,
    year: Optional[int] = None,
    director: Optional[str] = None,
    media_type: str = "all",
    session: RateLimitedSession = None,
) -> Optional[VPROFilm]:
    """
    Search VPRO using POMS API only.

    Args:
        title: Title to search for
        year: Optional release year for validation
        director: Optional director for disambiguation
        media_type: "film", "series", or "all"
        session: Optional shared session

    Returns:
        VPROFilm if found, None otherwise
    """
    poms = POMSAPIClient(session=session)

    try:
        items = poms.search(title, max_results=10, media_type=media_type)

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


def _search_web_fallback(
    title: str,
    year: Optional[int] = None,
    media_type: str = "all",
    session: RateLimitedSession = None,
) -> Optional[VPROFilm]:
    """
    Search VPRO using web search engines.

    Args:
        title: Title to search for
        year: Optional release year for validation
        media_type: "film", "series", or "all"
        session: Optional shared session

    Returns:
        VPROFilm if found, None otherwise
    """
    logger.info(f"Trying web search fallback for '{title}'...")

    searcher = WebSearcher(session=session)
    scraper = VPROPageScraper(session=session)

    try:
        with metrics.timer("web_search_duration_ms"):
            urls = searcher.search(title, year, media_type)

        if not urls:
            logger.info(f"Web search: No URLs found for '{title}'")
            return None

        logger.info(f"Web search found {len(urls)} URLs for '{title}'")

        for url in urls:
            film = scraper.scrape(url)
            if film and film.description:
                if year and film.year and abs(film.year - year) > YEAR_TOLERANCE:
                    continue

                logger.info(f"Web fallback: Found - {film.title} ({film.year})")
                metrics.inc("web_fallback_matches")
                return film

    except Exception as e:
        logger.error(f"Web search error: {e}")

    return None


def get_vpro_description(
    title: str,
    year: Optional[int] = None,
    imdb_id: Optional[str] = None,
    director: Optional[str] = None,
    media_type: str = "all",
    verbose: bool = False
) -> Optional[VPROFilm]:
    """
    Search VPRO Cinema for a film or series and return its Dutch description.

    Search Strategy:
        1. Search with original title via POMS API
        2. If no match AND have IMDB ID: try alternate titles from TMDB
        3. Web search fallback (DuckDuckGo, Startpage)

    Args:
        title: Title to search for
        year: Release year (improves matching)
        imdb_id: IMDB ID (enables alternate title lookup)
        director: Director name (for disambiguation)
        media_type: "film", "series", or "all" (default)
        verbose: Enable verbose logging

    Returns:
        VPROFilm object if found, None otherwise
    """
    if verbose:
        logging.basicConfig(level=logging.DEBUG)

    type_str = f" [{media_type}]" if media_type != "all" else ""
    imdb_str = f" [{imdb_id}]" if imdb_id else ""
    logger.info(f"Searching VPRO: '{title}' ({year}){type_str}{imdb_str}")

    metrics.inc("vpro_searches")

    # Create shared session for all requests
    session = create_session(timeout=30)

    try:
        # Track discovered IMDB for diagnostics
        discovered_imdb = None

        # Step 1: Try original title via POMS API
        result = _search_poms_api(title, year, director, media_type, session)
        if result:
            result.lookup_method = "poms"
            metrics.inc("vpro_searches", labels={"result": "found", "method": "poms"})
            return result

        # Step 2: Try alternate titles via TMDB
        tmdb = TMDBClient(session=session)
        alt_titles = []

        if imdb_id:
            # Have IMDB ID - fetch alternate titles directly
            logger.info(f"No POMS match for '{title}' - fetching alternate titles by IMDB...")
            alt_titles = tmdb.get_alternate_titles(imdb_id, media_type)
        else:
            # No IMDB ID - search TMDB by title+year to find original title
            logger.info(f"No POMS match for '{title}' - searching TMDB for alternate titles...")
            discovered_imdb, alt_titles = tmdb.search_by_title(title, year, media_type)
            if discovered_imdb:
                logger.info(f"TMDB found IMDB ID: {discovered_imdb}")

        # Filter out titles we already tried
        alt_titles = [t for t in alt_titles if not titles_match(t, title)]

        for alt_title in alt_titles[:5]:
            logger.info(f"Trying alternate title: '{alt_title}'")

            result = _search_poms_api(alt_title, year, director, media_type, session)
            if result:
                result.lookup_method = "tmdb_alt"
                result.discovered_imdb = discovered_imdb
                logger.info(f"Found via alternate title '{alt_title}': {result.title}")
                metrics.inc("vpro_searches", labels={"result": "found", "method": "tmdb_alt"})
                return result

        # Step 3: Web search fallback
        result = _search_web_fallback(title, year, media_type, session)
        if result:
            result.lookup_method = "web"
            metrics.inc("vpro_searches", labels={"result": "found", "method": "web"})
            return result

        logger.info(f"No VPRO Cinema entry found for '{title}' ({year})")
        metrics.inc("vpro_searches", labels={"result": "not_found"})
        return None

    finally:
        session.close()


# =============================================================================
# CLI
# =============================================================================

def main():
    """Command-line interface for testing."""
    import argparse
    from logging_config import configure_logging

    parser = argparse.ArgumentParser(
        description="Search VPRO Cinema for Dutch film and series descriptions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s "The Matrix" --year 1999
  %(prog)s "Le dernier métro" --year 1980
  %(prog)s "The Last Metro" --year 1980 --imdb tt0080610
  %(prog)s "Adolescence" --year 2025 --type series
  %(prog)s --refresh-credentials
        """
    )
    parser.add_argument("title", nargs='?', help="Title to search")
    parser.add_argument("--year", "-y", type=int, help="Release year")
    parser.add_argument("--imdb", "-i", help="IMDB ID (e.g., tt0080610)")
    parser.add_argument("--director", "-d", help="Director name")
    parser.add_argument(
        "--type", "-t",
        choices=["film", "series", "all"],
        default="all",
        help="Media type to search for (default: all)"
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument(
        "--refresh-credentials",
        action="store_true",
        help="Force refresh of POMS API credentials"
    )
    parser.add_argument("--version", action="version", version="%(prog)s 3.1.0")

    args = parser.parse_args()

    # Configure logging
    configure_logging(level="DEBUG" if args.verbose else "INFO")

    if args.refresh_credentials:
        print("Refreshing POMS API credentials...")
        creds = get_credential_manager()
        creds.delete_cache()
        if creds.invalidate_and_refresh():
            key, secret = creds.get_credentials()
            print(f"Credentials refreshed: key={key[:4]}..., secret={secret[:4]}...")
        else:
            print("Failed to refresh credentials (using defaults)")
        return 0

    if not args.title:
        parser.print_help()
        return 1

    type_str = f" [{args.type}]" if args.type != "all" else ""
    print(f"Searching VPRO Cinema for: {args.title}" +
          (f" ({args.year})" if args.year else "") + type_str)
    print("-" * 60)

    film = get_vpro_description(
        title=args.title,
        year=args.year,
        imdb_id=args.imdb,
        director=args.director,
        media_type=args.type,
        verbose=args.verbose
    )

    if film:
        type_label = "Series" if film.media_type == "series" else "Film"
        print(f"\nFound ({type_label}): {film.title}")
        print(f"  Year: {film.year or 'Unknown'}")
        print(f"  Type: {film.media_type}")
        print(f"  Director: {film.director or 'Unknown'}")
        print(f"  Rating: {film.vpro_rating}/10" if film.vpro_rating else "  Rating: N/A")
        print(f"  VPRO ID: {film.vpro_id or 'Unknown'}")
        print(f"  URL: {film.url or 'Unknown'}")
        print(f"  Genres: {', '.join(film.genres) if film.genres else 'Unknown'}")
        print(f"\n  Description ({len(film.description or '')} chars):")
        desc = film.description or ''
        print(f"  {desc[:500]}..." if len(desc) > 500 else f"  {desc}")
    else:
        print(f"\nNot found in VPRO Cinema")

    return 0 if film else 1


if __name__ == "__main__":
    exit(main())
