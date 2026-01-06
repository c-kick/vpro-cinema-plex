#!/usr/bin/env python3
"""
VPRO Cinema Scraper v2.5.0
==========================

Searches VPRO Cinema database for Dutch film descriptions.

Search Strategy:
    1. PRIMARY: NPO POMS API (direct database query via authenticated REST API)
    2. FALLBACK: Web search (DuckDuckGo → Startpage) + page scraping
    3. ALTERNATE TITLES: If no match and IMDB ID available, fetch alternate
       titles from TMDB and retry search

Credential Management:
    - Credentials are auto-extracted from vprogids.nl if API returns 401/403
    - Cached to credentials.json for persistence
    - Falls back to hardcoded defaults if extraction fails

Environment Variables:
    TMDB_API_KEY: API key for TMDB alternate titles lookup (optional but recommended)
    POMS_CACHE_FILE: Path to POMS credentials cache (default: ./credentials.json)

Usage:
    from vpro_cinema_scraper import get_vpro_description
    
    film = get_vpro_description("The Matrix", year=1999)
    if film:
        print(film.description)
"""

import hmac
import hashlib
import base64
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple
from urllib.parse import urlencode, quote_plus, parse_qs, urlparse, unquote

import requests
from bs4 import BeautifulSoup

# Configure logging
logger = logging.getLogger(__name__)

# =============================================================================
# Configuration
# =============================================================================

TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")
POMS_CACHE_FILE = os.environ.get("POMS_CACHE_FILE", "./credentials.json")

# Hardcoded fallback credentials (from vprogids.nl as of Jan 2026)
DEFAULT_API_KEY = "ione7ahfij"
DEFAULT_API_SECRET = "aag9veesei"


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class VPROFilm:
    """Represents a film with VPRO Cinema metadata."""
    title: str
    year: Optional[int] = None
    director: Optional[str] = None
    description: Optional[str] = None
    url: Optional[str] = None
    imdb_id: Optional[str] = None
    vpro_id: Optional[str] = None
    genres: List[str] = field(default_factory=list)
    vpro_rating: Optional[int] = None
    
    def to_dict(self) -> Dict[str, Any]:
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
        }


# =============================================================================
# Credential Manager - Auto-extracts credentials from vprogids.nl
# =============================================================================

class CredentialManager:
    """
    Manages POMS API credentials with automatic refresh capability.
    
    Credentials are embedded in vprogids.nl frontend JavaScript.
    If the API returns 401/403, this class can scrape fresh credentials.
    
    Credential sources (in order of priority):
        1. Cached credentials from credentials.json
        2. Fresh extraction from vprogids.nl
        3. Hardcoded fallback defaults
    """
    
    CREDENTIAL_URL = "https://www.vprogids.nl/cinema/zoek.html"
    
    # Patterns to find credentials in JavaScript
    # These match various ways the credentials might be defined
    CREDENTIAL_PATTERNS = [
        # Pattern: vpronlApiKey = "xxx" or vpronlSecret = "xxx"
        (r'vpronlApiKey\s*[=:]\s*["\']([^"\']+)["\']', r'vpronlSecret\s*[=:]\s*["\']([^"\']+)["\']'),
        # Pattern: apiKey: "xxx", secret: "xxx"  
        (r'apiKey\s*[=:]\s*["\']([^"\']+)["\']', r'(?:apiSecret|secret)\s*[=:]\s*["\']([^"\']+)["\']'),
        # Pattern: "apiKey":"xxx"
        (r'"apiKey"\s*:\s*"([^"]+)"', r'"(?:apiSecret|secret)"\s*:\s*"([^"]+)"'),
        # Pattern: key: 'xxx' (in config objects)
        (r'key\s*:\s*["\']([a-z0-9]{8,12})["\']', r'secret\s*:\s*["\']([a-z0-9]{8,12})["\']'),
    ]
    
    def __init__(self, cache_file: str = None):
        self.cache_file = cache_file or POMS_CACHE_FILE
        self._api_key: Optional[str] = None
        self._api_secret: Optional[str] = None
        self._load_cached()
    
    def _load_cached(self) -> bool:
        """Load credentials from cache file."""
        if not os.path.exists(self.cache_file):
            return False
        
        try:
            with open(self.cache_file, 'r') as f:
                data = json.load(f)
            
            self._api_key = data.get("api_key")
            self._api_secret = data.get("api_secret")
            
            if self._api_key and self._api_secret:
                fetched_at = data.get("fetched_at", "unknown")
                logger.debug(f"Loaded cached credentials (fetched: {fetched_at})")
                return True
        except Exception as e:
            logger.warning(f"Failed to load cached credentials: {e}")
        
        return False
    
    def _save_cache(self):
        """Save current credentials to cache file."""
        if not self._api_key or not self._api_secret:
            return
        
        try:
            # Ensure directory exists
            cache_dir = os.path.dirname(self.cache_file)
            if cache_dir:
                os.makedirs(cache_dir, exist_ok=True)
            
            data = {
                "api_key": self._api_key,
                "api_secret": self._api_secret,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "source": self.CREDENTIAL_URL,
            }
            
            with open(self.cache_file, 'w') as f:
                json.dump(data, f, indent=2)
            
            logger.info(f"Saved credentials to {self.cache_file}")
        except Exception as e:
            logger.warning(f"Failed to save credentials cache: {e}")
    
    def _extract_from_page(self, html: str) -> Tuple[Optional[str], Optional[str]]:
        """Extract API credentials from page HTML/JavaScript."""
        api_key = None
        api_secret = None
        
        for key_pattern, secret_pattern in self.CREDENTIAL_PATTERNS:
            if not api_key:
                key_match = re.search(key_pattern, html, re.IGNORECASE)
                if key_match:
                    api_key = key_match.group(1)
            
            if not api_secret:
                secret_match = re.search(secret_pattern, html, re.IGNORECASE)
                if secret_match:
                    api_secret = secret_match.group(1)
            
            if api_key and api_secret:
                break
        
        return api_key, api_secret
    
    def fetch_fresh_credentials(self) -> bool:
        """
        Fetch fresh credentials from vprogids.nl.
        
        Returns:
            True if credentials were successfully extracted and cached
        """
        logger.info("Fetching fresh API credentials from vprogids.nl...")
        
        try:
            session = requests.Session()
            session.headers.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'nl-NL,nl;q=0.9,en;q=0.8',
            })
            
            response = session.get(self.CREDENTIAL_URL, timeout=15)
            response.raise_for_status()
            
            # Search in HTML and inline scripts
            api_key, api_secret = self._extract_from_page(response.text)
            
            # Also check linked JavaScript files
            if not (api_key and api_secret):
                soup = BeautifulSoup(response.text, 'html.parser')
                for script in soup.find_all('script', src=True):
                    src = script['src']
                    if not src.startswith('http'):
                        src = f"https://www.vprogids.nl{src}" if src.startswith('/') else f"https://www.vprogids.nl/{src}"
                    
                    try:
                        js_response = session.get(src, timeout=10)
                        if js_response.ok:
                            key, secret = self._extract_from_page(js_response.text)
                            if key and not api_key:
                                api_key = key
                            if secret and not api_secret:
                                api_secret = secret
                            
                            if api_key and api_secret:
                                break
                    except Exception:
                        continue
            
            if api_key and api_secret:
                logger.info(f"Extracted fresh credentials: key={api_key[:4]}..., secret={api_secret[:4]}...")
                self._api_key = api_key
                self._api_secret = api_secret
                self._save_cache()
                return True
            else:
                logger.warning("Could not extract credentials from vprogids.nl")
                return False
                
        except Exception as e:
            logger.error(f"Failed to fetch credentials: {e}")
            return False
    
    @property
    def api_key(self) -> str:
        """Get current API key, falling back to default."""
        return self._api_key or DEFAULT_API_KEY
    
    @property
    def api_secret(self) -> str:
        """Get current API secret, falling back to default."""
        return self._api_secret or DEFAULT_API_SECRET
    
    def invalidate(self):
        """Mark current credentials as invalid, forcing refresh on next use."""
        logger.info("Invalidating cached credentials")
        self._api_key = None
        self._api_secret = None
        
        if os.path.exists(self.cache_file):
            try:
                os.remove(self.cache_file)
            except OSError:
                pass
    
    def refresh_if_needed(self) -> bool:
        """Refresh credentials if none are cached."""
        if self._api_key and self._api_secret:
            return True
        return self.fetch_fresh_credentials()


# Global credential manager instance
_credential_manager: Optional[CredentialManager] = None


def get_credential_manager() -> CredentialManager:
    """Get or create the global credential manager."""
    global _credential_manager
    if _credential_manager is None:
        _credential_manager = CredentialManager()
    return _credential_manager


# =============================================================================
# TMDB API Client - For Alternate Titles
# =============================================================================

class TMDBClient:
    """Client for TMDB API to fetch alternate titles."""
    
    BASE_URL = "https://api.themoviedb.org/3"
    
    def __init__(self, api_key: str = None, timeout: int = 10):
        self.api_key = api_key or TMDB_API_KEY
        self.timeout = timeout
        self.session = requests.Session()
    
    def _get(self, endpoint: str, params: dict = None) -> Optional[dict]:
        if not self.api_key:
            return None
        
        params = params or {}
        params["api_key"] = self.api_key
        
        try:
            url = f"{self.BASE_URL}{endpoint}"
            response = self.session.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.warning(f"TMDB API error: {e}")
            return None
    
    def find_by_imdb(self, imdb_id: str) -> Optional[int]:
        """Find TMDB movie ID from IMDB ID."""
        data = self._get(f"/find/{imdb_id}", {"external_source": "imdb_id"})
        if data and data.get("movie_results"):
            return data["movie_results"][0].get("id")
        return None
    
    def get_alternate_titles(self, imdb_id: str) -> List[str]:
        """
        Get alternate titles for a movie by IMDB ID.
        Prioritizes French, Dutch, Belgian, and German titles for VPRO searches.
        """
        if not self.api_key:
            logger.debug("TMDB API key not configured, skipping alternate titles")
            return []
        
        tmdb_id = self.find_by_imdb(imdb_id)
        if not tmdb_id:
            logger.debug(f"Could not find TMDB ID for {imdb_id}")
            return []
        
        details = self._get(f"/movie/{tmdb_id}")
        alt_data = self._get(f"/movie/{tmdb_id}/alternative_titles")
        
        titles = []
        seen = set()
        
        def add_title(t: str):
            if t and t.lower() not in seen:
                seen.add(t.lower())
                titles.append(t)
        
        # Priority 1: Original title
        if details:
            add_title(details.get("original_title"))
        
        # Priority 2: Preferred language titles
        preferred_countries = ["FR", "NL", "BE", "DE"]
        
        if alt_data and alt_data.get("titles"):
            for country in preferred_countries:
                for t in alt_data["titles"]:
                    if t.get("iso_3166_1") == country:
                        add_title(t.get("title"))
            
            for t in alt_data["titles"]:
                add_title(t.get("title"))
        
        logger.info(f"TMDB alternate titles for {imdb_id}: {titles[:5]}{'...' if len(titles) > 5 else ''}")
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
    
    API_BASE = "https://rs.poms.omroep.nl/v1/api"
    ORIGIN = "https://www.vprogids.nl"
    PROFILE = "vprocinema"
    
    def __init__(self, timeout: int = 30, credential_manager: CredentialManager = None):
        self.timeout = timeout
        self.creds = credential_manager or get_credential_manager()
        self.session = requests.Session()
        self.session.headers.update({
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0 (compatible; VPROCinemaProvider/2.5)',
        })
    
    def _get_npo_date(self) -> str:
        return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    
    def _get_parameters_string(self, params: Dict[str, str]) -> str:
        if not params:
            return ""
        sorted_keys = sorted(params.keys())
        parts = []
        for key in sorted_keys:
            if key != "iecomp":
                parts.append(f",{key}:{params[key]}")
        return "".join(parts)
    
    def _get_credentials(self, headers: Dict[str, str], path: str, params: Dict[str, str] = None) -> str:
        message_parts = [f"origin:{self.ORIGIN}"]
        
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
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": self.ORIGIN,
            "x-npo-date": self._get_npo_date(),
        }
        
        credentials = self._get_credentials(headers, path, params)
        headers["Authorization"] = f"NPO {self.creds.api_key}:{credentials}"
        
        return headers
    
    def _do_search(self, query: str, max_results: int) -> Tuple[requests.Response, str, Dict]:
        """Execute search request, returning response and request details for retry."""
        path = "pages/"
        params = {"profile": self.PROFILE, "max": str(max_results)}
        
        body = {
            "highlight": True,
            "searches": {"text": query},
            "facets": {"types": {"include": "MOVIE"}}
        }
        
        headers = self._get_headers(path, params)
        url = f"{self.API_BASE}/{path}?{urlencode(params)}"
        
        response = self.session.post(url, headers=headers, json=body, timeout=self.timeout)
        return response, path, params
    
    def search(self, query: str, max_results: int = 10) -> List[Dict[str, Any]]:
        """
        Search VPRO Cinema database for films.
        
        Automatically refreshes credentials on 401/403 and retries once.
        """
        try:
            response, path, params = self._do_search(query, max_results)
            
            # Check for auth failure
            if response.status_code in (401, 403):
                logger.warning(f"POMS API auth failed ({response.status_code}) - refreshing credentials...")
                
                # Invalidate and fetch fresh credentials
                self.creds.invalidate()
                if self.creds.fetch_fresh_credentials():
                    # Retry with new credentials
                    logger.info("Retrying with fresh credentials...")
                    response, _, _ = self._do_search(query, max_results)
                    
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
            logger.debug(f"POMS API returned {len(items)} items for '{query}'")
            return items
            
        except requests.Timeout:
            logger.error(f"POMS API request timed out after {self.timeout}s")
            return []
        except requests.ConnectionError as e:
            logger.error(f"POMS API connection failed: {e}")
            return []
        except requests.RequestException as e:
            logger.error(f"POMS API request failed: {e}")
            return []
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"POMS API response parsing failed: {e}")
            return []
    
    def parse_film(self, item: Dict[str, Any]) -> Optional[VPROFilm]:
        """Parse API response item into VPROFilm object."""
        result = item.get("result", {})
        
        if result.get("type") != "MOVIE":
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
        
        genres = [g.get("displayName", "") for g in result.get("genres", []) if g.get("displayName")]
        
        description = None
        paragraphs = result.get("paragraphs", [])
        if paragraphs:
            description = paragraphs[0].get("body", "")
        
        vpro_id = None
        url = result.get("url", "")
        if url:
            match = re.search(r'film~(\d+)~', url)
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
        )


# =============================================================================
# Web Search Fallback
# =============================================================================

class WebSearcher:
    """Search VPRO Cinema via web search engines."""
    
    def __init__(self, timeout: int = 15):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'nl-NL,nl;q=0.9,en;q=0.8',
        })
    
    def search_duckduckgo(self, title: str, year: Optional[int] = None) -> List[str]:
        """Search using DuckDuckGo HTML."""
        query = f'site:vprogids.nl/cinema/films "{title}"'
        if year:
            query += f' {year}'
        
        search_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        logger.debug(f"DuckDuckGo query: {query}")
        
        try:
            response = self.session.get(search_url, timeout=self.timeout)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            urls = []
            
            for link in soup.find_all('a', href=True):
                href = link['href']
                
                # DuckDuckGo wraps URLs in redirects
                if 'uddg=' in href:
                    parsed = urlparse(href)
                    params = parse_qs(parsed.query)
                    if 'uddg' in params:
                        actual_url = unquote(params['uddg'][0])
                        if 'vprogids.nl/cinema/films/film~' in actual_url:
                            urls.append(actual_url)
                elif 'vprogids.nl/cinema/films/film~' in href:
                    urls.append(href)
            
            # Dedupe
            seen = set()
            unique_urls = []
            for url in urls:
                if url not in seen:
                    seen.add(url)
                    unique_urls.append(url)
            
            logger.info(f"DuckDuckGo: found {len(unique_urls)} URLs for '{title}'")
            return unique_urls[:5]
            
        except requests.RequestException as e:
            logger.warning(f"DuckDuckGo search failed: {e}")
            return []
    
    def search_startpage(self, title: str, year: Optional[int] = None) -> List[str]:
        """Search using Startpage."""
        query = f'site:vprogids.nl/cinema/films "{title}"'
        if year:
            query += f' {year}'
        
        search_url = f"https://www.startpage.com/sp/search?query={quote_plus(query)}&cat=web&language=dutch"
        logger.debug(f"Startpage query: {query}")
        
        time.sleep(0.5)  # Rate limiting
        
        try:
            response = self.session.get(search_url, timeout=self.timeout)
            response.raise_for_status()
            
            if 'captcha' in response.text.lower() or 'robot' in response.text.lower():
                logger.warning("Startpage showing CAPTCHA - skipping")
                return []
            
            # Extract URLs
            urls = []
            pattern = r'https?://(?:www\.)?vprogids\.nl/cinema/films/film~[^"\'&\s<>]+'
            matches = re.findall(pattern, response.text)
            
            seen = set()
            for url in matches:
                url = re.sub(r'[&;].*$', '', url).rstrip('.')
                if url not in seen:
                    seen.add(url)
                    urls.append(url)
            
            logger.info(f"Startpage: found {len(urls)} URLs for '{title}'")
            return urls[:5]
            
        except requests.RequestException as e:
            logger.warning(f"Startpage search failed: {e}")
            return []
    
    def search(self, title: str, year: Optional[int] = None) -> List[str]:
        """Search using multiple engines."""
        urls = self.search_duckduckgo(title, year)
        if urls:
            return urls
        
        urls = self.search_startpage(title, year)
        if urls:
            return urls
        
        return []


# Alias for backward compatibility
StartpageSearcher = WebSearcher


# =============================================================================
# VPRO Page Scraper
# =============================================================================

class VPROPageScraper:
    """Scrapes film details from VPRO Cinema film pages."""
    
    def __init__(self, timeout: int = 15):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'text/html,application/xhtml+xml',
            'Accept-Language': 'nl-NL,nl;q=0.9',
        })
    
    def scrape(self, url: str) -> Optional[VPROFilm]:
        """Scrape film details from a VPRO Cinema film page."""
        try:
            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            
            title = None
            title_el = soup.find('h1')
            if title_el:
                title = title_el.get_text(strip=True)
            
            if not title:
                return None
            
            description = None
            article = soup.find('article')
            if article:
                for p in article.find_all('p'):
                    text = p.get_text(strip=True)
                    if len(text) > 100:
                        description = text
                        break
            
            if not description:
                intro = soup.find(class_=re.compile(r'intro|description|body'))
                if intro:
                    description = intro.get_text(strip=True)
            
            year = None
            year_match = re.search(r'\b(19|20)\d{2}\b', soup.get_text())
            if year_match:
                year = int(year_match.group())
            
            vpro_id = None
            id_match = re.search(r'film~(\d+)~', url)
            if id_match:
                vpro_id = id_match.group(1)
            
            director = None
            director_match = re.search(r'(?:van|by|regie[:\s]+)([A-Z][a-z]+ [A-Z][a-z]+)', soup.get_text())
            if director_match:
                director = director_match.group(1)
            
            return VPROFilm(
                title=title,
                year=year,
                director=director,
                description=description,
                url=url,
                vpro_id=vpro_id,
            )
            
        except Exception as e:
            logger.warning(f"Failed to scrape {url}: {e}")
            return None


# =============================================================================
# Helper Functions
# =============================================================================

def _normalize_title(title: str) -> str:
    return re.sub(r'[^\w\s]', '', title.lower()).strip()


def _titles_match(title1: str, title2: str) -> bool:
    return _normalize_title(title1) == _normalize_title(title2)


def _title_similarity(title1: str, title2: str) -> float:
    """Jaccard similarity between titles."""
    words1 = set(_normalize_title(title1).split())
    words2 = set(_normalize_title(title2).split())
    
    if not words1 or not words2:
        return 0.0
    
    intersection = words1 & words2
    union = words1 | words2
    return len(intersection) / len(union)


# =============================================================================
# Core Search Functions
# =============================================================================

def _search_vpro_single_title(
    title: str,
    year: Optional[int] = None,
    director: Optional[str] = None,
) -> Optional[VPROFilm]:
    """Search VPRO for a single title using POMS API with web fallback."""
    
    # Strategy 1: POMS API
    try:
        poms = POMSAPIClient(timeout=30)
        items = poms.search(title, max_results=10)
        
        if items:
            logger.debug(f"POMS API returned {len(items)} results for '{title}'")
            
            films = [poms.parse_film(item) for item in items]
            films = [f for f in films if f]
            
            if films:
                # Exact title + year match
                if year:
                    for film in films:
                        if film.year == year and _titles_match(film.title, title):
                            logger.info(f"POMS: Exact match - {film.title} ({film.year})")
                            return film
                
                # Title match only
                for film in films:
                    if _titles_match(film.title, title):
                        logger.info(f"POMS: Title match - {film.title} ({film.year})")
                        return film
                
                # Validate top result
                best = films[0]
                similarity = _title_similarity(title, best.title)
                year_diff = abs(best.year - year) if (best.year and year) else 0
                
                if year and year_diff > 2:
                    logger.warning(f"POMS: Rejecting '{best.title}' ({best.year}) - year diff {year_diff}")
                elif similarity < 0.3:
                    logger.warning(f"POMS: Rejecting '{best.title}' - low similarity {similarity:.0%}")
                else:
                    logger.info(f"POMS: Using top result - {best.title} ({best.year})")
                    return best
        else:
            logger.debug(f"POMS: No results for '{title}'")
            
    except Exception as e:
        logger.error(f"POMS API error: {e}")
    
    # Strategy 2: Web search fallback
    logger.info(f"Trying web search fallback for '{title}'...")
    
    try:
        searcher = WebSearcher(timeout=15)
        urls = searcher.search(title, year)
        
        if urls:
            logger.info(f"Web search found {len(urls)} URLs for '{title}'")
            scraper = VPROPageScraper(timeout=15)
            
            for url in urls:
                film = scraper.scrape(url)
                if film and film.description:
                    if year and film.year and abs(film.year - year) > 2:
                        continue
                    
                    logger.info(f"Web fallback: Found - {film.title} ({film.year})")
                    return film
        else:
            logger.info(f"Web search: No URLs found for '{title}'")
            
    except Exception as e:
        logger.error(f"Web search error: {e}")
    
    return None


def get_vpro_description(
    title: str,
    year: Optional[int] = None,
    imdb_id: Optional[str] = None,
    director: Optional[str] = None,
    verbose: bool = False
) -> Optional[VPROFilm]:
    """
    Search VPRO Cinema for a film and return its Dutch description.
    
    Search Strategy:
        1. Search with original title (POMS API + web fallback)
        2. If no match AND have IMDB ID: fetch alternate titles from TMDB
        3. Try each alternate title until match found
    
    Args:
        title: Film title to search for
        year: Release year (improves matching)
        imdb_id: IMDB ID (enables alternate title lookup)
        director: Director name (for disambiguation)
        verbose: Enable verbose logging
    
    Returns:
        VPROFilm object if found, None otherwise
    """
    if verbose:
        logging.basicConfig(level=logging.DEBUG)
    
    logger.info(f"Searching VPRO: '{title}' ({year})" + (f" [{imdb_id}]" if imdb_id else ""))
    
    # Step 1: Try original title
    result = _search_vpro_single_title(title, year, director)
    if result:
        return result
    
    # Step 2: Try alternate titles via TMDB
    if imdb_id:
        logger.info(f"No match for '{title}' - fetching alternate titles from TMDB...")
        
        tmdb = TMDBClient()
        alt_titles = tmdb.get_alternate_titles(imdb_id)
        
        alt_titles = [t for t in alt_titles if not _titles_match(t, title)]
        
        for alt_title in alt_titles[:5]:
            logger.info(f"Trying alternate title: '{alt_title}'")
            
            result = _search_vpro_single_title(alt_title, year, director)
            if result:
                logger.info(f"Found via alternate title '{alt_title}': {result.title}")
                return result
    
    logger.info(f"No VPRO Cinema entry found for '{title}' ({year})")
    return None


# =============================================================================
# CLI
# =============================================================================

def main():
    """Command-line interface for testing."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Search VPRO Cinema for Dutch film descriptions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s "The Matrix" --year 1999
  %(prog)s "Le dernier métro" --year 1980
  %(prog)s "The Last Metro" --year 1980 --imdb tt0080610
  %(prog)s --refresh-credentials
        """
    )
    parser.add_argument("title", nargs='?', help="Film title to search")
    parser.add_argument("--year", "-y", type=int, help="Release year")
    parser.add_argument("--imdb", "-i", help="IMDB ID (e.g., tt0080610)")
    parser.add_argument("--director", "-d", help="Director name")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--refresh-credentials", action="store_true", 
                        help="Force refresh of POMS API credentials")
    parser.add_argument("--version", action="version", version="%(prog)s 2.5.0")
    
    args = parser.parse_args()
    
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format='%(levelname)s: %(message)s')
    else:
        logging.basicConfig(level=logging.INFO, format='%(message)s')
    
    if args.refresh_credentials:
        print("Refreshing POMS API credentials...")
        creds = get_credential_manager()
        creds.invalidate()
        if creds.fetch_fresh_credentials():
            print(f"✓ Credentials refreshed: key={creds.api_key[:4]}..., secret={creds.api_secret[:4]}...")
        else:
            print("✗ Failed to refresh credentials (using defaults)")
        return 0
    
    if not args.title:
        parser.print_help()
        return 1
    
    print(f"Searching VPRO Cinema for: {args.title}" + (f" ({args.year})" if args.year else ""))
    print("-" * 60)
    
    film = get_vpro_description(
        title=args.title,
        year=args.year,
        imdb_id=args.imdb,
        director=args.director,
        verbose=args.verbose
    )
    
    if film:
        print(f"\n✓ Found: {film.title}")
        print(f"  Year: {film.year or 'Unknown'}")
        print(f"  Director: {film.director or 'Unknown'}")
        print(f"  Rating: {film.vpro_rating}/10" if film.vpro_rating else "  Rating: N/A")
        print(f"  VPRO ID: {film.vpro_id or 'Unknown'}")
        print(f"  URL: {film.url or 'Unknown'}")
        print(f"  Genres: {', '.join(film.genres) if film.genres else 'Unknown'}")
        print(f"\n  Description ({len(film.description or '')} chars):")
        desc = film.description or ''
        print(f"  {desc[:500]}..." if len(desc) > 500 else f"  {desc}")
    else:
        print(f"\n✗ Not found in VPRO Cinema")
    
    return 0 if film else 1


if __name__ == "__main__":
    exit(main())
