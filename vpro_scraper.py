"""
VPRO Cinema Web Scraper

Direct cinema.nl scraper for finding VPRO Cinema pages when the POMS API
doesn't return results. Searches cinema.nl directly with IMDB verification.

Note: vprogids.nl/cinema has migrated to cinema.nl.
"""

import logging
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional, List

from bs4 import BeautifulSoup
from urllib.parse import quote_plus

from constants import (
    YEAR_TOLERANCE,
    CINEMA_SEARCH_URL,
    CINEMA_BASE_URL,
)
from http_client import RateLimitedSession, create_session, SessionAwareComponent
from metrics import metrics
from models import VPROFilm
from text_utils import (
    sanitize_description,
    is_valid_description,
    extract_year_from_text,
    titles_match,
    title_similarity,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Match Confidence Levels
# =============================================================================

class MatchConfidence(str, Enum):
    """Confidence level for cinema.nl matches."""
    IMDB_EXACT = "cinema_imdb"      # IMDB matched - 100% reliable
    TITLE_YEAR = "cinema_title"     # Title+year matched - high confidence


# =============================================================================
# Search Candidates
# =============================================================================

@dataclass
class SearchCandidate:
    """Pre-filtered search result from cinema.nl cards."""
    url: str
    title: str
    year: Optional[int] = None
    rating: Optional[int] = None  # Star rating (1-5) visible in card


# =============================================================================
# Circuit Breaker
# =============================================================================

class CircuitBreaker:
    """
    Simple circuit breaker to prevent hammering cinema.nl on failures.

    After 3 consecutive failures within 60 seconds, the circuit opens
    and requests are skipped for 5 minutes.
    """

    def __init__(self, failure_threshold: int = 3, failure_window: float = 60.0,
                 recovery_time: float = 300.0):
        self.failure_threshold = failure_threshold
        self.failure_window = failure_window
        self.recovery_time = recovery_time
        self._failures: List[float] = []
        self._circuit_opened_at: Optional[float] = None

    def is_open(self) -> bool:
        """Check if circuit is open (blocking requests)."""
        if self._circuit_opened_at is None:
            return False

        # Check if recovery time has passed
        if time.monotonic() - self._circuit_opened_at >= self.recovery_time:
            self._circuit_opened_at = None
            self._failures.clear()
            logger.info("Cinema.nl circuit breaker closed (recovered)")
            return False

        return True

    def record_success(self) -> None:
        """Record a successful request."""
        self._failures.clear()
        if self._circuit_opened_at is not None:
            self._circuit_opened_at = None
            logger.info("Cinema.nl circuit breaker closed (success)")

    def record_failure(self) -> None:
        """Record a failed request."""
        now = time.monotonic()

        # Remove old failures outside the window
        self._failures = [t for t in self._failures if now - t < self.failure_window]
        self._failures.append(now)

        # Check if we should open the circuit
        if len(self._failures) >= self.failure_threshold:
            self._circuit_opened_at = now
            logger.warning(
                f"Cinema.nl circuit breaker opened after {len(self._failures)} failures"
            )


# Global circuit breaker instance
_circuit_breaker = CircuitBreaker()


# =============================================================================
# Cinema.nl Searcher
# =============================================================================

class CinemaSearcher(SessionAwareComponent):
    """
    Search cinema.nl directly for films and series.

    Optimizations:
    - Include year in query for better ranking
    - Use model=cinema to filter to films/series only (excludes articles)
    - Extract year from card text for pre-filtering
    - Extract rating from card (avoid extra scrape)
    - Limit to max 3 detail page scrapes

    Timeout budget: 5s per request, max 3 scrapes = 15s worst case.
    """

    MAX_CANDIDATES = 3  # Limit detail page scrapes
    REQUEST_TIMEOUT = 5  # Seconds per request

    def __init__(self, session: RateLimitedSession = None):
        """
        Initialize cinema searcher.

        Args:
            session: Optional shared session for connection pooling.
        """
        self.init_session(session, timeout=self.REQUEST_TIMEOUT)

    def search(self, title: str, year: Optional[int] = None) -> List[SearchCandidate]:
        """
        Search cinema.nl and return pre-filtered candidates.

        URL: https://www.cinema.nl/zoeken?q={title}+{year}&model=cinema

        Args:
            title: Title to search for
            year: Optional release year (included in query for better ranking)

        Returns:
            List of SearchCandidate objects, pre-filtered by year tolerance
        """
        # Build search query - include year for better ranking
        query_parts = [title]
        if year:
            query_parts.append(str(year))
        query = " ".join(query_parts)

        # Use model=cinema to filter to films/series only
        search_url = f"{CINEMA_SEARCH_URL}?q={quote_plus(query)}&model=cinema"
        logger.debug(f"Cinema.nl search: {search_url}")

        try:
            response = self.session.get(search_url)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, 'html.parser')
            candidates = self._parse_search_cards(soup, year)

            logger.info(
                f"Cinema.nl search: \"{query}\" -> {len(candidates)} candidates"
            )

            return candidates[:self.MAX_CANDIDATES]

        except Exception as e:
            logger.warning(f"Cinema.nl search failed: {e}")
            return []

    def _parse_search_cards(self, soup: BeautifulSoup,
                            target_year: Optional[int]) -> List[SearchCandidate]:
        """
        Extract candidates from search result cards.

        Pre-filters by year tolerance BEFORE returning to avoid
        scraping pages that can't possibly match.

        Args:
            soup: Parsed search results page
            target_year: Target year for filtering (if any)

        Returns:
            List of SearchCandidate objects within year tolerance
        """
        candidates = []

        # Find the card list - cinema.nl uses CardList class
        card_list = soup.find('ul', class_='CardList')
        if not card_list:
            # Try alternative selectors
            card_list = soup.find('ul', class_=re.compile(r'CardList|card-list|results'))

        if not card_list:
            logger.debug("No CardList found in search results")
            return []

        for item in card_list.find_all('li', class_=re.compile(r'CardList-item|card')):
            link = item.find('a', href=True)
            if not link:
                continue

            href = link.get('href', '')

            # Only process cinema database entries (/db/{id}-{slug})
            if not href.startswith('/db/'):
                continue

            url = CINEMA_BASE_URL + href

            # Extract title from card
            title_el = item.find(['h2', 'h3', 'h4']) or item.find(class_=re.compile(r'title|heading'))
            title = title_el.get_text(strip=True) if title_el else ""

            if not title:
                # Try link text as fallback
                title = link.get_text(strip=True)

            if not title:
                continue

            # Extract year from card metadata (look for "• YYYY •" pattern)
            card_text = item.get_text()
            year = None

            # Try structured metadata first
            year_match = re.search(r'[•·]\s*(\d{4})\s*[•·]', card_text)
            if year_match:
                year = int(year_match.group(1))
            else:
                # Fallback to any 4-digit year in card
                year = extract_year_from_text(card_text)

            # Extract rating from card if visible (avoid detail scrape)
            rating = None
            rating_match = re.search(r'(\d)\s*(?:van|/)\s*5', card_text)
            if rating_match:
                rating = int(rating_match.group(1))

            # Pre-filter by year tolerance
            if target_year and year:
                if abs(year - target_year) > YEAR_TOLERANCE:
                    logger.debug(
                        f"Skipping {title} ({year}) - year mismatch with {target_year}"
                    )
                    continue

            candidates.append(SearchCandidate(
                url=url,
                title=title,
                year=year,
                rating=rating,
            ))

        if target_year:
            logger.debug(
                f"Pre-filtered to {len(candidates)} candidates (year tolerance ±{YEAR_TOLERANCE})"
            )

        return candidates


# =============================================================================
# VPRO Page Scraper
# =============================================================================

class VPROPageScraper(SessionAwareComponent):
    """
    Scrapes film and series details from cinema.nl pages.

    Handles the new cinema.nl URL format: /db/{id}-{slug}
    """

    # IMDB pattern - robust to match various URL formats
    IMDB_PATTERN = re.compile(r'https?://(?:www\.)?imdb\.com/title/(tt\d{7,10})')
    REQUEST_TIMEOUT = 5  # Seconds per request

    def __init__(self, session: RateLimitedSession = None):
        """
        Initialize page scraper.

        Args:
            session: Optional shared session for connection pooling.
        """
        self.init_session(session, timeout=self.REQUEST_TIMEOUT)

    def scrape(self, url: str) -> Optional[VPROFilm]:
        """
        Scrape film or series details from a cinema.nl page.

        Args:
            url: URL of the cinema.nl page

        Returns:
            VPROFilm if scraping succeeded, None otherwise
        """
        try:
            response = self.session.get(url)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, 'html.parser')
            page_text = soup.get_text()

            # Extract title from h1
            title = None
            title_el = soup.find('h1')
            if title_el:
                title = title_el.get_text(strip=True)

            if not title:
                logger.debug(f"No title found on {url}")
                return None

            # Extract description from blockquote in Recensie section
            description = self._extract_description(soup)

            # Extract year from page metadata
            year = self._extract_year(soup, page_text)

            # Extract VPRO rating (X van 5 sterren -> convert to /10)
            vpro_rating = self._extract_rating(page_text)

            # Extract Kijkwijzer content rating
            content_rating = self._extract_content_rating(page_text)

            # Extract IMDB ID from page
            imdb_id = self._extract_imdb(soup)

            # Extract VPRO ID from URL (/db/{id}-{slug})
            vpro_id = None
            id_match = re.search(r'/db/(\d+)-', url)
            if id_match:
                vpro_id = id_match.group(1)

            # Detect media type from page
            media_type = self._detect_media_type(soup, page_text)

            # Extract director
            director = self._extract_director(soup, page_text)

            # Extract genres
            genres = self._extract_genres(soup, page_text)

            # Extract images
            images = self._extract_images(soup, title)

            return VPROFilm(
                title=title,
                year=year,
                director=director,
                description=description,
                url=url,
                imdb_id=imdb_id,
                vpro_id=vpro_id,
                genres=genres,
                images=images,
                vpro_rating=vpro_rating,
                content_rating=content_rating,
                media_type=media_type,
            )

        except Exception as e:
            logger.warning(f"Failed to scrape {url}: {e}")
            return None

    def _extract_description(self, soup: BeautifulSoup) -> Optional[str]:
        """Extract description from page, preferring blockquote in review."""
        description = None

        # Source 1: Blockquote (review excerpt)
        blockquote = soup.find('blockquote')
        if blockquote:
            text = blockquote.get_text(strip=True)
            sanitized = sanitize_description(text)
            if is_valid_description(sanitized):
                description = sanitized

        # Source 2: Article paragraphs
        if not description:
            article = soup.find('article')
            if article:
                for p in article.find_all('p'):
                    text = p.get_text(strip=True)
                    if len(text) > 100:
                        sanitized = sanitize_description(text)
                        if is_valid_description(sanitized):
                            description = sanitized
                            break

        # Source 3: Intro/description class
        if not description:
            intro = soup.find(class_=re.compile(r'intro|description|body|review'))
            if intro:
                sanitized = sanitize_description(intro.get_text(strip=True))
                if is_valid_description(sanitized):
                    description = sanitized

        # Source 4: Meta description tag
        if not description:
            meta_desc = soup.find('meta', attrs={'name': 'description'})
            if meta_desc and meta_desc.get('content'):
                sanitized = sanitize_description(meta_desc['content'])
                if is_valid_description(sanitized):
                    description = sanitized
                    logger.debug(f"Using meta description")

        # Source 5: OpenGraph description
        if not description:
            og_desc = soup.find('meta', attrs={'property': 'og:description'})
            if og_desc and og_desc.get('content'):
                sanitized = sanitize_description(og_desc['content'])
                if is_valid_description(sanitized):
                    description = sanitized
                    logger.debug(f"Using og:description")

        return description

    def _extract_year(self, soup: BeautifulSoup, page_text: str) -> Optional[int]:
        """Extract release year from page."""
        # Try structured metadata first (film • YYYY • genre pattern)
        year_match = re.search(r'(?:film|serie)\s*[•·]\s*(\d{4})', page_text.lower())
        if year_match:
            return int(year_match.group(1))

        # Try metadata section
        meta = soup.find(class_=re.compile(r'meta|credits|info'))
        if meta:
            year = extract_year_from_text(meta.get_text())
            if year:
                return year

        # Fallback to page text
        return extract_year_from_text(page_text)

    def _extract_rating(self, page_text: str) -> Optional[int]:
        """Extract VPRO rating (5-star to 10-point conversion)."""
        rating_match = re.search(r'(\d+)\s*van\s*5\s*sterren', page_text.lower())
        if rating_match:
            stars = int(rating_match.group(1))
            return stars * 2  # Convert to 10-point scale
        return None

    def _extract_content_rating(self, page_text: str) -> Optional[str]:
        """Extract Kijkwijzer content rating."""
        # Look for Kijkwijzer patterns (AL, 6, 9, 12, 14, 16, 18)
        kijkwijzer_match = re.search(r'\b(AL|6|9|12|14|16|18)\+?(?:\s|$)', page_text)
        if kijkwijzer_match:
            rating = kijkwijzer_match.group(1)
            return rating
        return None

    def _extract_imdb(self, soup: BeautifulSoup) -> Optional[str]:
        """Extract IMDB ID from page links."""
        # Look for IMDB links
        for link in soup.find_all('a', href=True):
            match = self.IMDB_PATTERN.search(link['href'])
            if match:
                return match.group(1)

        # Also check page text for IMDB URLs
        page_html = str(soup)
        match = self.IMDB_PATTERN.search(page_html)
        if match:
            return match.group(1)

        return None

    def _detect_media_type(self, soup: BeautifulSoup, page_text: str) -> str:
        """Detect if page is for a film or series."""
        text_lower = page_text.lower()

        # First check structured metadata (most reliable)
        # Look for "film • YYYY" or "serie • YYYY" patterns
        if re.search(r'\bfilm\s*[•·]', text_lower):
            return "film"
        if re.search(r'\bserie\s*[•·]', text_lower):
            return "series"

        # Check for series-specific indicators (must be standalone words)
        # Exclude partial matches like "miniserie" containing "serie"
        series_patterns = [
            r'\bserie\b(?![\w])',  # "serie" but not "miniserie"
            r'\bseizoen\s*\d',     # "seizoen 1", "seizoen 2"
            r'\baflever',          # "aflevering"
            r'\bepisode\s*\d',     # "episode 1"
            r'\bseason\s*\d',      # "season 1"
        ]
        for pattern in series_patterns:
            if re.search(pattern, text_lower):
                return "series"

        return "film"

    def _extract_director(self, soup: BeautifulSoup, page_text: str) -> Optional[str]:
        """Extract director name from page."""
        # Look for director in credits section
        credits = soup.find(class_=re.compile(r'credits|crew'))
        if credits:
            # Look for "Regie:" or "Regisseur:" pattern
            director_match = re.search(
                r'(?:regie|regisseur|director)[:\s]+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)',
                credits.get_text()
            )
            if director_match:
                return director_match.group(1)

        # Fallback: search page text
        director_match = re.search(
            r'(?:regie|regisseur|van|by|director)[:\s]+([A-Z][a-z]+ [A-Z][a-z]+)',
            page_text
        )
        if director_match:
            return director_match.group(1)

        return None

    def _extract_genres(self, soup: BeautifulSoup, page_text: str) -> List[str]:
        """Extract genres from page."""
        genres = []

        # Common Dutch film genres to look for
        known_genres = [
            'actie', 'avontuur', 'animatie', 'biografie', 'comedy', 'misdaad',
            'documentaire', 'drama', 'familie', 'fantasy', 'film-noir',
            'geschiedenis', 'horror', 'muziek', 'musical', 'mysterie',
            'romantiek', 'sciencefiction', 'sci-fi', 'sport', 'thriller',
            'oorlog', 'western', 'komedie', 'romantisch', 'actiefilm',
            'dramafilm', 'horrorfilm', 'tragikomedie'
        ]

        # Look in credits/metadata section first
        meta = soup.find(class_=re.compile(r'credits|meta|genre|info|details'))
        search_text = meta.get_text().lower() if meta else page_text.lower()

        # Also check structured metadata (genre • genre pattern)
        genre_section = re.search(r'[•·]\s*([^•·]+?)\s*[•·]', search_text)
        if genre_section:
            potential_genres = genre_section.group(1).split(',')
            for g in potential_genres:
                g = g.strip().lower()
                if g in known_genres:
                    genres.append(g.capitalize())

        # Fallback: search for known genres in text
        if not genres:
            for genre in known_genres:
                if re.search(rf'\b{genre}\b', search_text):
                    genres.append(genre.capitalize())

        # Deduplicate while preserving order
        seen = set()
        unique_genres = []
        for g in genres:
            g_lower = g.lower()
            if g_lower not in seen:
                seen.add(g_lower)
                unique_genres.append(g)

        return unique_genres[:5]  # Limit to 5 genres

    def _extract_images(self, soup: BeautifulSoup, title: str) -> List[dict]:
        """
        Extract images from cinema.nl page.

        Only extracts images from the ImageCluster div that follows
        the <h2>Afbeeldingen</h2> heading to avoid unrelated images
        from news articles etc.

        Args:
            soup: Parsed page HTML
            title: Film title for image metadata

        Returns:
            List of image dicts with type, url, and title
        """
        images = []
        seen_urls = set()

        def add_image(url: str, img_type: str = "PICTURE", img_title: str = None):
            """Add image if not already seen."""
            if not url or url in seen_urls:
                return
            # Only accept vpro.nl images
            if 'vpro.nl' not in url:
                return
            seen_urls.add(url)
            images.append({
                "type": img_type,
                "url": url,
                "title": img_title or title
            })

        # Find the "Afbeeldingen" heading and get the ImageCluster that follows it
        afbeeldingen_heading = soup.find('h2', string=re.compile(r'Afbeeldingen', re.IGNORECASE))
        if afbeeldingen_heading:
            # Look for ImageCluster as next sibling or within next sibling
            next_elem = afbeeldingen_heading.find_next_sibling()
            while next_elem:
                # Check if this element is or contains an ImageCluster
                if next_elem.get('class') and 'ImageCluster' in ' '.join(next_elem.get('class', [])):
                    image_cluster = next_elem
                    break
                image_cluster = next_elem.find(class_='ImageCluster')
                if image_cluster:
                    break
                # Stop if we hit another heading (section ended)
                if next_elem.name in ['h1', 'h2', 'h3']:
                    break
                next_elem = next_elem.find_next_sibling()
            else:
                image_cluster = None

            if image_cluster:
                # Extract images from the cluster
                # Cinema.nl uses simple src attributes with thumbnail URLs
                # URL format: https://images.vpro.nl/{id}/ex_0,ey_77,eh_846,ew_1504,w_160/{filename}.webp
                # We can request larger versions by changing w_160 to w_1920
                for img in image_cluster.find_all('img'):
                    src = img.get('src') or img.get('data-src')
                    if src and 'vpro.nl' in src:
                        # Upgrade thumbnail to larger size by replacing width param
                        # w_160 -> w_1920 for full size
                        large_url = re.sub(r',w_\d+/', ',w_1920/', src)
                        if large_url == src:
                            # No width param found, try different pattern
                            large_url = re.sub(r'/w_\d+/', '/w_1920/', src)

                        alt = img.get('alt', '')
                        add_image(large_url, "PICTURE", alt if alt else title)

                logger.debug(f"Extracted {len(images)} images from Afbeeldingen section")

        return images[:10]  # Limit to 10 images


# =============================================================================
# Cinema.nl Fallback Search
# =============================================================================

def search_cinema_fallback(
    title: str,
    year: Optional[int] = None,
    imdb_id: Optional[str] = None,
    alt_titles: List[str] = None,
    media_type: str = "all",
    session: RateLimitedSession = None,
) -> Optional[VPROFilm]:
    """
    Search cinema.nl with IMDB verification.

    Strategy:
    1. Search with "{title} {year}" for best ranking
    2. Pre-filter candidates by year from cards (max 3)
    3. Scrape each candidate's detail page
    4. Match by IMDB (exact) or title+year (fallback)

    Args:
        title: Title to search for
        year: Optional release year
        imdb_id: Optional IMDB ID for verification
        alt_titles: Optional list of alternate titles to try
        media_type: "film", "series", or "all"
        session: Optional shared session

    Returns:
        VPROFilm with lookup_method set to confidence level, or None
    """
    # Check circuit breaker
    if _circuit_breaker.is_open():
        logger.info("Cinema.nl circuit breaker is open - skipping fallback")
        return None

    logger.info(f"Trying cinema.nl fallback for '{title}'...")

    searcher = CinemaSearcher(session=session)
    scraper = VPROPageScraper(session=session)

    # Build list of titles to try
    titles_to_try = [title]
    if alt_titles:
        titles_to_try.extend(alt_titles[:5])  # Limit alternate titles

    try:
        with metrics.timer("cinema_search_duration_ms"):
            for search_title in titles_to_try:
                candidates = searcher.search(search_title, year)

                if not candidates:
                    continue

                logger.info(
                    f"Cinema.nl: {len(candidates)} candidates for '{search_title}'"
                )

                for candidate in candidates:
                    film = scraper.scrape(candidate.url)

                    if not film or not film.description:
                        continue

                    # Check for match
                    match_type = _check_match(
                        film, title, year, imdb_id, alt_titles
                    )

                    if match_type:
                        film.lookup_method = match_type.value
                        _circuit_breaker.record_success()
                        metrics.inc("cinema_fallback_matches", labels={"type": match_type.value})
                        logger.info(
                            f"Cinema.nl match ({match_type.value}): "
                            f"{film.title} ({film.year})"
                        )
                        return film

                logger.debug(f"No match found for '{search_title}'")

        # No matches found
        _circuit_breaker.record_failure()
        logger.info(f"Cinema.nl fallback: No match for '{title}'")
        return None

    except Exception as e:
        _circuit_breaker.record_failure()
        logger.error(f"Cinema.nl fallback error: {e}")
        return None


def _check_match(
    film: VPROFilm,
    target_title: str,
    target_year: Optional[int],
    target_imdb: Optional[str],
    alt_titles: Optional[List[str]] = None,
) -> Optional[MatchConfidence]:
    """
    Check if scraped film matches the target.

    Returns the confidence level of the match, or None if no match.
    """
    # Priority 1: IMDB match (exact, 100% reliable)
    if target_imdb and film.imdb_id:
        if target_imdb.lower() == film.imdb_id.lower():
            logger.debug(f"IMDB match: {target_imdb}")
            return MatchConfidence.IMDB_EXACT
        else:
            # IMDB mismatch - definitely not the same film
            logger.debug(f"IMDB mismatch: {target_imdb} != {film.imdb_id}")
            return None

    # Priority 2: Title + year match
    all_titles = [target_title]
    if alt_titles:
        all_titles.extend(alt_titles)

    for check_title in all_titles:
        if titles_match(film.title, check_title):
            # Title matches - check year
            if target_year and film.year:
                if abs(target_year - film.year) <= YEAR_TOLERANCE:
                    logger.debug(f"Title+year match: {check_title} ({film.year})")
                    return MatchConfidence.TITLE_YEAR
            elif not target_year:
                # No year to check, title match is sufficient
                logger.debug(f"Title match (no year): {check_title}")
                return MatchConfidence.TITLE_YEAR

    # Fallback: fuzzy title match with high similarity
    for check_title in all_titles:
        similarity = title_similarity(film.title, check_title)
        if similarity >= 0.8:  # High threshold for fuzzy match
            if target_year and film.year:
                if abs(target_year - film.year) <= YEAR_TOLERANCE:
                    logger.debug(
                        f"Fuzzy title+year match: {film.title} ~ {check_title} "
                        f"(similarity: {similarity:.2f})"
                    )
                    return MatchConfidence.TITLE_YEAR

    return None


__all__ = [
    'CinemaSearcher',
    'VPROPageScraper',
    'SearchCandidate',
    'MatchConfidence',
    'search_cinema_fallback',
]
