"""
Thread-safe file-based cache with LRU eviction.

Provides:
- Atomic file writes (temp file + rename)
- File locking for concurrent access
- LRU eviction when size limits exceeded
- TTL enforcement for cache entries
- Directory sharding for filesystem performance
"""

import json
import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List

from constants import (
    DEFAULT_CACHE_TTL_FOUND,
    DEFAULT_CACHE_TTL_NOT_FOUND,
    MAX_CACHE_SIZE_MB,
    MAX_CACHE_ENTRIES,
    CacheStatus,
)
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from models import VPROFilm

logger = logging.getLogger(__name__)

# Try to import fcntl for file locking (Unix only)
try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    HAS_FCNTL = False
    logger.debug("fcntl not available, file locking disabled")


@dataclass
class CacheEntry:
    """
    Structured cache entry for metadata.

    All fields are explicitly typed for validation.
    """
    title: str
    year: Optional[int]
    description: Optional[str]
    url: Optional[str]
    imdb_id: Optional[str]
    vpro_id: Optional[str]
    media_type: str
    status: str  # CacheStatus value
    fetched_at: str = ""  # ISO format timestamp
    last_accessed: str = ""  # ISO format timestamp
    # Lookup diagnostics
    lookup_method: Optional[str] = None  # "poms", "tmdb_alt", "web"
    discovered_imdb: Optional[str] = None  # IMDB found via TMDB lookup
    # Content metadata
    content_rating: Optional[str] = None  # Kijkwijzer age rating (AL, 6, 9, 12, 14, 16, 18)
    vpro_rating: Optional[int] = None  # VPRO appreciation rating (1-10)
    images: Optional[List[Dict[str, str]]] = None  # [{type, url, title}]

    def is_expired(self) -> bool:
        """
        Check if this cache entry has expired based on TTL.

        Found entries have longer TTL (30 days) than not-found entries (7 days).

        Returns:
            True if entry has expired
        """
        try:
            if not self.fetched_at:
                return True

            fetched = datetime.fromisoformat(
                self.fetched_at.replace('Z', '+00:00')
            )
            age_seconds = (datetime.now(timezone.utc) - fetched).total_seconds()

            # Use shorter TTL for not-found entries
            if self.status == CacheStatus.NOT_FOUND.value:
                return age_seconds > DEFAULT_CACHE_TTL_NOT_FOUND
            return age_seconds > DEFAULT_CACHE_TTL_FOUND

        except (ValueError, AttributeError):
            return True  # Invalid timestamp = expired

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CacheEntry":
        """
        Create CacheEntry from dictionary.

        Handles missing fields gracefully with defaults.
        """
        return cls(
            title=data.get("title", ""),
            year=data.get("year"),
            description=data.get("description"),
            url=data.get("url"),
            imdb_id=data.get("imdb_id"),
            vpro_id=data.get("vpro_id"),
            media_type=data.get("media_type", "film"),
            status=data.get("status", CacheStatus.NOT_FOUND.value),
            fetched_at=data.get("fetched_at", ""),
            last_accessed=data.get("last_accessed", ""),
            lookup_method=data.get("lookup_method"),
            discovered_imdb=data.get("discovered_imdb"),
            content_rating=data.get("content_rating"),
            vpro_rating=data.get("vpro_rating"),
            images=data.get("images"),
        )

    @classmethod
    def from_vpro_film(
        cls,
        film: "VPROFilm",
        status: str = None,
        lookup_method: str = None,
        sanitize_desc: bool = True,
    ) -> "CacheEntry":
        """
        Create CacheEntry from VPROFilm.

        Factory method to reduce duplication when caching VPRO results.

        Args:
            film: VPROFilm object with metadata
            status: Cache status (defaults to FOUND if film has description)
            lookup_method: Override lookup method (defaults to film.lookup_method)
            sanitize_desc: Whether to sanitize description (default True)

        Returns:
            CacheEntry ready for caching
        """
        # Import here to avoid circular import at module level
        from text_utils import sanitize_description

        # Determine effective values
        effective_imdb = film.imdb_id or film.discovered_imdb
        effective_status = status or (
            CacheStatus.FOUND.value if film.description else CacheStatus.NOT_FOUND.value
        )

        # Sanitize description if needed
        desc = film.description
        if desc and sanitize_desc:
            desc = sanitize_description(desc)

        # Only store discovered_imdb if it's new information (differs from imdb_id)
        discovered = film.discovered_imdb
        if discovered and discovered == film.imdb_id:
            discovered = None

        return cls(
            title=film.title,
            year=film.year,
            description=desc,
            url=film.url,
            imdb_id=effective_imdb,
            vpro_id=film.vpro_id,
            media_type=film.media_type,
            status=effective_status,
            fetched_at="",
            last_accessed="",
            lookup_method=lookup_method or film.lookup_method,
            discovered_imdb=discovered,
            content_rating=film.content_rating,
            vpro_rating=film.vpro_rating,
            images=film.images or None,
        )

    @classmethod
    def not_found(
        cls,
        title: str,
        year: Optional[int] = None,
        imdb_id: Optional[str] = None,
        media_type: str = "film",
    ) -> "CacheEntry":
        """
        Create a NOT_FOUND cache entry.

        Factory method for caching negative lookup results.

        Args:
            title: Original search title
            year: Original search year
            imdb_id: Original search IMDB ID
            media_type: "film" or "series"

        Returns:
            CacheEntry with NOT_FOUND status
        """
        return cls(
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


class FileCache:
    """
    Thread-safe file-based cache with LRU eviction.

    Features:
    - Atomic writes via temp file + rename
    - Optional file locking (Unix) for concurrent access
    - Directory sharding to avoid too many files in one directory
    - LRU eviction when entry count or size limit exceeded
    - TTL enforcement on read

    Usage:
        cache = FileCache("./cache")
        entry = cache.read("vpro-some-key")
        if entry:
            print(entry.description)
        else:
            # Fetch and store
            cache.write("vpro-some-key", new_entry)
    """

    def __init__(self, cache_dir: str = None):
        """
        Initialize the file cache.

        Args:
            cache_dir: Directory for cache files. Defaults to CACHE_DIR env var or ./cache
        """
        self._cache_dir = Path(
            cache_dir or os.environ.get("CACHE_DIR", "./cache")
        )
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._access_times: Dict[str, float] = {}

        # Load existing access times on startup
        self._load_access_times()

    def _get_cache_path(self, key: str) -> Path:
        """
        Get cache file path for a key.

        Uses hash-based directory sharding to avoid filesystem issues
        with too many files in one directory.

        Args:
            key: Cache key

        Returns:
            Path to cache file
        """
        # Hash the key for sharding and filename safety
        key_hash = hashlib.sha256(key.encode()).hexdigest()

        # Use first 2 chars of hash for shard directory
        shard_dir = self._cache_dir / key_hash[:2]
        shard_dir.mkdir(exist_ok=True)

        # Sanitize key for filename (keep it readable)
        safe_key = "".join(
            c if c.isalnum() or c in '-_' else '_'
            for c in key
        )[:80]

        return shard_dir / f"{safe_key}_{key_hash[:12]}.json"

    def _load_access_times(self) -> None:
        """Load access times from existing cache files for LRU tracking."""
        try:
            for item in self._cache_dir.iterdir():
                if item.is_dir() and len(item.name) == 2:
                    for cache_file in item.glob("*.json"):
                        try:
                            stat = cache_file.stat()
                            self._access_times[str(cache_file)] = stat.st_mtime
                        except OSError:
                            pass
        except OSError as e:
            logger.warning(f"Failed to load cache access times: {e}")

    def _lock_file(self, file_handle, exclusive: bool = False) -> None:
        """
        Apply file lock if available.

        Args:
            file_handle: Open file handle
            exclusive: True for write lock, False for read lock
        """
        if HAS_FCNTL:
            try:
                lock_type = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                fcntl.flock(file_handle.fileno(), lock_type | fcntl.LOCK_NB)
            except (IOError, OSError):
                # Lock not available, proceed anyway
                pass

    def _extract_key_components(self, key: str) -> tuple:
        """
        Extract title-year prefix and media type from cache key.

        Args:
            key: Cache key like 'vpro-die-hard-1988-none-m'

        Returns:
            Tuple of (title_year_prefix, type_suffix) or (None, None) if invalid
        """
        parts = key.rsplit("-", 2)
        if len(parts) < 2:
            return None, None

        if parts[-1] in ("m", "s"):
            return parts[0], parts[-1]

        # No type suffix - extract prefix before 'none'
        if "-none" in key:
            return key.rsplit("-none", 1)[0], "m"

        return None, None

    def _has_type_suffix(self, key: str) -> bool:
        """Check if key has media type suffix (-m or -s)."""
        return key.endswith("-m") or key.endswith("-s")

    def _has_none_imdb(self, key: str) -> bool:
        """Check if key has 'none' as IMDB placeholder."""
        return "-none-" in key or key.endswith("-none")

    def _find_by_title_year(self, key: str) -> Path:
        """
        Find cache entry by title+year when IMDB is 'none'.

        Searches for any cached file matching the title-year pattern,
        regardless of IMDB ID. This handles cases where Plex sends a
        request without IMDB but we have a cached entry with IMDB.

        Args:
            key: Cache key like 'vpro-die-hard-1988-none-m'

        Returns:
            Path to matching cache file, or non-existent Path if not found
        """
        title_year_prefix, type_suffix = self._extract_key_components(key)
        if not title_year_prefix:
            logger.debug(f"Cache fallback: key '{key}' has insufficient parts")
            return Path("/nonexistent")

        logger.debug(f"Cache fallback search: prefix='{title_year_prefix}', type='{type_suffix}'")

        # Search all shards for matching files
        try:
            for cache_file in self._cache_dir.glob(f"**/*.json"):
                if not cache_file.parent.name or len(cache_file.parent.name) != 2:
                    continue
                filename = cache_file.stem
                if filename.startswith(title_year_prefix) and f"-{type_suffix}_" in filename:
                    logger.info(f"Cache fallback HIT: {key} -> {filename}")
                    return cache_file
        except OSError as e:
            logger.warning(f"Cache fallback error: {e}")

        return Path("/nonexistent")

    def _unlock_file(self, file_handle) -> None:
        """Release file lock if available."""
        if HAS_FCNTL:
            try:
                fcntl.flock(file_handle.fileno(), fcntl.LOCK_UN)
            except (IOError, OSError):
                pass

    def _resolve_cache_path(self, key: str) -> Optional[Path]:
        """
        Resolve cache path with backward compatibility fallbacks.

        Tries multiple strategies:
        1. Exact key match
        2. Key with default type suffix (-m) for old keys
        3. Title+year search for keys with 'none' IMDB

        Args:
            key: Cache key to resolve

        Returns:
            Path to cache file if found, None otherwise
        """
        # Strategy 1: Exact key
        path = self._get_cache_path(key)
        if path.exists():
            return path

        # Strategy 2: Add default type suffix if missing
        if not self._has_type_suffix(key):
            path = self._get_cache_path(key + "-m")
            if path.exists():
                return path

        # Strategy 3: Search by title+year for 'none' IMDB keys
        if self._has_none_imdb(key):
            path = self._find_by_title_year(key)
            if path.exists():
                return path

        return None

    def read(self, key: str) -> Optional[CacheEntry]:
        """
        Read entry from cache.

        Checks TTL and returns None if expired or not found.
        Supports backward compatibility for:
        - Old keys without -m/-s suffix
        - Keys with 'none' IMDB that may be cached with actual IMDB

        Args:
            key: Cache key

        Returns:
            CacheEntry if found and valid, None otherwise
        """
        cache_path = self._resolve_cache_path(key)
        if not cache_path:
            return None

        try:
            with open(cache_path, 'r', encoding='utf-8') as f:
                self._lock_file(f, exclusive=False)
                try:
                    data = json.load(f)
                finally:
                    self._unlock_file(f)

            entry = CacheEntry.from_dict(data)

            # Check expiration
            if entry.is_expired():
                logger.debug(f"Cache entry expired: {key}")
                self._delete_file(cache_path)
                return None

            # Update access time for LRU
            self._touch(cache_path)

            return entry

        except (json.JSONDecodeError, TypeError, KeyError) as e:
            logger.warning(f"Invalid cache entry {key}: {e}")
            self._delete_file(cache_path)
            return None
        except OSError as e:
            logger.warning(f"Cache read error for {key}: {e}")
            return None

    def write(self, key: str, entry: CacheEntry) -> bool:
        """
        Write entry to cache atomically.

        Uses temp file + rename pattern to prevent corruption.

        Args:
            key: Cache key
            entry: CacheEntry to store

        Returns:
            True if write succeeded
        """
        # Check if eviction needed before writing
        self._maybe_evict()

        cache_path = self._get_cache_path(key)

        try:
            # Set timestamps
            now = datetime.now(timezone.utc).isoformat()
            entry.last_accessed = now
            if not entry.fetched_at:
                entry.fetched_at = now

            # Write to temp file first
            temp_path = cache_path.with_suffix('.tmp')

            with open(temp_path, 'w', encoding='utf-8') as f:
                self._lock_file(f, exclusive=True)
                try:
                    json.dump(entry.to_dict(), f, ensure_ascii=False, indent=2)
                finally:
                    self._unlock_file(f)

            # Atomic rename
            temp_path.replace(cache_path)

            # Update access time tracking
            with self._lock:
                self._access_times[str(cache_path)] = time.time()

            return True

        except OSError as e:
            logger.warning(f"Cache write error for {key}: {e}")
            # Clean up temp file if it exists
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    def _touch(self, cache_path: Path) -> None:
        """Update access time for LRU tracking."""
        try:
            cache_path.touch()
            with self._lock:
                self._access_times[str(cache_path)] = time.time()
        except OSError:
            pass

    def _delete_file(self, cache_path: Path) -> None:
        """Delete a cache file and remove from tracking."""
        try:
            cache_path.unlink(missing_ok=True)
            with self._lock:
                self._access_times.pop(str(cache_path), None)
        except OSError:
            pass

    def _maybe_evict(self) -> None:
        """
        Evict oldest entries if cache exceeds limits.

        Triggered before writes to ensure space is available.
        """
        with self._lock:
            # Check entry count first (fast)
            if len(self._access_times) < MAX_CACHE_ENTRIES:
                # Also check total size
                try:
                    total_size = sum(
                        Path(p).stat().st_size
                        for p in self._access_times.keys()
                        if Path(p).exists()
                    )
                    if total_size < MAX_CACHE_SIZE_MB * 1024 * 1024:
                        return  # No eviction needed
                except OSError:
                    return

            # Evict oldest 10% by access time
            sorted_by_access = sorted(
                self._access_times.items(),
                key=lambda x: x[1]
            )
            to_evict = sorted_by_access[:max(1, len(sorted_by_access) // 10)]

            for path_str, _ in to_evict:
                self._delete_file(Path(path_str))

            logger.info(f"Evicted {len(to_evict)} cache entries")

    def delete(self, key: str) -> bool:
        """
        Delete a specific cache entry.

        Args:
            key: Cache key to delete

        Returns:
            True if entry was deleted
        """
        cache_path = self._get_cache_path(key)
        if cache_path.exists():
            self._delete_file(cache_path)
            return True
        return False

    def clear(self, preserve_credentials: bool = True) -> int:
        """
        Clear all cache entries.

        Args:
            preserve_credentials: Keep credentials.json if True

        Returns:
            Number of files deleted
        """
        count = 0
        try:
            # Clear sharded directories
            for item in self._cache_dir.iterdir():
                if item.is_dir() and len(item.name) == 2:
                    for cache_file in item.glob("*.json"):
                        self._delete_file(cache_file)
                        count += 1

            # Clear root level json files (except credentials)
            for cache_file in self._cache_dir.glob("*.json"):
                if preserve_credentials and "credentials" in cache_file.name:
                    continue
                self._delete_file(cache_file)
                count += 1

        except OSError as e:
            logger.warning(f"Cache clear error: {e}")

        return count

    def stats(self) -> Dict[str, Any]:
        """
        Get cache statistics.

        Returns:
            Dict with cache stats
        """
        with self._lock:
            total_size = 0
            found_count = 0
            not_found_count = 0
            expired_count = 0

            for path_str in list(self._access_times.keys()):
                path = Path(path_str)
                if path.exists():
                    try:
                        total_size += path.stat().st_size
                        data = json.loads(path.read_text())
                        entry = CacheEntry.from_dict(data)

                        if entry.is_expired():
                            expired_count += 1
                        elif entry.description:
                            found_count += 1
                        else:
                            not_found_count += 1
                    except (OSError, json.JSONDecodeError):
                        pass

            return {
                "total_entries": len(self._access_times),
                "found_entries": found_count,
                "not_found_entries": not_found_count,
                "expired_entries": expired_count,
                "total_size_mb": round(total_size / 1024 / 1024, 2),
                "max_entries": MAX_CACHE_ENTRIES,
                "max_size_mb": MAX_CACHE_SIZE_MB,
            }

    def keys(self) -> List[str]:
        """
        Get all cache keys.

        Note: This reconstructs keys from filenames, may not be exact.

        Returns:
            List of cache keys
        """
        keys = []
        try:
            for item in self._cache_dir.iterdir():
                if item.is_dir() and len(item.name) == 2:
                    for cache_file in item.glob("*.json"):
                        # Extract key from filename (before hash suffix)
                        name = cache_file.stem
                        if '_' in name:
                            key_part = name.rsplit('_', 1)[0]
                            keys.append(key_part)
        except OSError:
            pass
        return keys
