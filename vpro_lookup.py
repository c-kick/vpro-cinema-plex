#!/usr/bin/env python3
"""
VPRO Cinema Lookup Orchestrator

Main entry point for searching VPRO Cinema database for Dutch film descriptions.

NOTE: This module only supports MOVIES. TV series support has been removed
because VPRO's data sources don't provide complete series metadata.

Search Strategy:
    1. PRIMARY: NPO POMS API (direct database query via authenticated REST API)
    2. ALTERNATE TITLES: If no match and IMDB ID available, fetch alternate
       titles from TMDB and retry POMS search
    3. FALLBACK: Direct cinema.nl search with IMDB verification

Note: vprogids.nl/cinema has migrated to cinema.nl.

Usage:
    from vpro_lookup import get_vpro_description

    # Search for a film
    film = get_vpro_description("The Matrix", year=1999)
    if film:
        print(film.description)
"""

import logging
from typing import Optional

from credentials import get_credential_manager
from http_client import create_session
from metrics import metrics
from models import VPROFilm
from poms_client import TMDBClient, search_poms_api
from text_utils import titles_match
from vpro_scraper import search_cinema_fallback

logger = logging.getLogger(__name__)


# =============================================================================
# Main Orchestrator
# =============================================================================

def get_vpro_description(
    title: str,
    year: Optional[int] = None,
    imdb_id: Optional[str] = None,
    director: Optional[str] = None,
    verbose: bool = False,
    skip_poms: bool = False,
    skip_tmdb: bool = False,
) -> Optional[VPROFilm]:
    """
    Search VPRO Cinema for a film and return its Dutch description.

    NOTE: Only movies are supported. TV series support has been removed.

    Search Strategy:
        1. Search with original title via POMS API (unless skip_poms=True)
        2. If no match AND have IMDB ID: try alternate titles from TMDB (unless skip_tmdb=True)
        3. Cinema.nl direct search with IMDB verification

    Args:
        title: Title to search for
        year: Release year (improves matching)
        imdb_id: IMDB ID (enables alternate title lookup and verification)
        director: Director name (for disambiguation)
        verbose: Enable verbose logging
        skip_poms: Skip POMS API, go directly to fallback (for testing)
        skip_tmdb: Skip TMDB alternate title lookup (for testing)

    Returns:
        VPROFilm object if found, None otherwise
    """
    if verbose:
        logging.basicConfig(level=logging.DEBUG)

    imdb_str = f" [{imdb_id}]" if imdb_id else ""
    logger.info(f"Searching VPRO: '{title}' ({year}){imdb_str}")

    metrics.inc("vpro_searches")

    # Create shared session for all requests
    session = create_session(timeout=30)

    try:
        # Track discovered IMDB for diagnostics
        discovered_imdb = None
        alt_titles = []

        # Step 1: Try original title via POMS API (unless skipped)
        if not skip_poms:
            result = search_poms_api(title, year, director, session=session, imdb_id=imdb_id)
            if result:
                result.lookup_method = "poms"
                metrics.inc("vpro_searches", labels={"result": "found", "method": "poms"})
                return result
        else:
            logger.info(f"Skipping POMS API (skip_poms=True)")

        # Step 2: Try alternate titles via TMDB (unless skipped)
        if not skip_tmdb:
            tmdb = TMDBClient(session=session)

            if imdb_id:
                # Have IMDB ID - fetch alternate titles directly
                logger.info(f"No POMS match for '{title}' - fetching alternate titles by IMDB...")
                alt_titles = tmdb.get_alternate_titles(imdb_id)
            else:
                # No IMDB ID - search TMDB by title+year to find original title
                logger.info(f"No POMS match for '{title}' - searching TMDB for alternate titles...")
                discovered_imdb, alt_titles = tmdb.search_by_title(title, year)
                if discovered_imdb:
                    logger.info(f"TMDB found IMDB ID: {discovered_imdb}")

            # Filter out titles we already tried
            alt_titles = [t for t in alt_titles if not titles_match(t, title)]

            if not skip_poms:
                for alt_title in alt_titles[:5]:
                    logger.info(f"Trying alternate title: '{alt_title}'")

                    result = search_poms_api(alt_title, year, director, session=session, imdb_id=imdb_id)
                    if result:
                        result.lookup_method = "tmdb_alt"
                        result.discovered_imdb = discovered_imdb
                        logger.info(f"Found via alternate title '{alt_title}': {result.title}")
                        metrics.inc("vpro_searches", labels={"result": "found", "method": "tmdb_alt"})
                        return result
        else:
            logger.info(f"Skipping TMDB alternate titles (skip_tmdb=True)")

        # Step 3: Cinema.nl direct search with IMDB verification
        result = search_cinema_fallback(
            title=title,
            year=year,
            imdb_id=imdb_id or discovered_imdb,
            alt_titles=alt_titles,
            session=session
        )
        if result:
            # lookup_method already set by search_cinema_fallback
            metrics.inc("vpro_searches", labels={"result": "found", "method": result.lookup_method})
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
    parser.add_argument("title", nargs='?', help="Title to search")
    parser.add_argument("--year", "-y", type=int, help="Release year")
    parser.add_argument("--imdb", "-i", help="IMDB ID (e.g., tt0080610)")
    parser.add_argument("--director", "-d", help="Director name")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument(
        "--refresh-credentials",
        action="store_true",
        help="Force refresh of POMS API credentials"
    )
    parser.add_argument(
        "--skip-poms",
        action="store_true",
        help="Skip POMS API, test cinema.nl fallback directly"
    )
    parser.add_argument(
        "--skip-tmdb",
        action="store_true",
        help="Skip TMDB alternate title lookup"
    )
    parser.add_argument("--version", action="version", version="%(prog)s 4.1.1")

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

    skip_info = []
    if args.skip_poms:
        skip_info.append("POMS")
    if args.skip_tmdb:
        skip_info.append("TMDB")
    skip_str = f" [SKIP: {', '.join(skip_info)}]" if skip_info else ""

    print(f"Searching VPRO Cinema for: {args.title}" +
          (f" ({args.year})" if args.year else "") + skip_str)
    print("-" * 60)

    film = get_vpro_description(
        title=args.title,
        year=args.year,
        imdb_id=args.imdb,
        director=args.director,
        verbose=args.verbose,
        skip_poms=args.skip_poms,
        skip_tmdb=args.skip_tmdb,
    )

    if film:
        method_label = getattr(film, 'lookup_method', 'unknown') or 'unknown'
        print(f"\nFound via {method_label}: {film.title}")
        print(f"  Year: {film.year or 'Unknown'}")
        print(f"  Lookup Method: {method_label}")
        print(f"  Director: {film.director or 'Unknown'}")
        print(f"  Rating: {film.vpro_rating}/10" if film.vpro_rating else "  Rating: N/A")
        print(f"  Age Rating: {film.content_rating}" if film.content_rating else "  Age Rating: N/A")
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
