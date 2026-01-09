#!/usr/bin/env python3
"""
VPRO Cinema Plex Metadata Provider v3.2.0

A custom Plex metadata provider that fetches Dutch film and TV series
descriptions from VPRO Cinema.

Architecture:
    /library/metadata/matches (POST)
        - Returns IMMEDIATELY with basic match (title, year, ratingKey)
        - NO search - encodes title/year/imdb_id/media_type in ratingKey for later lookup
        - Supports both movies (type 1) and TV shows (type 2)
        - This prevents Plex UI timeouts

    /library/metadata/{ratingKey} (GET)
        - Does the actual VPRO lookup (Plex waits up to 90 seconds here)
        - Checks file cache first
        - Uses NPO POMS API (primary) with web search fallback
        - Returns summary if found, omits summary for fallback to secondary provider

Environment Variables:
    PORT: Server port (default: 5100)
    LOG_LEVEL: Logging level (default: INFO)
    CACHE_DIR: Cache directory (default: ./cache)
    TMDB_API_KEY: TMDB API key for alternate titles lookup (optional but recommended)
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
    PROVIDER_IDENTIFIER_TV,
    PROVIDER_TITLE,
    PROVIDER_TITLE_TV,
    PROVIDER_VERSION,
    VPRO_RETURN_IMAGES,
    VPRO_RETURN_RATING,
)
from cache import FileCache, CacheEntry
from credentials import get_credential_manager
from text_utils import (
    normalize_for_cache_key,
    validate_rating_key,
    sanitize_description,
    extract_imdb_from_text,
    extract_year_from_text,
)
from logging_config import configure_logging, setup_flask_request_id, get_request_id
from metrics import metrics
from vpro_lookup import get_vpro_description
from poms_client import search_poms_multiple

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
    media_type: str = "film"
) -> str:
    """
    Generate a rating key encoding title, year, IMDB ID, and media type.

    Format: vpro-{sanitized_title}-{year}-{imdb_id}-{type}
    Where type is 'm' for film/movie or 's' for series.

    Backwards compatible: old keys without type suffix are treated as films.

    Args:
        title: Content title
        year: Release year (optional)
        imdb_id: IMDB ID (optional)
        media_type: "film" or "series"

    Returns:
        Encoded rating key string
    """
    sanitized_title = normalize_for_cache_key(title) or "unknown"
    year_str = str(year) if year else "0"
    imdb_str = imdb_id.lower() if imdb_id else "none"
    type_char = MediaType(media_type).to_type_char()
    return f"vpro-{sanitized_title}-{year_str}-{imdb_str}-{type_char}"


def parse_rating_key(rating_key: str) -> dict:
    """
    Parse a rating key back into components.

    Backwards compatible: keys without type suffix are treated as films.

    Args:
        rating_key: Rating key to parse

    Returns:
        Dict with title, year, imdb_id, media_type
    """
    result = {"title": None, "year": None, "imdb_id": None, "media_type": "film"}

    if not rating_key or not rating_key.startswith("vpro-"):
        return result

    key_part = rating_key[5:]  # Remove "vpro-"

    # Extract media type from end (new format: -m or -s)
    if key_part.endswith("-m"):
        result["media_type"] = "film"
        key_part = key_part[:-2]
    elif key_part.endswith("-s"):
        result["media_type"] = "series"
        key_part = key_part[:-2]

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
    """Unified metadata request parameters."""
    rating_key: str
    provider_type: str  # "movie" or "tv"

    @property
    def identifier(self) -> str:
        """Get provider identifier based on type."""
        return PROVIDER_IDENTIFIER_TV if self.provider_type == "tv" else PROVIDER_IDENTIFIER

    @property
    def base_path(self) -> str:
        """Get base URL path based on type."""
        return "/series" if self.provider_type == "tv" else "/movies"


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
    }

    # Only include summary if we have a description
    if entry.description:
        metadata["summary"] = entry.description

    # Include Kijkwijzer content rating if available
    if entry.content_rating:
        metadata["contentRating"] = f"nl/{entry.content_rating}"

    # Include VPRO rating if enabled and available
    if VPRO_RETURN_RATING and entry.vpro_rating:
        # Plex rating is 0-10 scale, VPRO is 1-10
        metadata["rating"] = float(entry.vpro_rating)

    # Build external GUIDs
    guids = []
    if entry.imdb_id:
        guids.append({"id": f"imdb://{entry.imdb_id}"})
    if entry.vpro_id:
        guids.append({"id": f"vpro://{entry.vpro_id}"})
    if guids:
        metadata["Guid"] = guids

    return {
        "MediaContainer": {
            "offset": 0,
            "totalSize": 1,
            "identifier": req.identifier,
            "size": 1,
            "Metadata": [metadata]
        }
    }


def _build_empty_response(identifier: str) -> dict:
    """Build empty response for errors or not-found cases."""
    return {
        "MediaContainer": {
            "offset": 0,
            "totalSize": 0,
            "identifier": identifier,
            "size": 0,
            "Metadata": []
        }
    }


def _build_images_response(rating_key: str, identifier: str) -> dict:
    """
    Build images response from cached data.

    Returns VPRO images if VPRO_RETURN_IMAGES is enabled and images exist in cache,
    otherwise returns empty.
    """
    if not VPRO_RETURN_IMAGES:
        return {
            "MediaContainer": {
                "offset": 0,
                "totalSize": 0,
                "identifier": identifier,
                "size": 0,
                "Image": []
            }
        }

    # Try to get images from cache
    cached = cache.read(rating_key)
    if not cached or not cached.images:
        return {
            "MediaContainer": {
                "offset": 0,
                "totalSize": 0,
                "identifier": identifier,
                "size": 0,
                "Image": []
            }
        }

    # Build Plex image list
    # Plex expects: type (poster/art/banner), url, thumb (optional), ratingKey
    plex_images = []
    for i, img in enumerate(cached.images):
        img_type = img.get("type", "PICTURE")
        # Map VPRO image types to Plex types
        if img_type == "PROMO_PORTRAIT":
            plex_type = "poster"
        elif img_type in ("PICTURE", "PROMO_LANDSCAPE"):
            plex_type = "art"  # backdrop/fanart
        else:
            plex_type = "art"

        plex_images.append({
            "type": plex_type,
            "url": img.get("url"),
            "ratingKey": f"{rating_key}-img-{i}",
        })

    return {
        "MediaContainer": {
            "offset": 0,
            "totalSize": len(plex_images),
            "identifier": identifier,
            "size": len(plex_images),
            "Image": plex_images
        }
    }


def handle_metadata_request(req: MetadataRequest) -> dict:
    """
    Unified metadata handler for both movie and TV endpoints.

    This eliminates code duplication between get_metadata() and get_metadata_tv().

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
        # Handle cached not-found entries (7-day TTL)
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
    media_type = parsed.get("media_type", "film")

    if not title:
        logger.warning(f"Could not parse title from rating key: {req.rating_key}")
        return _build_empty_response(req.identifier)

    logger.info(f"Cache miss - searching: title='{title}', year={year}, imdb={imdb_id}, type={media_type}")

    # Perform the VPRO lookup
    try:
        with metrics.timer("vpro_lookup_duration_ms"):
            film = get_vpro_description(
                title=title,
                year=year,
                imdb_id=imdb_id,
                media_type=media_type,
            )
    except Exception as e:
        logger.error(f"VPRO search error: {e}")
        film = None

    # Build and cache result
    if film and film.description:
        # Use discovered_imdb if no original imdb_id was provided
        effective_imdb = film.imdb_id or film.discovered_imdb
        entry = CacheEntry(
            title=film.title,
            year=film.year,
            description=sanitize_description(film.description),
            url=film.url,
            imdb_id=effective_imdb,
            vpro_id=film.vpro_id,
            media_type=film.media_type,
            status=CacheStatus.FOUND.value,
            fetched_at="",
            last_accessed="",
            lookup_method=film.lookup_method,
            # Only store discovered_imdb if it differs from the effective imdb_id
            discovered_imdb=film.discovered_imdb if film.discovered_imdb and film.discovered_imdb != film.imdb_id else None,
            content_rating=film.content_rating,
            vpro_rating=film.vpro_rating,
            images=film.images if film.images else None,
        )
        cache.write(req.rating_key, entry)
        lookup_info = f" via {film.lookup_method}" if film.lookup_method else ""
        logger.info(f"Found: {film.title} ({film.year}) [{film.media_type}] - {len(film.description)} chars{lookup_info}")
        metrics.inc("vpro_found")
        return _build_metadata_response(req, entry)
    else:
        # Cache not-found results with shorter TTL (7 days vs 30 for found)
        # This prevents hammering APIs on library refresh while still allowing
        # newly added VPRO content to be found within a week
        not_found_entry = CacheEntry(
            title=title,
            year=year,
            description=None,
            url=None,
            imdb_id=imdb_id,
            vpro_id=None,
            media_type=media_type,
            status=CacheStatus.NOT_FOUND.value,
            fetched_at="",
            last_accessed="",
        )
        cache.write(req.rating_key, not_found_entry)
        logger.info(f"Not found: {title} ({year}) - cached for 7 days, omitting summary for fallback")
        metrics.inc("vpro_not_found")
        return _build_empty_response(req.identifier)


# =============================================================================
# Unified Match Handler
# =============================================================================

@dataclass
class MatchRequest:
    """Match request parameters."""
    title: str
    year: Optional[int]
    imdb_id: Optional[str]
    media_type: str
    provider_type: str  # "movie" or "tv"
    manual: bool = False  # Fix Match mode - return multiple results

    @property
    def identifier(self) -> str:
        return PROVIDER_IDENTIFIER_TV if self.provider_type == "tv" else PROVIDER_IDENTIFIER

    @property
    def base_path(self) -> str:
        return "/series" if self.provider_type == "tv" else "/movies"


def handle_match_request(req: MatchRequest) -> dict:
    """
    Unified match handler for both movie and TV endpoints.

    Returns IMMEDIATELY - actual lookup happens in get_metadata.

    Args:
        req: Match request parameters

    Returns:
        Plex-compatible match response
    """
    logger.info(f"Match request: title='{req.title}', year={req.year}, imdb={req.imdb_id}, type={req.media_type}")
    metrics.inc("match_requests", labels={"type": req.media_type})

    if not req.title:
        return _build_empty_response(req.identifier)

    rating_key = generate_rating_key(req.title, req.year, req.imdb_id, req.media_type)
    plex_type = MediaType(req.media_type).to_plex_type_str()

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

    logger.info(f"Match returned: {req.title} ({req.year}) [{req.media_type}] -> {rating_key}")

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
    logger.info(
        f"Manual match (Fix Match): title='{req.title}', year={req.year}, "
        f"type={req.media_type}"
    )
    metrics.inc("manual_match_requests", labels={"type": req.media_type})

    if not req.title:
        return _build_empty_response(req.identifier)

    # Perform immediate search for Fix Match
    films = search_poms_multiple(
        title=req.title,
        year=req.year,
        media_type=req.media_type,
        max_results=10,
    )

    if not films:
        logger.info(f"Manual match: No results for '{req.title}'")
        return _build_empty_response(req.identifier)

    metadata_list = []
    for film in films:
        rating_key = generate_rating_key(
            film.title, film.year, film.imdb_id, film.media_type
        )
        plex_type = MediaType(film.media_type).to_plex_type_str()

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
        logger.debug(f"Film '{film.title}': VPRO_RETURN_IMAGES={VPRO_RETURN_IMAGES}, images={len(film.images) if film.images else 0}")
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
        entry = CacheEntry(
            title=film.title,
            year=film.year,
            description=film.description,
            url=film.url,
            imdb_id=film.imdb_id,
            vpro_id=film.vpro_id,
            media_type=film.media_type,
            status=CacheStatus.FOUND.value,
            fetched_at="",
            last_accessed="",
            lookup_method="manual_match",
            content_rating=film.content_rating,
            vpro_rating=film.vpro_rating,
            images=film.images if film.images else None,
        )
        cache.write(rating_key, entry)

    logger.info(
        f"Manual match: Returning {len(metadata_list)} results for '{req.title}'"
    )

    return {
        "MediaContainer": {
            "offset": 0,
            "totalSize": len(metadata_list),
            "identifier": req.identifier,
            "size": len(metadata_list),
            "Metadata": metadata_list
        }
    }


def parse_match_data(data: dict, provider_type: str) -> MatchRequest:
    """
    Parse match request data from Plex.

    Args:
        data: JSON request data
        provider_type: "movie" or "tv"

    Returns:
        MatchRequest with parsed parameters
    """
    title = data.get('title', '')
    year = data.get('year')
    metadata_type = data.get('type', 1 if provider_type == "movie" else 2)
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

    # Determine media type from Plex type
    media_type = MediaType.from_plex_type(metadata_type)

    return MatchRequest(
        title=title,
        year=year,
        imdb_id=imdb_id,
        media_type=media_type.value,
        provider_type=provider_type,
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
        /test?title=Adolescence&year=2025&type=series
    """
    title = request.args.get('title', '')
    year = request.args.get('year', type=int)
    imdb_id = request.args.get('imdb', '')
    media_type = request.args.get('type', 'all')

    if media_type not in ('film', 'series', 'all'):
        media_type = 'all'

    if not title:
        return jsonify({
            "error": "Missing 'title' parameter",
            "usage": "/test?title=Name&year=1979&imdb=tt1234567&type=film|series|all",
            "examples": [
                "/test?title=Apocalypse+Now&year=1979",
                "/test?title=The+Last+Metro&year=1980&imdb=tt0080610",
                "/test?title=Adolescence&year=2025&type=series"
            ]
        }), 400

    logger.info(f"Test search: title='{title}', year={year}, imdb={imdb_id}, type={media_type}")

    try:
        film = get_vpro_description(
            title=title,
            year=year,
            imdb_id=imdb_id or None,
            media_type=media_type,
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
        "query": {"title": title, "year": year, "imdb": imdb_id, "type": media_type},
        "result": {
            "title": film.title,
            "year": film.year,
            "media_type": film.media_type,
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
    """Return provider information for MOVIES."""
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
            ]
        }
    })


@app.route('/series', methods=['GET'])
def provider_root_tv():
    """Return provider information for TV SHOWS."""
    return jsonify({
        "MediaProvider": {
            "identifier": PROVIDER_IDENTIFIER_TV,
            "title": PROVIDER_TITLE_TV,
            "version": PROVIDER_VERSION,
            "Types": [
                {"type": 2, "Scheme": [{"scheme": PROVIDER_IDENTIFIER_TV}]},
                {"type": 3, "Scheme": [{"scheme": PROVIDER_IDENTIFIER_TV}]},
                {"type": 4, "Scheme": [{"scheme": PROVIDER_IDENTIFIER_TV}]}
            ],
            "Feature": [
                {"type": "metadata", "key": "/library/metadata"},
                {"type": "match", "key": "/library/metadata/matches"}
            ]
        }
    })


# =============================================================================
# Movie Endpoints
# =============================================================================

@app.route('/movies/library/metadata/<rating_key>', methods=['GET'])
def get_metadata(rating_key: str):
    """Get metadata for a movie by its rating key."""
    req = MetadataRequest(rating_key=rating_key, provider_type="movie")
    return jsonify(handle_metadata_request(req))


@app.route('/movies/library/metadata/matches', methods=['POST'])
def match_metadata():
    """Match movie content based on hints from Plex."""
    data = request.get_json() or {}
    metadata_type = data.get('type', 1)

    # Log raw request data for debugging
    logger.debug(f"Match request raw data: {data}")

    # Seasons and Episodes - delegate to secondary provider
    if metadata_type in (3, 4):
        logger.info(f"Season/Episode match request (type {metadata_type}) - delegating")
        return jsonify(_build_empty_response(PROVIDER_IDENTIFIER))

    match_req = parse_match_data(data, "movie")

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
    rating_key = generate_rating_key(match_req.title, match_req.year, match_req.imdb_id, match_req.media_type)
    log_match_request(
        raw_data=data,
        title=match_req.title,
        year=match_req.year,
        imdb_id=match_req.imdb_id,
        media_type=match_req.media_type,
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
# TV Endpoints
# =============================================================================

@app.route('/series/library/metadata/<rating_key>', methods=['GET'])
def get_metadata_tv(rating_key: str):
    """Get metadata for a TV show by its rating key."""
    req = MetadataRequest(rating_key=rating_key, provider_type="tv")
    return jsonify(handle_metadata_request(req))


@app.route('/series/library/metadata/matches', methods=['POST'])
def match_metadata_tv():
    """Match TV content based on hints from Plex."""
    data = request.get_json() or {}
    metadata_type = data.get('type', 2)

    # Seasons and Episodes - delegate to secondary provider
    if metadata_type in (3, 4):
        logger.info(f"Season/Episode match request (type {metadata_type}) - delegating")
        return jsonify(_build_empty_response(PROVIDER_IDENTIFIER_TV))

    match_req = parse_match_data(data, "tv")

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
    rating_key = generate_rating_key(match_req.title, match_req.year, match_req.imdb_id, match_req.media_type)
    log_match_request(
        raw_data=data,
        title=match_req.title,
        year=match_req.year,
        imdb_id=match_req.imdb_id,
        media_type=match_req.media_type,
        rating_key=rating_key,
        filename=filename,
        guid=data.get('guid', ''),
    )

    return jsonify(handle_match_request(match_req))


@app.route('/series/library/metadata/<rating_key>/images', methods=['GET'])
def get_images_tv(rating_key: str):
    """Return VPRO images if enabled, otherwise empty."""
    return jsonify(_build_images_response(rating_key, PROVIDER_IDENTIFIER_TV))


@app.route('/series/library/metadata/<rating_key>/extras', methods=['GET'])
def get_extras_tv(rating_key: str):
    """Return empty - no extras."""
    return jsonify(_build_empty_response(PROVIDER_IDENTIFIER_TV))


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
    logger.info(f"Provider identifiers: {PROVIDER_IDENTIFIER}, {PROVIDER_IDENTIFIER_TV}")
    logger.info(f"TMDB alternate titles: {'enabled' if os.environ.get('TMDB_API_KEY') else 'disabled'}")
    logger.info(f"Cache directory: {CACHE_DIR}")
    logger.info(f"Test endpoint: http://localhost:{PORT}/test?title=TITLE&year=YEAR")
    app.run(host="0.0.0.0", port=PORT, debug=False)
