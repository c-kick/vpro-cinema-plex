#!/usr/bin/env python3
"""
VPRO Cinema Plex Metadata Provider v3.0.0

A custom Plex metadata provider that fetches Dutch film and TV series
descriptions from VPRO Cinema.

Changes in 3.0.0:
    - Added TV series support (VPRO Cinema /cinema/series/)
    - Updated cache key format to include media type (backwards-compatible)
    - POMS API now searches both MOVIE and SERIES types
    - TMDB lookup now supports TV series (tv_results)
    - Web search fallback searches both /cinema/films/ and /cinema/series/

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
import time
import logging
from typing import Optional, Tuple
from flask import Flask, request, jsonify

from vpro_cinema_scraper import get_vpro_description, VPROFilm

# =============================================================================
# Configuration
# =============================================================================

# CRITICAL: Use the same identifier as v2.0.0 to maintain Plex registration
PROVIDER_IDENTIFIER = "tv.plex.agents.custom.vpro.cinema"
PROVIDER_IDENTIFIER_TV = "tv.plex.agents.custom.vpro.cinema.tv"
PROVIDER_TITLE = "VPRO Cinema (Dutch Summaries)"
PROVIDER_TITLE_TV = "VPRO Cinema TV (Dutch Summaries)"
PROVIDER_VERSION = "3.0.0"

PORT = int(os.environ.get("PORT", 5100))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
CACHE_DIR = os.environ.get("CACHE_DIR", "./cache")
NOT_FOUND_CACHE_TTL = 7 * 24 * 60 * 60  # 7 days

# Ensure cache directory exists
os.makedirs(CACHE_DIR, exist_ok=True)

# Logging setup
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Flask app
app = Flask(__name__)


# =============================================================================
# Helper Functions
# =============================================================================

def _sanitize_for_key(text: str) -> str:
    """Sanitize text for use in rating key (lowercase, alphanumeric, hyphens)."""
    text = text.lower().strip()
    text = re.sub(r'[^a-z0-9\s-]', '', text)
    text = re.sub(r'\s+', '-', text)
    text = re.sub(r'-+', '-', text).strip('-')
    return text[:50]


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
    """
    sanitized_title = _sanitize_for_key(title) or "unknown"
    year_str = str(year) if year else "0"
    imdb_str = imdb_id.lower() if imdb_id else "none"
    type_char = "s" if media_type == "series" else "m"
    return f"vpro-{sanitized_title}-{year_str}-{imdb_str}-{type_char}"


def parse_rating_key(rating_key: str) -> dict:
    """Parse a rating key back into components.

    Backwards compatible: keys without type suffix are treated as films.
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
    # else: old format without type suffix, default to "film"

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
# Filename Extraction
# =============================================================================

def extract_imdb_from_filename(filename: str) -> Optional[str]:
    """Extract IMDB ID from filename (Radarr naming: imdb-ttXXXXXXX)."""
    if not filename:
        return None
    
    patterns = [
        r'imdb-(tt\d{7,})',
        r'\{imdb-(tt\d{7,})\}',
        r'\[(tt\d{7,})\]',
        r'(?<![a-z])(tt\d{7,})(?![a-z])',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, filename, re.IGNORECASE)
        if match:
            return match.group(1).lower()
    
    return None


def extract_year_from_filename(filename: str) -> Optional[int]:
    """Extract year from filename."""
    if not filename:
        return None
    
    match = re.search(r'\((\d{4})\)', filename)
    if match:
        year = int(match.group(1))
        if 1900 <= year <= 2100:
            return year
    return None


# =============================================================================
# File-based Cache
# =============================================================================

def _get_cache_path(rating_key: str) -> str:
    safe_key = re.sub(r'[^\w\-]', '_', rating_key)
    return os.path.join(CACHE_DIR, f"{safe_key}.json")


def _cache_read(rating_key: str) -> Optional[dict]:
    """Read cached metadata. Applies TTL for not-found entries."""
    cache_path = _get_cache_path(rating_key)
    if not os.path.exists(cache_path):
        return None
    
    try:
        with open(cache_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # Check TTL for not-found entries
        if not data.get("description"):
            fetched_at = data.get("fetched_at")
            if fetched_at:
                try:
                    fetched_time = time.strptime(fetched_at, "%Y-%m-%dT%H:%M:%SZ")
                    age = time.time() - time.mktime(fetched_time)
                    if age > NOT_FOUND_CACHE_TTL:
                        logger.info(f"Not-found cache expired for {rating_key}")
                        os.remove(cache_path)
                        return None
                except (ValueError, OSError):
                    pass
        
        return data
    except Exception as e:
        logger.warning(f"Cache read error: {e}")
        return None


def _cache_write(rating_key: str, data: dict):
    """Write metadata to cache."""
    cache_path = _get_cache_path(rating_key)
    try:
        data["fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Cache write error: {e}")


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

    # Validate media_type
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
            verbose=False
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
            "description_length": len(film.description) if film.description else 0,
            "description": film.description
        }
    })


# =============================================================================
# Cache Debug Endpoint
# =============================================================================

@app.route('/cache', methods=['GET'])
def cache_status():
    """
    Debug endpoint to view cached metadata.
    
    Usage:
        /cache           - list all cached rating keys
        /cache?key=xxx   - view specific cached item
    """
    key = request.args.get('key', '')
    
    if key:
        cached = _cache_read(key)
        if cached:
            return jsonify({"key": key, "cached": True, "metadata": cached})
        else:
            return jsonify({"key": key, "cached": False}), 404
    
    # List all cached files
    try:
        cache_files = [f for f in os.listdir(CACHE_DIR) if f.endswith('.json')]
        keys = [f[:-5] for f in cache_files]
    except Exception:
        keys = []
    
    return jsonify({
        "cache_dir": CACHE_DIR,
        "cache_size": len(keys),
        "keys": keys
    })


@app.route('/cache/clear', methods=['POST'])
def cache_clear():
    """Clear all cache entries (preserves credentials.json)."""
    try:
        cache_files = [f for f in os.listdir(CACHE_DIR)
                       if f.endswith('.json') and f != 'credentials.json']
        for f in cache_files:
            os.remove(os.path.join(CACHE_DIR, f))
        return jsonify({"cleared": len(cache_files)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =============================================================================
# Provider Root - Movies
# =============================================================================

@app.route('/', methods=['GET'])
def provider_root():
    """Return provider information for MOVIES only."""
    return jsonify({
        "MediaProvider": {
            "identifier": PROVIDER_IDENTIFIER,
            "title": PROVIDER_TITLE,
            "version": PROVIDER_VERSION,
            "Types": [
                {"type": 1, "Scheme": [{"scheme": PROVIDER_IDENTIFIER}]}  # Movies only
            ],
            "Feature": [
                {"type": "metadata", "key": "/library/metadata"},
                {"type": "match", "key": "/library/metadata/matches"}
            ]
        }
    })


# =============================================================================
# Provider Root - TV Shows
# =============================================================================

@app.route('/tv', methods=['GET'])
def provider_root_tv():
    """Return provider information for TV SHOWS only."""
    return jsonify({
        "MediaProvider": {
            "identifier": PROVIDER_IDENTIFIER_TV,
            "title": PROVIDER_TITLE_TV,
            "version": PROVIDER_VERSION,
            "Types": [
                {"type": 2, "Scheme": [{"scheme": PROVIDER_IDENTIFIER_TV}]},  # TV Shows
                {"type": 3, "Scheme": [{"scheme": PROVIDER_IDENTIFIER_TV}]},  # Seasons
                {"type": 4, "Scheme": [{"scheme": PROVIDER_IDENTIFIER_TV}]}   # Episodes
            ],
            "Feature": [
                {"type": "metadata", "key": "/library/metadata"},
                {"type": "match", "key": "/library/metadata/matches"}
            ]
        }
    })


# =============================================================================
# Metadata Endpoint
# =============================================================================

@app.route('/library/metadata/<rating_key>', methods=['GET'])
def get_metadata(rating_key: str):
    """
    Get metadata for a specific item by its rating key.
    
    This is where the actual VPRO lookup happens.
    Returns summary if found, omits summary for fallback to secondary provider.
    """
    logger.info(f"Metadata request for: {rating_key}")
    
    # Check cache first
    cached = _cache_read(rating_key)
    if cached:
        logger.info(f"Cache hit for {rating_key}")

        # Get media type from cache or default to film
        cached_media_type = cached.get("media_type", "film")
        plex_type = "show" if cached_media_type == "series" else "movie"

        plex_metadata = {
            "ratingKey": rating_key,
            "key": f"/library/metadata/{rating_key}",
            "guid": f"{PROVIDER_IDENTIFIER}://{plex_type}/{rating_key}",
            "type": plex_type,
        }

        # Only include summary if we have a description
        if cached.get("description"):
            plex_metadata["summary"] = cached["description"]

        # Include external GUIDs
        guids = []
        if cached.get("imdb_id"):
            guids.append({"id": f"imdb://{cached['imdb_id']}"})
        if cached.get("vpro_id"):
            guids.append({"id": f"vpro://{cached['vpro_id']}"})
        if guids:
            plex_metadata["Guid"] = guids

        return jsonify({
            "MediaContainer": {
                "offset": 0,
                "totalSize": 1,
                "identifier": PROVIDER_IDENTIFIER,
                "size": 1,
                "Metadata": [plex_metadata]
            }
        })

    # Parse rating key to get search parameters
    parsed = parse_rating_key(rating_key)
    title = parsed.get("title")
    year = parsed.get("year")
    imdb_id = parsed.get("imdb_id")
    media_type = parsed.get("media_type", "film")
    
    if not title:
        logger.warning(f"Could not parse title from rating key: {rating_key}")
        return jsonify({
            "MediaContainer": {
                "offset": 0,
                "totalSize": 0,
                "identifier": PROVIDER_IDENTIFIER,
                "size": 0,
                "Metadata": []
            }
        }), 404
    
    plex_type = "show" if media_type == "series" else "movie"
    logger.info(f"Cache miss - searching VPRO: title='{title}', year={year}, imdb={imdb_id}, type={media_type}")

    # Perform the VPRO lookup
    try:
        film = get_vpro_description(
            title=title,
            year=year,
            imdb_id=imdb_id,
            media_type=media_type,
            verbose=False
        )
    except Exception as e:
        logger.error(f"VPRO search error: {e}")
        film = None

    # Build response metadata
    plex_metadata = {
        "ratingKey": rating_key,
        "key": f"/library/metadata/{rating_key}",
        "guid": f"{PROVIDER_IDENTIFIER}://{plex_type}/{rating_key}",
        "type": plex_type,
    }

    if film and film.description:
        # Found - include summary
        plex_metadata["summary"] = film.description

        guids = []
        if film.imdb_id:
            guids.append({"id": f"imdb://{film.imdb_id}"})
        if film.vpro_id:
            guids.append({"id": f"vpro://{film.vpro_id}"})
        if guids:
            plex_metadata["Guid"] = guids

        _cache_write(rating_key, {
            "title": film.title,
            "year": film.year,
            "url": film.url,
            "description": film.description,
            "imdb_id": film.imdb_id,
            "vpro_id": film.vpro_id,
            "media_type": film.media_type,
        })

        logger.info(f"VPRO found: {film.title} ({film.year}) [{film.media_type}] - {len(film.description)} chars")
    else:
        # Not found - return without summary for secondary provider fallback
        logger.info(f"No VPRO match for '{title}' - returning without summary")

        _cache_write(rating_key, {
            "title": title,
            "year": year,
            "url": None,
            "description": None,
            "imdb_id": imdb_id,
            "vpro_id": None,
            "media_type": media_type,
        })
    
    return jsonify({
        "MediaContainer": {
            "offset": 0,
            "totalSize": 1,
            "identifier": PROVIDER_IDENTIFIER,
            "size": 1,
            "Metadata": [plex_metadata]
        }
    })


# =============================================================================
# Match Endpoint
# =============================================================================

@app.route('/library/metadata/matches', methods=['POST'])
def match_metadata():
    """
    Match content based on hints from Plex.
    Returns IMMEDIATELY - actual lookup happens in get_metadata.
    """
    data = request.get_json() or {}
    
    title = data.get('title', '')
    year = data.get('year')
    metadata_type = data.get('type', 1)
    guid = data.get('guid', '')
    filename = data.get('filename', '')
    
    # Try Media array for filename
    media = data.get('Media', [])
    if not filename and media:
        try:
            filename = media[0].get('Part', [{}])[0].get('file', '')
        except (IndexError, KeyError, TypeError):
            pass
    
    # Extract IMDB from guid first, then filename
    imdb_id = None
    if guid:
        imdb_match = re.search(r'(tt\d+)', guid, re.IGNORECASE)
        if imdb_match:
            imdb_id = imdb_match.group(1).lower()
    
    if not imdb_id and filename:
        imdb_id = extract_imdb_from_filename(filename)
        if imdb_id:
            logger.info(f"Extracted IMDB {imdb_id} from filename")
    
    if not year and filename:
        year = extract_year_from_filename(filename)
    
    # Handle different metadata types
    # Type 1 = Movie, Type 2 = TV Show, Type 3 = Season, Type 4 = Episode
    if metadata_type == 1:
        media_type = "film"
        plex_type = "movie"
    elif metadata_type == 2:
        media_type = "series"
        plex_type = "show"
    elif metadata_type == 3:
        # Seasons - return empty to let Plex Series handle it
        logger.info(f"Season match request - delegating to secondary provider")
        return jsonify({
            "MediaContainer": {
                "offset": 0, "totalSize": 0,
                "identifier": PROVIDER_IDENTIFIER, "size": 0,
                "Metadata": []
            }
        })
    elif metadata_type == 4:
        # Episodes - return empty to let Plex Series handle it
        logger.info(f"Episode match request - delegating to secondary provider")
        return jsonify({
            "MediaContainer": {
                "offset": 0, "totalSize": 0,
                "identifier": PROVIDER_IDENTIFIER, "size": 0,
                "Metadata": []
            }
        })
    else:
        return jsonify({
            "MediaContainer": {
                "offset": 0, "totalSize": 0,
                "identifier": PROVIDER_IDENTIFIER, "size": 0,
                "Metadata": []
            }
        })

    logger.info(f"Match request: title='{title}', year={year}, imdb={imdb_id}, type={media_type}")
    
    if not title:
        return jsonify({
            "MediaContainer": {
                "offset": 0, "totalSize": 0,
                "identifier": PROVIDER_IDENTIFIER, "size": 0,
                "Metadata": []
            }
        })
    
    rating_key = generate_rating_key(title, year, imdb_id, media_type)

    match_metadata = {
        "ratingKey": rating_key,
        "key": f"/library/metadata/{rating_key}",
        "guid": f"{PROVIDER_IDENTIFIER}://{plex_type}/{rating_key}",
        "type": plex_type,
        "title": title,
    }
    if year:
        match_metadata["year"] = int(year)

    guids = []
    if imdb_id:
        guids.append({"id": f"imdb://{imdb_id}"})
    if guids:
        match_metadata["Guid"] = guids

    logger.info(f"Match returned: {title} ({year}) [{media_type}] -> {rating_key}")
    
    return jsonify({
        "MediaContainer": {
            "offset": 0,
            "totalSize": 1,
            "identifier": PROVIDER_IDENTIFIER,
            "size": 1,
            "Metadata": [match_metadata]
        }
    })


# =============================================================================
# Images Endpoint
# =============================================================================

@app.route('/library/metadata/<rating_key>/images', methods=['GET'])
def get_images(rating_key: str):
    """Return empty - VPRO doesn't provide artwork."""
    return jsonify({
        "MediaContainer": {
            "offset": 0,
            "totalSize": 0,
            "identifier": PROVIDER_IDENTIFIER,
            "size": 0,
            "Image": []
        }
    })


@app.route('/library/metadata/<rating_key>/extras', methods=['GET'])
def get_extras(rating_key: str):
    """Return empty - no extras."""
    return jsonify({
        "MediaContainer": {
            "offset": 0,
            "totalSize": 0,
            "identifier": PROVIDER_IDENTIFIER,
            "size": 0,
            "Metadata": []
        }
    })


# =============================================================================
# TV Provider Endpoints
# =============================================================================

@app.route('/tv/library/metadata/<rating_key>', methods=['GET'])
def get_metadata_tv(rating_key: str):
    """TV metadata endpoint - delegates to main handler, uses TV identifier in response."""
    logger.info(f"TV Metadata request for: {rating_key}")

    # Check cache first
    cached = _cache_read(rating_key)
    if cached:
        logger.info(f"Cache hit for {rating_key}")
        cached_media_type = cached.get("media_type", "series")
        plex_type = "show" if cached_media_type == "series" else "movie"

        plex_metadata = {
            "ratingKey": rating_key,
            "key": f"/tv/library/metadata/{rating_key}",
            "guid": f"{PROVIDER_IDENTIFIER_TV}://{plex_type}/{rating_key}",
            "type": plex_type,
        }

        if cached.get("found") and cached.get("description"):
            plex_metadata["summary"] = cached["description"]
            logger.info(f"Returning cached summary ({len(cached['description'])} chars)")
        else:
            logger.info("Cache indicates not found - omitting summary for fallback")

        return jsonify({
            "MediaContainer": {
                "offset": 0, "totalSize": 1,
                "identifier": PROVIDER_IDENTIFIER_TV, "size": 1,
                "Metadata": [plex_metadata]
            }
        })

    # Parse rating key
    parsed = parse_rating_key(rating_key)
    title = parsed["title"]
    year = parsed["year"]
    imdb_id = parsed["imdb_id"]
    media_type = parsed.get("media_type", "series")
    plex_type = "show" if media_type == "series" else "movie"

    if not title:
        logger.warning(f"Could not parse rating key: {rating_key}")
        return jsonify({
            "MediaContainer": {
                "offset": 0, "totalSize": 0,
                "identifier": PROVIDER_IDENTIFIER_TV, "size": 0,
                "Metadata": []
            }
        })

    logger.info(f"Searching VPRO for: {title} ({year}) [{media_type}]")

    # Do VPRO lookup
    film = get_vpro_description(title, year, imdb_id, media_type=media_type)

    plex_metadata = {
        "ratingKey": rating_key,
        "key": f"/tv/library/metadata/{rating_key}",
        "guid": f"{PROVIDER_IDENTIFIER_TV}://{plex_type}/{rating_key}",
        "type": plex_type,
    }

    if film and film.description:
        plex_metadata["summary"] = film.description

        _cache_write(rating_key, {
            "found": True,
            "title": film.title,
            "year": film.year,
            "url": film.url,
            "description": film.description,
            "imdb_id": film.imdb_id,
            "vpro_id": film.vpro_id,
            "media_type": film.media_type,
        })

        logger.info(f"VPRO found: {film.title} ({film.year}) [{film.media_type}] - {len(film.description)} chars")
    else:
        _cache_write(rating_key, {
            "found": False,
            "title": title,
            "year": year,
            "url": None,
            "description": None,
            "imdb_id": imdb_id,
            "vpro_id": None,
            "media_type": media_type,
        })
        logger.info(f"VPRO not found: {title} ({year}) - omitting summary for fallback")

    return jsonify({
        "MediaContainer": {
            "offset": 0, "totalSize": 1,
            "identifier": PROVIDER_IDENTIFIER_TV, "size": 1,
            "Metadata": [plex_metadata]
        }
    })


@app.route('/tv/library/metadata/matches', methods=['POST'])
def match_metadata_tv():
    """TV match endpoint - handles TV shows, seasons, episodes."""
    data = request.get_json() or {}

    title = data.get('title', '')
    year = data.get('year')
    metadata_type = data.get('type', 2)  # Default to TV show
    guid = data.get('guid', '')
    filename = data.get('filename', '')

    media = data.get('Media', [])
    if not filename and media:
        try:
            filename = media[0].get('Part', [{}])[0].get('file', '')
        except (IndexError, KeyError, TypeError):
            pass

    imdb_id = None
    if guid:
        imdb_match = re.search(r'(tt\d+)', guid, re.IGNORECASE)
        if imdb_match:
            imdb_id = imdb_match.group(1).lower()

    if not imdb_id and filename:
        imdb_id = extract_imdb_from_filename(filename)

    if not year and filename:
        year = extract_year_from_filename(filename)

    # Handle TV types
    if metadata_type == 2:
        media_type = "series"
        plex_type = "show"
    elif metadata_type in (3, 4):
        # Seasons and Episodes - delegate to secondary provider
        logger.info(f"Season/Episode match request (type {metadata_type}) - delegating to secondary provider")
        return jsonify({
            "MediaContainer": {
                "offset": 0, "totalSize": 0,
                "identifier": PROVIDER_IDENTIFIER_TV, "size": 0,
                "Metadata": []
            }
        })
    else:
        return jsonify({
            "MediaContainer": {
                "offset": 0, "totalSize": 0,
                "identifier": PROVIDER_IDENTIFIER_TV, "size": 0,
                "Metadata": []
            }
        })

    logger.info(f"TV Match request: title='{title}', year={year}, imdb={imdb_id}, type={media_type}")

    if not title:
        return jsonify({
            "MediaContainer": {
                "offset": 0, "totalSize": 0,
                "identifier": PROVIDER_IDENTIFIER_TV, "size": 0,
                "Metadata": []
            }
        })

    rating_key = generate_rating_key(title, year, imdb_id, media_type)

    match_metadata = {
        "ratingKey": rating_key,
        "key": f"/tv/library/metadata/{rating_key}",
        "guid": f"{PROVIDER_IDENTIFIER_TV}://{plex_type}/{rating_key}",
        "type": plex_type,
        "title": title,
    }
    if year:
        match_metadata["year"] = int(year)

    guids = []
    if imdb_id:
        guids.append({"id": f"imdb://{imdb_id}"})
    if guids:
        match_metadata["Guid"] = guids

    logger.info(f"TV Match returned: {title} ({year}) [{media_type}] -> {rating_key}")

    return jsonify({
        "MediaContainer": {
            "offset": 0, "totalSize": 1,
            "identifier": PROVIDER_IDENTIFIER_TV, "size": 1,
            "Metadata": [match_metadata]
        }
    })


@app.route('/tv/library/metadata/<rating_key>/images', methods=['GET'])
def get_images_tv(rating_key: str):
    """TV images endpoint - return empty."""
    return jsonify({
        "MediaContainer": {
            "offset": 0, "totalSize": 0,
            "identifier": PROVIDER_IDENTIFIER_TV, "size": 0,
            "Image": []
        }
    })


@app.route('/tv/library/metadata/<rating_key>/extras', methods=['GET'])
def get_extras_tv(rating_key: str):
    """TV extras endpoint - return empty."""
    return jsonify({
        "MediaContainer": {
            "offset": 0, "totalSize": 0,
            "identifier": PROVIDER_IDENTIFIER_TV, "size": 0,
            "Metadata": []
        }
    })


# =============================================================================
# Health Check
# =============================================================================

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({
        "status": "healthy",
        "version": PROVIDER_VERSION,
        "identifier": PROVIDER_IDENTIFIER,
        "tmdb_configured": bool(os.environ.get("TMDB_API_KEY"))
    })


# =============================================================================
# Main
# =============================================================================

if __name__ == "__main__":
    logger.info(f"Starting VPRO Cinema Provider v{PROVIDER_VERSION} on port {PORT}")
    logger.info(f"Provider identifier: {PROVIDER_IDENTIFIER}")
    logger.info(f"TMDB alternate titles: {'enabled' if os.environ.get('TMDB_API_KEY') else 'disabled'}")
    logger.info(f"Test endpoint: http://localhost:{PORT}/test?title=TITLE&year=YEAR")
    app.run(host="0.0.0.0", port=PORT, debug=False)
