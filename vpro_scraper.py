"""
VPRO Cinema Web Scraper

Web search fallback for finding VPRO Cinema pages when the POMS API
doesn't return results. Uses DuckDuckGo and Startpage as search engines.
"""

import logging
import re
from typing import Optional, List
from urllib.parse import quote_plus, parse_qs, urlparse, unquote

from bs4 import BeautifulSoup

from constants import YEAR_TOLERANCE
from http_client import RateLimitedSession, create_session
from metrics import metrics
from models import VPROFilm
from text_utils import sanitize_description, is_valid_description

logger = logging.getLogger(__name__)


# =============================================================================
# Web Search
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
# Web Search Function
# =============================================================================

def search_web_fallback(
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


__all__ = [
    'WebSearcher',
    'VPROPageScraper',
    'StartpageSearcher',
    'search_web_fallback',
]
