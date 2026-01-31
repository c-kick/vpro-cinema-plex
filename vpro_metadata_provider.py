#!/usr/bin/env python3
"""
VPRO Cinema Plex Metadata Provider v4.0.2

A custom Plex metadata provider that fetches Dutch film descriptions
from VPRO Cinema.

NOTE: This provider only supports MOVIES. TV series support has been removed
because VPRO's data sources don't provide complete series metadata (seasons,
episodes, episode descriptions), which causes Plex scanning failures.

Architecture:
    /library/metadata/matches (POST)
        - Returns IMMEDIATELY with basic match (title, year, ratingKey)
        - NO search - encodes title/year/imdb_id in ratingKey for later lookup
        - This prevents Plex UI timeouts

    /library/metadata/{ratingKey} (GET)
        - Does the actual VPRO lookup (Plex waits up to 90 seconds here)
        - Checks file cache first
        - Uses NPO POMS API (primary) with web search fallback
        - If VPRO not found, returns TMDB fallback metadata so Plex can still add the movie
        - Returns summary if found, omits summary for fallback to secondary provider

Environment Variables:
    PORT: Server port (default: 5100)
    LOG_LEVEL: Logging level (default: INFO)
    CACHE_DIR: Cache directory (default: ./cache)
    TMDB_API_KEY: TMDB API key for alternate titles lookup and fallback metadata (recommended)
"""

import os
import re
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from flask import Flask, request, jsonify, g

from constants import (
    MediaType,
    CacheStatus,
    PROVIDER_IDENTIFIER,
    PROVIDER_TITLE,
    PROVIDER_VERSION,
    VPRO_RETURN_SUMMARY,
    VPRO_RETURN_CONTENT_RATING,
    VPRO_RETURN_IMAGES,
    VPRO_RETURN_RATING,
)
from cache import FileCache, CacheEntry
from credentials import get_credential_manager
from text_utils import (
    normalize_for_cache_key,
    validate_rating_key,
    extract_imdb_from_text,
    extract_year_from_text,
)
from logging_config import configure_logging, setup_flask_request_id, get_request_id
from metrics import metrics
from vpro_lookup import get_vpro_description
from poms_client import search_poms_multiple, TMDBClient

# =============================================================================
# Configuration
# =============================================================================

PORT = int(os.environ.get("PORT", 5100))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
CACHE_DIR = os.environ.get("CACHE_DIR", "./cache")
STRUCTURED_LOGGING = os.environ.get("STRUCTURED_LOGGING", "").lower() == "true"

# Configure logging
configure_logging(level=LOG_LEVEL, structured=STRUCTURED_LOGGING)
logger = logging.getLogger(__name__)

# Initialize cache
cache = FileCache(CACHE_DIR)

# Match request log for troubleshooting
MATCH_LOG_FILE = Path(CACHE_DIR) / "match_requests.jsonl"
MAX_MATCH_LOG_ENTRIES = 1000  # Rotate after this many entries

# Flask app
app = Flask(__name__)
setup_flask_request_id(app)


# =============================================================================
# Match Request Logging
# =============================================================================

def log_match_request(
    raw_data: dict,
    title: str,
    year: Optional[int],
    imdb_id: Optional[str],
    media_type: str,
    rating_key: str,
    filename: str = "",
    guid: str = "",
):
    """
    Log match request for troubleshooting mismatches.

    Writes to JSONL file with automatic rotation.
    """
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "title": title,
        "year": year,
        "imdb_id": imdb_id,
        "media_type": media_type,
        "rating_key": rating_key,
        "filename": filename,
        "guid": guid,
    }

    try:
        # Ensure directory exists
        MATCH_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

        # Rotate if too large
        if MATCH_LOG_FILE.exists():
            line_count = sum(1 for _ in open(MATCH_LOG_FILE, 'r', encoding='utf-8'))
            if line_count >= MAX_MATCH_LOG_ENTRIES:
                # Keep last half of entries
                with open(MATCH_LOG_FILE, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                with open(MATCH_LOG_FILE, 'w', encoding='utf-8') as f:
                    f.writelines(lines[len(lines) // 2:])

        # Append new entry
        with open(MATCH_LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')

    except Exception as e:
        logger.warning(f"Failed to log match request: {e}")


# =============================================================================
# Rating Key Generation & Parsing
# =============================================================================

def generate_rating_key(
    title: str,
    year: Optional[int] = None,
    imdb_id: Optional[str] = None,
) -> str:
    """
    Generate a rating key encoding title, year, and IMDB ID.

    Format: vpro-{sanitized_title}-{year}-{imdb_id}-m
    The 'm' suffix indicates movie (kept for backward compatibility).

    Args:
        title: Content title
        year: Release year (optional)
        imdb_id: IMDB ID (optional)

    Returns:
        Encoded rating key string
    """
    sanitized_title = normalize_for_cache_key(title) or "unknown"
    year_str = str(year) if year else "0"
    imdb_str = imdb_id.lower() if imdb_id else "none"
    return f"vpro-{sanitized_title}-{year_str}-{imdb_str}-m"


# Rating key pattern: vpro-{title}-{year}-{imdb|none}-{m|s}
# Examples: vpro-die-hard-1988-tt0095016-m, vpro-breaking-bad-2008-none-s
RATING_KEY_PATTERN = re.compile(
    r'^vpro-(?P<title>.+)-(?P<year>\d+)-(?P<imdb>tt\d+|none)-(?P<type>[ms])$'
)


def parse_rating_key(rating_key: str) -> dict:
    """
    Parse a rating key back into components.

    Format: vpro-{title}-{year}-{imdb|none}-{m|s}

    Backwards compatible: keys without type suffix or with 's' suffix
    are now all treated as films (TV series support removed).

    Args:
        rating_key: Rating key to parse

    Returns:
        Dict with title, year, imdb_id
    """
    result = {"title": None, "year": None, "imdb_id": None}

    if not rating_key or not rating_key.startswith("vpro-"):
        return result

    # Try the standard format first (most common)
    match = RATING_KEY_PATTERN.match(rating_key)
    if match:
        result["title"] = match.group("title").replace("-", " ")
        year_val = int(match.group("year"))
        result["year"] = year_val if year_val > 0 else None
        imdb = match.group("imdb")
        result["imdb_id"] = imdb if imdb != "none" else None
        # Note: type suffix is ignored now - all entries treated as movies
        return result

    # Fallback: Parse legacy format without type suffix
    key_part = rating_key[5:]  # Remove "vpro-"

    # Extract IMDB ID from end
    imdb_match = re.search(r'-(tt\d+)$', key_part)
    if imdb_match:
        result["imdb_id"] = imdb_match.group(1)
        key_part = key_part[:imdb_match.start()]
    elif key_part.endswith("-none"):
        key_part = key_part[:-5]

    # Extract year
    year_match = re.search(r'-(\d{4})$', key_part)
    if year_match:
        year_val = int(year_match.group(1))
        if year_val > 0:
            result["year"] = year_val
        key_part = key_part[:year_match.start()]
    elif key_part.endswith("-0"):
        key_part = key_part[:-2]

    # Remaining is title
    if key_part:
        result["title"] = key_part.replace("-", " ").strip()

    return result


# =============================================================================
# Unified Metadata Handler
# =============================================================================

@dataclass
class MetadataRequest:
    """Metadata request parameters."""
    rating_key: str

    @property
    def identifier(self) -> str:
        """Get provider identifier."""
        return PROVIDER_IDENTIFIER

    @property
    def base_path(self) -> str:
        """Get base URL path."""
        return "/movies"


def _build_media_container(
    identifier: str,
    items: list = None,
    item_key: str = "Metadata",
) -> dict:
    """
    Build a Plex MediaContainer response.

    Factory function to reduce duplication in response building.

    Args:
        identifier: Provider identifier
        items: List of items (metadata, images, etc.)
        item_key: Key name for items ("Metadata", "Image", etc.)

    Returns:
        MediaContainer dict
    """
    items = items or []
    return {
        "MediaContainer": {
            "offset": 0,
            "totalSize": len(items),
            "identifier": identifier,
            "size": len(items),
            item_key: items
        }
    }


def _build_metadata_response(
    req: MetadataRequest,
    entry: CacheEntry,
) -> dict:
    """
    Build Plex-compatible metadata response.

    Args:
        req: Metadata request parameters
        entry: Cache entry with metadata

    Returns:
        Plex MediaContainer response dict
    """
    plex_type = MediaType(entry.media_type).to_plex_type_str()

    metadata = {
        "ratingKey": req.rating_key,
        "key": f"{req.base_path}/library/metadata/{req.rating_key}",
        "guid": f"{req.identifier}://{plex_type}/{req.rating_key}",
        "type": plex_type,
        "title": entry.title,
    }

    # Include year if available
    if entry.year:
        metadata["year"] = int(entry.year)

    # Only include summary if enabled and we have a description
    if VPRO_RETURN_SUMMARY and entry.description:
        metadata["summary"] = entry.description

    # Include Kijkwijzer content rating if enabled and available
    if VPRO_RETURN_CONTENT_RATING and entry.content_rating:
        metadata["contentRating"] = f"nl/{entry.content_rating}"

    # Include VPRO rating if enabled and available
    # NOTE: Plex may store this value but displays icons based on library settings,
    # not custom providers. See README for details on this Plex limitation.
    if VPRO_RETURN_RATING and entry.vpro_rating:
        metadata["audienceRating"] = float(entry.vpro_rating)

    # Build external GUIDs
    guids = []
    if entry.imdb_id:
        guids.append({"id": f"imdb://{entry.imdb_id}"})
    if entry.vpro_id:
        guids.append({"id": f"vpro://{entry.vpro_id}"})
    if guids:
        metadata["Guid"] = guids

    return _build_media_container(req.identifier, [metadata])


def _build_empty_response(identifier: str) -> dict:
    """Build empty response for errors or not-found cases."""
    return _build_media_container(identifier)


def _map_vpro_image_type(vpro_type: str) -> str:
    """Map VPRO image type to Plex type."""
    if vpro_type == "PROMO_PORTRAIT":
        return "poster"
    return "art"  # backdrop/fanart for PICTURE, PROMO_LANDSCAPE, etc.


def _build_images_response(rating_key: str, identifier: str) -> dict:
    """
    Build images response from cached data.

    Returns VPRO images if VPRO_RETURN_IMAGES is enabled and images exist in cache,
    otherwise returns empty.
    """
    if not VPRO_RETURN_IMAGES:
        return _build_media_container(identifier, item_key="Image")

    # Try to get images from cache
    cached = cache.read(rating_key)
    if not cached or not cached.images:
        return _build_media_container(identifier, item_key="Image")

    # Build Plex image list
    plex_images = [
        {
            "type": _map_vpro_image_type(img.get("type", "PICTURE")),
            "url": img.get("url"),
            "ratingKey": f"{rating_key}-img-{i}",
        }
        for i, img in enumerate(cached.images)
    ]

    return _build_media_container(identifier, plex_images, item_key="Image")


def handle_metadata_request(req: MetadataRequest) -> dict:
    """
    Metadata handler for movie endpoints.

    Args:
        req: Metadata request parameters

    Returns:
        Plex-compatible response dict
    """
    request_id = get_request_id()
    logger.info(f"Metadata request: {req.rating_key}")

    # Validate rating key to prevent path traversal
    if not validate_rating_key(req.rating_key):
        logger.warning(f"Invalid rating key rejected: {req.rating_key}")
        metrics.inc("invalid_rating_keys")
        return _build_empty_response(req.identifier)

    # Check cache first
    cached = cache.read(req.rating_key)
    if cached:
        # Handle cached not-found entries (1-hour TTL - reduced from 7 days)
        if cached.status == CacheStatus.NOT_FOUND.value:
            logger.info(f"Cache hit (not-found) for {req.rating_key}")
            metrics.inc("cache_hits", labels={"status": "not_found"})
            return _build_empty_response(req.identifier)
        logger.info(f"Cache hit for {req.rating_key}")
        metrics.inc("cache_hits", labels={"status": "found"})
        return _build_metadata_response(req, cached)

    metrics.inc("cache_misses")

    # Parse rating key to get search parameters
    parsed = parse_rating_key(req.rating_key)
    title = parsed.get("title")
    year = parsed.get("year")
    imdb_id = parsed.get("imdb_id")

    if not title:
        logger.warning(f"Could not parse title from rating key: {req.rating_key}")
        return _build_empty_response(req.identifier)

    logger.info(f"Cache miss - searching: title='{title}', year={year}, imdb={imdb_id}")

    # Perform the VPRO lookup (movies only)
    try:
        with metrics.timer("vpro_lookup_duration_ms"):
            film = get_vpro_description(
                title=title,
                year=year,
                imdb_id=imdb_id,
            )
    except Exception as e:
        logger.error(f"VPRO search error: {e}")
        film = None

    # Build and cache result
    if film and film.description:
        entry = CacheEntry.from_vpro_film(film)
        cache.write(req.rating_key, entry)
        lookup_info = f" via {film.lookup_method}" if film.lookup_method else ""
        logger.info(f"Found: {film.title} ({film.year}) - {len(film.description)} chars{lookup_info}")
        metrics.inc("vpro_found")
        return _build_metadata_response(req, entry)
    else:
        # VPRO not found - try TMDB fallback for basic metadata
        # This allows Plex to still add the movie with minimal info
        tmdb_fallback = _try_tmdb_fallback(title, year, imdb_id)
        if tmdb_fallback:
            cache.write(req.rating_key, tmdb_fallback)
            logger.info(f"TMDB fallback: {title} ({year}) - basic metadata provided")
            metrics.inc("tmdb_fallback")
            return _build_metadata_response(req, tmdb_fallback)

        # Neither VPRO nor TMDB found - cache not-found with short TTL (1 hour)
        # This prevents hammering APIs on library refresh while allowing
        # quick retries for newly indexed content
        not_found_entry = CacheEntry.not_found(title, year, imdb_id)
        cache.write(req.rating_key, not_found_entry)
        logger.info(f"Not found: {title} ({year}) - cached for 1 hour, omitting summary for fallback")
        metrics.inc("vpro_not_found")
        return _build_empty_response(req.identifier)


def _try_tmdb_fallback(title: str, year: Optional[int], imdb_id: Optional[str]) -> Optional[CacheEntry]:
    """
    Try to get basic metadata from TMDB when VPRO search fails.

    This provides fallback metadata so Plex can still add movies not in VPRO's database.
    TMDB is already used for alternate title lookup, so we leverage that connection.

    Args:
        title: Movie title
        year: Release year (optional)
        imdb_id: IMDB ID (optional)

    Returns:
        CacheEntry with basic TMDB metadata, or None if not found
    """
    try:
        tmdb = TMDBClient()
        if not tmdb.api_key:
            logger.debug("TMDB API key not configured, skipping fallback")
            return None

        # If we have IMDB ID, use it to find the movie
        if imdb_id:
            tmdb_id, detected_type = tmdb.find_by_imdb(imdb_id, "film")
            if tmdb_id and detected_type == "film":
                # Get basic movie details from TMDB
                details = tmdb._get(f"/movie/{tmdb_id}")
                if details:
                    tmdb_title = details.get("title", title)
                    tmdb_year = None
                    if details.get("release_date"):
                        try:
                            tmdb_year = int(details["release_date"][:4])
                        except (ValueError, IndexError):
                            tmdb_year = year

                    # Build a minimal cache entry with TMDB data
                    # Note: We don't include TMDB overview as summary - that's not
                    # VPRO content. We just provide basic metadata so Plex can add the movie.
                    return CacheEntry(
                        title=tmdb_title,
                        year=tmdb_year,
                        description=None,  # No description - let Plex use secondary agent
                        url=None,
                        imdb_id=imdb_id,
                        vpro_id=None,
                        media_type="film",
                        status=CacheStatus.FOUND.value,
                        lookup_method="tmdb_fallback",
                    )

        # No IMDB ID or not found - search by title+year
        discovered_imdb, alt_titles = tmdb.search_by_title(title, year, "film")
        if discovered_imdb:
            # Found via title search
            return CacheEntry(
                title=title,
                year=year,
                description=None,  # No description - let Plex use secondary agent
                url=None,
                imdb_id=discovered_imdb,
                vpro_id=None,
                media_type="film",
                status=CacheStatus.FOUND.value,
                lookup_method="tmdb_fallback",
                discovered_imdb=discovered_imdb,
            )

    except Exception as e:
        logger.warning(f"TMDB fallback error: {e}")

    return None


# =============================================================================
# Unified Match Handler
# =============================================================================

@dataclass
class MatchRequest:
    """Match request parameters."""
    title: str
    year: Optional[int]
    imdb_id: Optional[str]
    manual: bool = False  # Fix Match mode - return multiple results

    @property
    def identifier(self) -> str:
        return PROVIDER_IDENTIFIER

    @property
    def base_path(self) -> str:
        return "/movies"


def handle_match_request(req: MatchRequest) -> dict:
    """
    Match handler for movie endpoints.

    Returns IMMEDIATELY - actual lookup happens in get_metadata.

    Args:
        req: Match request parameters

    Returns:
        Plex-compatible match response
    """
    logger.info(f"Match request: title='{req.title}', year={req.year}, imdb={req.imdb_id}")
    metrics.inc("match_requests")

    if not req.title:
        return _build_empty_response(req.identifier)

    rating_key = generate_rating_key(req.title, req.year, req.imdb_id)
    plex_type = "movie"

    match_metadata = {
        "ratingKey": rating_key,
        "key": f"{req.base_path}/library/metadata/{rating_key}",
        "guid": f"{req.identifier}://{plex_type}/{rating_key}",
        "type": plex_type,
        "title": req.title,
    }

    if req.year:
        match_metadata["year"] = int(req.year)

    if req.imdb_id:
        match_metadata["Guid"] = [{"id": f"imdb://{req.imdb_id}"}]

    logger.info(f"Match returned: {req.title} ({req.year}) -> {rating_key}")

    return {
        "MediaContainer": {
            "offset": 0,
            "totalSize": 1,
            "identifier": req.identifier,
            "size": 1,
            "Metadata": [match_metadata]
        }
    }


def handle_manual_match_request(req: MatchRequest) -> dict:
    """
    Handle Fix Match manual search - returns MULTIPLE results.

    This does an IMMEDIATE search (unlike normal match which defers lookup).
    Called when Plex sends manual=1 in the match request, typically when
    user clicks "Fix Match" in the UI.

    Args:
        req: Match request parameters (with manual=True)

    Returns:
        Plex-compatible response with multiple metadata entries
    """
    logger.info(f"Manual match (Fix Match): title='{req.title}', year={req.year}")
    metrics.inc("manual_match_requests")

    if not req.title:
        return _build_empty_response(req.identifier)

    # Perform immediate search for Fix Match (movies only)
    films = search_poms_multiple(
        title=req.title,
        year=req.year,
        media_type="film",
        max_results=10,
    )

    if not films:
        logger.info(f"Manual match: No results for '{req.title}'")
        return _build_empty_response(req.identifier)

    metadata_list = []
    for film in films:
        rating_key = generate_rating_key(film.title, film.year, film.imdb_id)
        plex_type = "movie"

        metadata = {
            "ratingKey": rating_key,
            "key": f"{req.base_path}/library/metadata/{rating_key}",
            "guid": f"{req.identifier}://{plex_type}/{rating_key}",
            "type": plex_type,
            "title": film.title,
            "summary": film.description,  # Include description in Fix Match results
        }

        if film.year:
            metadata["year"] = film.year

        # Include thumbnail for Fix Match UI when images are enabled
        if VPRO_RETURN_IMAGES and film.images:
            # Find first poster/portrait image, or fall back to any image
            thumb_url = None
            for img in film.images:
                img_type = img.get("type", "")
                if img_type == "PROMO_PORTRAIT":
                    thumb_url = img.get("url")
                    break
            if not thumb_url and film.images:
                thumb_url = film.images[0].get("url")
            if thumb_url:
                metadata["thumb"] = thumb_url

        # Include external GUIDs if available
        guids = []
        if film.imdb_id:
            guids.append({"id": f"imdb://{film.imdb_id}"})
        if film.vpro_id:
            guids.append({"id": f"vpro://{film.vpro_id}"})
        if guids:
            metadata["Guid"] = guids

        metadata_list.append(metadata)

        # Pre-cache the result for faster metadata fetch when user selects it
        entry = CacheEntry.from_vpro_film(film, lookup_method="manual_match", sanitize_desc=False)
        cache.write(rating_key, entry)

    logger.info(f"Manual match: Returning {len(metadata_list)} results for '{req.title}'")

    return {
        "MediaContainer": {
            "offset": 0,
            "totalSize": len(metadata_list),
            "identifier": req.identifier,
            "size": len(metadata_list),
            "Metadata": metadata_list
        }
    }


def parse_match_data(data: dict) -> MatchRequest:
    """
    Parse match request data from Plex.

    Args:
        data: JSON request data

    Returns:
        MatchRequest with parsed parameters
    """
    title = data.get('title', '')
    year = data.get('year')
    guid = data.get('guid', '')
    filename = data.get('filename', '')
    manual = data.get('manual', 0) == 1  # Fix Match mode

    # Try Media array for filename
    media = data.get('Media', [])
    if not filename and media:
        try:
            filename = media[0].get('Part', [{}])[0].get('file', '')
        except (IndexError, KeyError, TypeError):
            pass

    # Extract IMDB from guid first, then filename
    logger.debug(f"parse_match_data: guid='{guid}', filename='{filename}', manual={manual}")
    imdb_id = extract_imdb_from_text(guid)
    if imdb_id:
        logger.debug(f"Extracted IMDB {imdb_id} from guid")
    else:
        imdb_id = extract_imdb_from_text(filename)
        if imdb_id:
            logger.debug(f"Extracted IMDB {imdb_id} from filename")
        else:
            logger.debug("No IMDB ID found in guid or filename")

    # Extract year from filename if not provided
    if not year and filename:
        year = extract_year_from_text(filename)

    return MatchRequest(
        title=title,
        year=year,
        imdb_id=imdb_id,
        manual=manual,
    )


# =============================================================================
# Test Endpoint
# =============================================================================

@app.route('/test', methods=['GET'])
def test_search():
    """
    Test endpoint for manual testing.

    Usage:
        /test?title=Apocalypse+Now
        /test?title=Apocalypse+Now&year=1979
        /test?title=The+Last+Metro&year=1980&imdb=tt0080610
        /test?title=Downfall&year=2004&skip_poms=1  # Test cinema.nl fallback
        /test?title=Test&skip_poms=1&skip_tmdb=1    # Test cinema.nl only
    """
    title = request.args.get('title', '')
    year = request.args.get('year', type=int)
    imdb_id = request.args.get('imdb', '')
    skip_poms = request.args.get('skip_poms', '').lower() in ('1', 'true', 'yes')
    skip_tmdb = request.args.get('skip_tmdb', '').lower() in ('1', 'true', 'yes')

    if not title:
        return jsonify({
            "error": "Missing 'title' parameter",
            "usage": "/test?title=Name&year=1979&imdb=tt1234567&skip_poms=1&skip_tmdb=1",
            "examples": [
                "/test?title=Apocalypse+Now&year=1979",
                "/test?title=The+Last+Metro&year=1980&imdb=tt0080610",
                "/test?title=Der+Untergang&year=2004&skip_poms=1  (test cinema.nl fallback)",
            ]
        }), 400

    skip_info = []
    if skip_poms:
        skip_info.append("POMS")
    if skip_tmdb:
        skip_info.append("TMDB")
    skip_str = f" [SKIP: {', '.join(skip_info)}]" if skip_info else ""

    logger.info(f"Test search: title='{title}', year={year}, imdb={imdb_id}{skip_str}")

    try:
        film = get_vpro_description(
            title=title,
            year=year,
            imdb_id=imdb_id or None,
            skip_poms=skip_poms,
            skip_tmdb=skip_tmdb,
        )
    except Exception as e:
        logger.error(f"Test search error: {e}")
        return jsonify({"error": str(e), "title": title, "year": year}), 500

    if not film:
        return jsonify({
            "found": False,
            "title": title,
            "year": year,
            "message": "Not found in VPRO Cinema"
        }), 404

    return jsonify({
        "found": True,
        "query": {
            "title": title,
            "year": year,
            "imdb": imdb_id,
            "skip_poms": skip_poms,
            "skip_tmdb": skip_tmdb,
        },
        "result": {
            "title": film.title,
            "year": film.year,
            "lookup_method": getattr(film, 'lookup_method', None),
            "director": getattr(film, 'director', None),
            "imdb_id": film.imdb_id,
            "vpro_id": film.vpro_id,
            "vpro_url": film.url,
            "genres": getattr(film, 'genres', []),
            "vpro_rating": getattr(film, 'vpro_rating', None),
            "content_rating": getattr(film, 'content_rating', None),
            "images": getattr(film, 'images', []),
            "description_length": len(film.description) if film.description else 0,
            "description": film.description
        }
    })


# =============================================================================
# Cache Endpoints
# =============================================================================

@app.route('/cache', methods=['GET'])
def cache_status():
    """
    View cache statistics and entries.

    Usage:
        /cache           - list all cached rating keys
        /cache?key=xxx   - view specific cached item
    """
    key = request.args.get('key', '')

    if key:
        cached = cache.read(key)
        if cached:
            return jsonify({"key": key, "cached": True, "metadata": cached.to_dict()})
        else:
            return jsonify({"key": key, "cached": False}), 404

    return jsonify({
        "stats": cache.stats(),
        "keys": cache.keys()[:100],  # Limit to first 100
    })


@app.route('/cache/clear', methods=['POST'])
def cache_clear():
    """Clear all cache entries (preserves credentials.json)."""
    try:
        count = cache.clear(preserve_credentials=True)
        return jsonify({"cleared": count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/cache/delete', methods=['POST'])
def cache_delete():
    """
    Delete specific cache entries by key or pattern.

    Usage:
        POST /cache/delete?key=vpro-confess-fletch-2022-none-m
        POST /cache/delete?pattern=fletch
    """
    key = request.args.get('key', '')
    pattern = request.args.get('pattern', '')

    if not key and not pattern:
        return jsonify({
            "error": "Missing 'key' or 'pattern' parameter",
            "usage": {
                "by_key": "POST /cache/delete?key=vpro-title-year-imdb-m",
                "by_pattern": "POST /cache/delete?pattern=fletch"
            }
        }), 400

    try:
        deleted = []

        if key:
            # Delete specific key
            if cache.delete(key):
                deleted.append(key)
        elif pattern:
            # Delete all keys matching pattern (case-insensitive)
            pattern_lower = pattern.lower()
            for cache_key in cache.keys():
                if pattern_lower in cache_key.lower():
                    if cache.delete(cache_key):
                        deleted.append(cache_key)

        return jsonify({
            "deleted": deleted,
            "count": len(deleted)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =============================================================================
# Provider Root Endpoints
# =============================================================================

@app.route('/movies', methods=['GET'])
def provider_root():
    """
    Return provider information for MOVIES.

    The MediaProvider response declares the agent's capabilities to Plex.
    The Source array is critical for enabling Local Media Assets (LMA) detection,
    which handles external subtitle files (.srt, .ass, .sub), local artwork, and
    embedded metadata. Without this declaration, Plex won't scan for sidecar files.

    See: https://forums.plex.tv/t/announcement-custom-metadata-providers/934384
    """
    return jsonify({
        "MediaProvider": {
            "identifier": PROVIDER_IDENTIFIER,
            "title": PROVIDER_TITLE,
            "version": PROVIDER_VERSION,
            "Types": [
                {"type": 1, "Scheme": [{"scheme": PROVIDER_IDENTIFIER}]}
            ],
            "Feature": [
                {"type": "metadata", "key": "/library/metadata"},
                {"type": "match", "key": "/library/metadata/matches"}
            ],
            # Source array declares additional providers to run alongside this agent.
            # This enables Local Media Assets to scan for external subtitle files,
            # local artwork (poster.jpg, fanart.jpg), and embedded metadata.
            # Without this, Plex won't detect sidecar files like .nl.srt or .en.srt.
            "Source": [
                {
                    "identifier": "com.plexapp.agents.localmedia",
                    "enabled": True,
                    "name": "Local Media Assets (Movies)"
                }
            ]
        }
    })




# =============================================================================
# Movie Endpoints
# =============================================================================

@app.route('/movies/library/metadata/<rating_key>', methods=['GET'])
def get_metadata(rating_key: str):
    """Get metadata for a movie by its rating key."""
    req = MetadataRequest(rating_key=rating_key)
    return jsonify(handle_metadata_request(req))


@app.route('/movies/library/metadata/matches', methods=['POST'])
def match_metadata():
    """Match movie content based on hints from Plex."""
    data = request.get_json() or {}

    # Log raw request data for debugging
    logger.debug(f"Match request raw data: {data}")

    match_req = parse_match_data(data)

    # Fix Match mode - return multiple results for user selection
    if match_req.manual:
        return jsonify(handle_manual_match_request(match_req))

    # Extract filename for logging
    filename = data.get('filename', '')
    if not filename:
        media = data.get('Media', [])
        if media:
            try:
                filename = media[0].get('Part', [{}])[0].get('file', '')
            except (IndexError, KeyError, TypeError):
                pass

    # Log match request for troubleshooting
    rating_key = generate_rating_key(match_req.title, match_req.year, match_req.imdb_id)
    log_match_request(
        raw_data=data,
        title=match_req.title,
        year=match_req.year,
        imdb_id=match_req.imdb_id,
        media_type="film",
        rating_key=rating_key,
        filename=filename,
        guid=data.get('guid', ''),
    )

    return jsonify(handle_match_request(match_req))


@app.route('/movies/library/metadata/<rating_key>/images', methods=['GET'])
def get_images(rating_key: str):
    """Return VPRO images if enabled, otherwise empty."""
    return jsonify(_build_images_response(rating_key, PROVIDER_IDENTIFIER))


@app.route('/movies/library/metadata/<rating_key>/extras', methods=['GET'])
def get_extras(rating_key: str):
    """Return empty - no extras."""
    return jsonify(_build_empty_response(PROVIDER_IDENTIFIER))




# =============================================================================
# Health Check Endpoints
# =============================================================================

@app.route('/health', methods=['GET'])
def health_check():
    """Shallow health check - confirms app is running."""
    return jsonify({
        "status": "healthy",
        "version": PROVIDER_VERSION,
        "identifier": PROVIDER_IDENTIFIER,
    })


@app.route('/health/ready', methods=['GET'])
def readiness_check():
    """
    Deep health check for Kubernetes readiness probe.

    Checks:
    - Cache directory writable
    - Credentials available
    - External API configuration
    """
    checks = {}
    healthy = True

    # Check 1: Cache directory writable
    try:
        test_file = Path(CACHE_DIR) / ".health_check"
        test_file.write_text("ok")
        test_file.unlink()
        checks["cache_writable"] = {"status": "ok"}
    except Exception as e:
        checks["cache_writable"] = {"status": "error", "message": str(e)}
        healthy = False

    # Check 2: Credentials available
    try:
        creds = get_credential_manager()
        key, secret = creds.get_credentials()
        if key and secret:
            checks["credentials"] = {"status": "ok", "key_prefix": key[:4]}
        else:
            checks["credentials"] = {"status": "degraded", "message": "using defaults"}
    except Exception as e:
        checks["credentials"] = {"status": "error", "message": str(e)}
        healthy = False

    # Check 3: TMDB configured
    tmdb_key = os.environ.get("TMDB_API_KEY")
    checks["tmdb"] = {
        "status": "ok" if tmdb_key else "disabled",
        "configured": bool(tmdb_key)
    }

    # Check 4: VPRO optional features
    checks["vpro_features"] = {
        "summary": VPRO_RETURN_SUMMARY,
        "content_rating": VPRO_RETURN_CONTENT_RATING,
        "images": VPRO_RETURN_IMAGES,
        "rating": VPRO_RETURN_RATING,  # Note: Plex may not display (see README)
    }

    status_code = 200 if healthy else 503

    return jsonify({
        "status": "healthy" if healthy else "unhealthy",
        "version": PROVIDER_VERSION,
        "checks": checks,
        "cache_stats": cache.stats(),
        "metrics": metrics.get_stats(),
    }), status_code


@app.route('/health/live', methods=['GET'])
def liveness_check():
    """Liveness probe - checks app isn't deadlocked."""
    return jsonify({"status": "alive"}), 200


@app.route('/metrics', methods=['GET'])
def metrics_endpoint():
    """Return application metrics."""
    return jsonify(metrics.get_stats())


# =============================================================================
# Main
# =============================================================================

if __name__ == "__main__":
    logger.info(f"Starting VPRO Cinema Provider v{PROVIDER_VERSION} on port {PORT}")
    logger.info(f"Provider identifier: {PROVIDER_IDENTIFIER} (movies only)")
    logger.info(f"TMDB: {'enabled' if os.environ.get('TMDB_API_KEY') else 'disabled'}")
    logger.info(f"Cache directory: {CACHE_DIR}")
    logger.info(f"Test endpoint: http://localhost:{PORT}/test?title=TITLE&year=YEAR")
    app.run(host="0.0.0.0", port=PORT, debug=False)
