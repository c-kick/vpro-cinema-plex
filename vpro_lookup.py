#!/usr/bin/env python3
"""
VPRO Cinema Lookup Orchestrator

Main entry point for searching VPRO Cinema database for Dutch film
and TV series descriptions.

Search Strategy:
    1. PRIMARY: NPO POMS API (direct database query via authenticated REST API)
    2. FALLBACK: Web search (DuckDuckGo -> Startpage) + page scraping
    3. ALTERNATE TITLES: If no match and IMDB ID available, fetch alternate
       titles from TMDB and retry search

Usage:
    from vpro_lookup import get_vpro_description

    # Search for a film
    film = get_vpro_description("The Matrix", year=1999)
    if film:
        print(film.description)

    # Search specifically for a series
    series = get_vpro_description("Adolescence", year=2025, media_type="series")
    if series:
        print(series.description)
"""

import logging
from typing import Optional

from credentials import get_credential_manager
from http_client import create_session
from metrics import metrics
from models import VPROFilm
from poms_client import TMDBClient, search_poms_api
from text_utils import titles_match
from vpro_scraper import search_web_fallback

logger = logging.getLogger(__name__)


# =============================================================================
# Main Orchestrator
# =============================================================================

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
        result = search_poms_api(title, year, director, media_type, session, imdb_id)
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

            result = search_poms_api(alt_title, year, director, media_type, session, imdb_id)
            if result:
                result.lookup_method = "tmdb_alt"
                result.discovered_imdb = discovered_imdb
                logger.info(f"Found via alternate title '{alt_title}': {result.title}")
                metrics.inc("vpro_searches", labels={"result": "found", "method": "tmdb_alt"})
                return result

        # Step 3: Web search fallback
        result = search_web_fallback(title, year, media_type, session)
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
    parser.add_argument("--version", action="version", version="%(prog)s 3.1.2")

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


# Re-export VPROFilm for convenience
__all__ = [
    'get_vpro_description',
    'VPROFilm',
]
