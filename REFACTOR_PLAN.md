# VPRO Cinema Plex - Refactor Plan

*Prepared by: The quiet one in the corner*

---

## Overview

This plan addresses the critical issues identified in code review, organized by priority and dependency order. Changes are designed to be backwards-compatible where possible.

---

## Phase 1: Foundation (No Breaking Changes)

### 1.1 Create Shared Constants Module

**File:** `constants.py` (new)

```python
from enum import Enum, auto
from typing import Final

class MediaType(str, Enum):
    """Media type enumeration - string enum for JSON serialization."""
    FILM = "film"
    SERIES = "series"
    ALL = "all"

    @classmethod
    def from_plex_type(cls, plex_type: int) -> "MediaType":
        """Convert Plex type ID to MediaType."""
        return {1: cls.FILM, 2: cls.SERIES, 3: cls.SERIES, 4: cls.SERIES}.get(plex_type, cls.FILM)

    def to_plex_type_str(self) -> str:
        """Convert to Plex type string."""
        return "show" if self == MediaType.SERIES else "movie"

class CacheStatus(str, Enum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    EXPIRED = "expired"

# Provider constants
PROVIDER_IDENTIFIER: Final = "tv.plex.agents.custom.vpro.cinema"
PROVIDER_IDENTIFIER_TV: Final = "tv.plex.agents.custom.vpro.cinema.tv"
PROVIDER_VERSION: Final = "3.1.0"

# Cache settings
DEFAULT_CACHE_TTL_FOUND: Final = 30 * 24 * 60 * 60  # 30 days for found entries
DEFAULT_CACHE_TTL_NOT_FOUND: Final = 7 * 24 * 60 * 60  # 7 days for not-found
MAX_CACHE_SIZE_MB: Final = 500  # Maximum cache size
MAX_CACHE_ENTRIES: Final = 10000  # Maximum number of cached items

# Rate limits (requests per second)
RATE_LIMIT_POMS: Final = 5.0
RATE_LIMIT_TMDB: Final = 4.0  # TMDB allows 40/10s
RATE_LIMIT_WEB_SEARCH: Final = 0.5  # Be nice to search engines

# Retry settings
MAX_RETRIES: Final = 3
RETRY_BACKOFF_BASE: Final = 2.0  # Exponential backoff base (seconds)

# Title matching
TITLE_SIMILARITY_THRESHOLD: Final = 0.3
YEAR_TOLERANCE: Final = 2
MAX_TITLE_LENGTH: Final = 50
```

**Rationale:** Centralizes all magic strings/numbers, uses proper typing, enables IDE autocompletion and catches typos at import time.

---

### 1.2 Thread-Safe Credential Manager

**File:** `credentials.py` (new, extracted from vpro_cinema_scraper.py)

```python
import threading
import json
import os
import time
import logging
from typing import Optional, Tuple
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class Credentials:
    """Immutable credential holder."""
    api_key: str
    api_secret: str
    fetched_at: datetime
    source: str

class CredentialManager:
    """
    Thread-safe credential manager with automatic refresh.

    Uses a read-write lock pattern:
    - Multiple readers can access credentials simultaneously
    - Writers get exclusive access during refresh
    """

    CREDENTIAL_URL = "https://www.vprogids.nl/cinema/zoek.html"
    DEFAULT_API_KEY = "ione7ahfij"
    DEFAULT_API_SECRET = "aag9veesei"

    # More robust patterns - order matters (most specific first)
    CREDENTIAL_PATTERNS = [
        (r'vpronlApiKey\s*[=:]\s*["\']([a-z0-9]{8,15})["\']',
         r'vpronlSecret\s*[=:]\s*["\']([a-z0-9]{8,15})["\']'),
        (r'"apiKey"\s*:\s*"([a-z0-9]{8,15})"',
         r'"(?:apiSecret|secret)"\s*:\s*"([a-z0-9]{8,15})"'),
    ]

    _instance: Optional["CredentialManager"] = None
    _instance_lock = threading.Lock()

    def __new__(cls, cache_file: str = None):
        """Singleton pattern with thread-safe initialization."""
        if cls._instance is None:
            with cls._instance_lock:
                # Double-check locking
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._initialized = False
                    cls._instance = instance
        return cls._instance

    def __init__(self, cache_file: str = None):
        if self._initialized:
            return

        self._lock = threading.RLock()  # Reentrant for nested calls
        self._credentials: Optional[Credentials] = None
        self._cache_file = Path(cache_file or os.environ.get(
            "POMS_CACHE_FILE", "./cache/credentials.json"
        ))
        self._refresh_in_progress = False
        self._last_refresh_attempt = 0.0
        self._refresh_cooldown = 60.0  # Minimum seconds between refresh attempts

        self._load_cached()
        self._initialized = True

    def _load_cached(self) -> bool:
        """Load credentials from cache file (called during init, already locked)."""
        if not self._cache_file.exists():
            return False

        try:
            data = json.loads(self._cache_file.read_text())
            self._credentials = Credentials(
                api_key=data["api_key"],
                api_secret=data["api_secret"],
                fetched_at=datetime.fromisoformat(data.get("fetched_at", "2000-01-01")),
                source=data.get("source", "cache")
            )
            logger.debug(f"Loaded cached credentials from {self._cache_file}")
            return True
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning(f"Failed to load cached credentials: {e}")
            return False

    def _save_cache(self, creds: Credentials) -> None:
        """Save credentials to cache file atomically."""
        try:
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)

            # Write to temp file first, then atomic rename
            temp_file = self._cache_file.with_suffix('.tmp')
            temp_file.write_text(json.dumps({
                "api_key": creds.api_key,
                "api_secret": creds.api_secret,
                "fetched_at": creds.fetched_at.isoformat(),
                "source": creds.source,
            }, indent=2))
            temp_file.replace(self._cache_file)  # Atomic on POSIX

        except OSError as e:
            logger.warning(f"Failed to save credentials cache: {e}")

    @property
    def api_key(self) -> str:
        with self._lock:
            return self._credentials.api_key if self._credentials else self.DEFAULT_API_KEY

    @property
    def api_secret(self) -> str:
        with self._lock:
            return self._credentials.api_secret if self._credentials else self.DEFAULT_API_SECRET

    def get_credentials(self) -> Tuple[str, str]:
        """Get current credentials as tuple (key, secret)."""
        with self._lock:
            if self._credentials:
                return (self._credentials.api_key, self._credentials.api_secret)
            return (self.DEFAULT_API_KEY, self.DEFAULT_API_SECRET)

    def invalidate_and_refresh(self) -> bool:
        """
        Invalidate current credentials and fetch fresh ones.
        Thread-safe with cooldown to prevent hammering.

        Returns True if refresh succeeded.
        """
        with self._lock:
            # Check cooldown
            now = time.monotonic()
            if now - self._last_refresh_attempt < self._refresh_cooldown:
                logger.debug("Credential refresh on cooldown, skipping")
                return self._credentials is not None

            # Check if another thread is already refreshing
            if self._refresh_in_progress:
                logger.debug("Credential refresh already in progress")
                return self._credentials is not None

            self._refresh_in_progress = True
            self._last_refresh_attempt = now

        try:
            # Do the actual fetch outside the lock
            new_creds = self._fetch_fresh_credentials()

            with self._lock:
                if new_creds:
                    self._credentials = new_creds
                    self._save_cache(new_creds)
                    return True
                return False
        finally:
            with self._lock:
                self._refresh_in_progress = False

    def _fetch_fresh_credentials(self) -> Optional[Credentials]:
        """Fetch fresh credentials from vprogids.nl."""
        logger.info("Fetching fresh API credentials...")

        try:
            session = requests.Session()
            session.headers.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept-Language': 'nl-NL,nl;q=0.9',
            })

            response = session.get(self.CREDENTIAL_URL, timeout=15)
            response.raise_for_status()

            api_key, api_secret = self._extract_credentials(response.text)

            # Also check linked JS files if not found
            if not (api_key and api_secret):
                soup = BeautifulSoup(response.text, 'html.parser')
                for script in soup.find_all('script', src=True):
                    src = script['src']
                    if not src.startswith('http'):
                        base = "https://www.vprogids.nl"
                        src = f"{base}{src}" if src.startswith('/') else f"{base}/{src}"

                    try:
                        js_resp = session.get(src, timeout=10)
                        if js_resp.ok:
                            k, s = self._extract_credentials(js_resp.text)
                            api_key = api_key or k
                            api_secret = api_secret or s
                            if api_key and api_secret:
                                break
                    except requests.RequestException:
                        continue

            if api_key and api_secret:
                logger.info("Successfully extracted fresh credentials")
                return Credentials(
                    api_key=api_key,
                    api_secret=api_secret,
                    fetched_at=datetime.now(timezone.utc),
                    source=self.CREDENTIAL_URL
                )

            logger.warning("Could not extract credentials from vprogids.nl")
            return None

        except requests.RequestException as e:
            logger.error(f"Failed to fetch credentials: {e}")
            return None

    def _extract_credentials(self, text: str) -> Tuple[Optional[str], Optional[str]]:
        """Extract credentials from page text using patterns."""
        import re
        api_key = api_secret = None

        for key_pattern, secret_pattern in self.CREDENTIAL_PATTERNS:
            if not api_key:
                match = re.search(key_pattern, text, re.IGNORECASE)
                if match:
                    api_key = match.group(1)
            if not api_secret:
                match = re.search(secret_pattern, text, re.IGNORECASE)
                if match:
                    api_secret = match.group(1)
            if api_key and api_secret:
                break

        return api_key, api_secret


def get_credential_manager() -> CredentialManager:
    """Get the singleton credential manager instance."""
    return CredentialManager()
```

**Key improvements:**
- Singleton pattern with double-checked locking
- RLock for thread safety
- Cooldown period prevents hammering on failures
- Atomic file writes prevent corruption
- Immutable Credentials dataclass prevents accidental mutation

---

### 1.3 Atomic File Cache with LRU Eviction

**File:** `cache.py` (new)

```python
import json
import os
import time
import threading
import logging
import hashlib
from pathlib import Path
from typing import Optional, Dict, Any
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import fcntl  # Unix file locking

from constants import (
    DEFAULT_CACHE_TTL_FOUND,
    DEFAULT_CACHE_TTL_NOT_FOUND,
    MAX_CACHE_SIZE_MB,
    MAX_CACHE_ENTRIES,
    CacheStatus,
)

logger = logging.getLogger(__name__)

@dataclass
class CacheEntry:
    """Structured cache entry."""
    title: str
    year: Optional[int]
    description: Optional[str]
    url: Optional[str]
    imdb_id: Optional[str]
    vpro_id: Optional[str]
    media_type: str
    status: str  # CacheStatus value
    fetched_at: str  # ISO format
    last_accessed: str  # ISO format, for LRU

    def is_expired(self) -> bool:
        """Check if this cache entry has expired."""
        try:
            fetched = datetime.fromisoformat(self.fetched_at.replace('Z', '+00:00'))
            age_seconds = (datetime.now(timezone.utc) - fetched).total_seconds()

            ttl = DEFAULT_CACHE_TTL_FOUND if self.description else DEFAULT_CACHE_TTL_NOT_FOUND
            return age_seconds > ttl
        except (ValueError, AttributeError):
            return True  # Invalid timestamp = expired

class FileCache:
    """
    Thread-safe file-based cache with:
    - Atomic writes (temp file + rename)
    - File locking for concurrent access
    - LRU eviction when size limit exceeded
    - TTL enforcement
    """

    def __init__(self, cache_dir: str = None):
        self._cache_dir = Path(cache_dir or os.environ.get("CACHE_DIR", "./cache"))
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._access_times: Dict[str, float] = {}  # In-memory LRU tracking

        # Load existing access times on startup
        self._load_access_times()

    def _get_cache_path(self, key: str) -> Path:
        """Get cache file path for a key (uses hash to avoid filesystem issues)."""
        # Use hash prefix for directory sharding (prevents too many files in one dir)
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        shard_dir = self._cache_dir / key_hash[:2]
        shard_dir.mkdir(exist_ok=True)

        # Sanitize key for filename
        safe_key = "".join(c if c.isalnum() or c in '-_' else '_' for c in key)[:100]
        return shard_dir / f"{safe_key}_{key_hash[:16]}.json"

    def _load_access_times(self) -> None:
        """Load access times from existing cache files."""
        try:
            for shard in self._cache_dir.iterdir():
                if shard.is_dir() and len(shard.name) == 2:
                    for cache_file in shard.glob("*.json"):
                        try:
                            stat = cache_file.stat()
                            self._access_times[str(cache_file)] = stat.st_mtime
                        except OSError:
                            pass
        except OSError as e:
            logger.warning(f"Failed to load cache access times: {e}")

    def read(self, key: str) -> Optional[CacheEntry]:
        """Read from cache, returns None if not found or expired."""
        cache_path = self._get_cache_path(key)

        if not cache_path.exists():
            return None

        try:
            # File locking for concurrent read safety
            with open(cache_path, 'r', encoding='utf-8') as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)  # Shared lock for reading
                try:
                    data = json.load(f)
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)

            entry = CacheEntry(**data)

            # Check expiration
            if entry.is_expired():
                logger.debug(f"Cache entry expired: {key}")
                self._delete(cache_path)
                return None

            # Update access time (for LRU)
            self._touch(cache_path)

            return entry

        except (json.JSONDecodeError, TypeError, KeyError) as e:
            logger.warning(f"Invalid cache entry {key}: {e}")
            self._delete(cache_path)
            return None
        except OSError as e:
            logger.warning(f"Cache read error for {key}: {e}")
            return None

    def write(self, key: str, entry: CacheEntry) -> bool:
        """Write to cache atomically."""
        cache_path = self._get_cache_path(key)

        # Check if eviction needed before writing
        self._maybe_evict()

        try:
            # Update timestamps
            now = datetime.now(timezone.utc).isoformat()
            entry.last_accessed = now
            if not entry.fetched_at:
                entry.fetched_at = now

            # Write to temp file first
            temp_path = cache_path.with_suffix('.tmp')

            with open(temp_path, 'w', encoding='utf-8') as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)  # Exclusive lock
                try:
                    json.dump(asdict(entry), f, ensure_ascii=False, indent=2)
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)

            # Atomic rename
            temp_path.replace(cache_path)

            # Update in-memory access tracking
            with self._lock:
                self._access_times[str(cache_path)] = time.time()

            return True

        except OSError as e:
            logger.warning(f"Cache write error for {key}: {e}")
            return False

    def _touch(self, cache_path: Path) -> None:
        """Update access time for LRU tracking."""
        try:
            cache_path.touch()
            with self._lock:
                self._access_times[str(cache_path)] = time.time()
        except OSError:
            pass

    def _delete(self, cache_path: Path) -> None:
        """Delete a cache file."""
        try:
            cache_path.unlink(missing_ok=True)
            with self._lock:
                self._access_times.pop(str(cache_path), None)
        except OSError:
            pass

    def _maybe_evict(self) -> None:
        """Evict oldest entries if cache is too large."""
        with self._lock:
            # Check entry count
            if len(self._access_times) < MAX_CACHE_ENTRIES:
                # Also check size (rough estimate)
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
                self._delete(Path(path_str))

            logger.info(f"Evicted {len(to_evict)} cache entries")

    def clear(self, preserve_credentials: bool = True) -> int:
        """Clear all cache entries. Returns count of deleted files."""
        count = 0
        try:
            for shard in self._cache_dir.iterdir():
                if shard.is_dir() and len(shard.name) == 2:
                    for cache_file in shard.glob("*.json"):
                        if preserve_credentials and "credentials" in cache_file.name:
                            continue
                        self._delete(cache_file)
                        count += 1
        except OSError as e:
            logger.warning(f"Cache clear error: {e}")

        return count

    def stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        with self._lock:
            total_size = 0
            expired_count = 0
            found_count = 0
            not_found_count = 0

            for path_str in list(self._access_times.keys()):
                path = Path(path_str)
                if path.exists():
                    try:
                        total_size += path.stat().st_size
                        data = json.loads(path.read_text())
                        if data.get("description"):
                            found_count += 1
                        else:
                            not_found_count += 1
                    except (OSError, json.JSONDecodeError):
                        pass

            return {
                "total_entries": len(self._access_times),
                "found_entries": found_count,
                "not_found_entries": not_found_count,
                "total_size_mb": round(total_size / 1024 / 1024, 2),
                "max_entries": MAX_CACHE_ENTRIES,
                "max_size_mb": MAX_CACHE_SIZE_MB,
            }
```

**Key improvements:**
- File locking with `fcntl` prevents corruption
- Atomic writes via temp file + rename
- LRU eviction prevents unbounded growth
- Directory sharding for filesystem performance
- Structured entries with validation

---

## Phase 2: Network Resilience

### 2.1 HTTP Client with Retry Logic & Rate Limiting

**File:** `http_client.py` (new)

```python
import time
import threading
import logging
from typing import Optional, Dict, Any, Callable
from functools import wraps
from dataclasses import dataclass
from collections import defaultdict

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from constants import (
    MAX_RETRIES,
    RETRY_BACKOFF_BASE,
    RATE_LIMIT_POMS,
    RATE_LIMIT_TMDB,
    RATE_LIMIT_WEB_SEARCH,
)

logger = logging.getLogger(__name__)

@dataclass
class RateLimitConfig:
    """Rate limit configuration."""
    requests_per_second: float
    burst_size: int = 1

class TokenBucketRateLimiter:
    """
    Thread-safe token bucket rate limiter.

    Allows bursting up to `burst_size` requests, then enforces
    `requests_per_second` limit.
    """

    def __init__(self, requests_per_second: float, burst_size: int = 1):
        self.rate = requests_per_second
        self.burst_size = burst_size
        self.tokens = float(burst_size)
        self.last_update = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, timeout: float = 30.0) -> bool:
        """
        Acquire a token, blocking until available or timeout.
        Returns True if acquired, False if timeout.
        """
        deadline = time.monotonic() + timeout

        while True:
            with self._lock:
                now = time.monotonic()
                # Refill tokens based on elapsed time
                elapsed = now - self.last_update
                self.tokens = min(self.burst_size, self.tokens + elapsed * self.rate)
                self.last_update = now

                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return True

                # Calculate wait time for next token
                wait_time = (1.0 - self.tokens) / self.rate

            if time.monotonic() + wait_time > deadline:
                return False

            time.sleep(min(wait_time, 0.1))  # Sleep in small increments


class RateLimitedSession:
    """
    requests.Session wrapper with:
    - Connection pooling
    - Automatic retries with exponential backoff
    - Per-host rate limiting
    - Proper timeout handling
    """

    # Class-level rate limiters (shared across instances)
    _rate_limiters: Dict[str, TokenBucketRateLimiter] = {}
    _rate_limiter_lock = threading.Lock()

    # Default rate limits by domain pattern
    DEFAULT_RATE_LIMITS = {
        "poms.omroep.nl": RateLimitConfig(RATE_LIMIT_POMS, burst_size=3),
        "api.themoviedb.org": RateLimitConfig(RATE_LIMIT_TMDB, burst_size=5),
        "duckduckgo.com": RateLimitConfig(RATE_LIMIT_WEB_SEARCH, burst_size=2),
        "startpage.com": RateLimitConfig(RATE_LIMIT_WEB_SEARCH, burst_size=1),
        "vprogids.nl": RateLimitConfig(2.0, burst_size=3),  # Be nice
    }

    def __init__(
        self,
        timeout: float = 30.0,
        max_retries: int = MAX_RETRIES,
        backoff_factor: float = RETRY_BACKOFF_BASE,
        user_agent: str = None,
    ):
        self.timeout = timeout
        self.session = requests.Session()

        # Configure retry strategy
        retry_strategy = Retry(
            total=max_retries,
            backoff_factor=backoff_factor,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
            raise_on_status=False,  # Don't raise, let us handle it
        )

        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=10,
            pool_maxsize=20,
        )
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        # Default headers
        self.session.headers.update({
            "User-Agent": user_agent or (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/html, */*",
            "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
        })

    def _get_rate_limiter(self, url: str) -> Optional[TokenBucketRateLimiter]:
        """Get or create rate limiter for URL's host."""
        from urllib.parse import urlparse
        host = urlparse(url).netloc.lower()

        # Find matching rate limit config
        config = None
        for pattern, cfg in self.DEFAULT_RATE_LIMITS.items():
            if pattern in host:
                config = cfg
                break

        if not config:
            return None

        with self._rate_limiter_lock:
            if host not in self._rate_limiters:
                self._rate_limiters[host] = TokenBucketRateLimiter(
                    config.requests_per_second,
                    config.burst_size
                )
            return self._rate_limiters[host]

    def _apply_rate_limit(self, url: str) -> None:
        """Block until rate limit allows request."""
        limiter = self._get_rate_limiter(url)
        if limiter:
            if not limiter.acquire(timeout=60.0):
                raise requests.exceptions.Timeout("Rate limit timeout")

    def get(self, url: str, **kwargs) -> requests.Response:
        """Rate-limited GET request."""
        self._apply_rate_limit(url)
        kwargs.setdefault("timeout", self.timeout)
        return self.session.get(url, **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        """Rate-limited POST request."""
        self._apply_rate_limit(url)
        kwargs.setdefault("timeout", self.timeout)
        return self.session.post(url, **kwargs)

    def close(self) -> None:
        """Close the session."""
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# Convenience function for creating configured sessions
def create_session(
    service_name: str = "default",
    timeout: float = 30.0,
) -> RateLimitedSession:
    """Create a rate-limited session for a specific service."""
    return RateLimitedSession(timeout=timeout)
```

**Key improvements:**
- Token bucket rate limiting prevents API abuse
- Connection pooling via HTTPAdapter
- Automatic retries with exponential backoff
- Per-host rate limiting
- Proper resource cleanup

---

### 2.2 Fix CAPTCHA Detection

**In `vpro_cinema_scraper.py`, update `search_startpage` method:**

```python
def _is_captcha_page(self, html: str) -> bool:
    """
    Detect CAPTCHA/bot protection pages without false positives.

    Checks for specific CAPTCHA indicators, not just word presence.
    """
    html_lower = html.lower()

    # Specific CAPTCHA indicators (not just word presence)
    captcha_indicators = [
        'id="captcha"',
        'class="captcha"',
        'name="captcha"',
        'g-recaptcha',
        'h-captcha',
        'cf-turnstile',  # Cloudflare
        'please verify you are human',
        'confirm you are not a robot',
        'complete the security check',
        'unusual traffic from your computer',
        '/captcha/',
        'data-sitekey=',  # reCAPTCHA/hCaptcha site key
    ]

    # Check for actual CAPTCHA elements, not just the word
    for indicator in captcha_indicators:
        if indicator in html_lower:
            return True

    # Additional check: very short response with "robot" often indicates block
    # But only if the page is suspiciously short (not a real content page)
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
```

---

## Phase 3: Input Validation & Normalization

### 3.1 Title Normalization Module

**File:** `text_utils.py` (new)

```python
import re
import unicodedata
from typing import Optional, Set

def normalize_unicode(text: str) -> str:
    """
    Normalize Unicode text for comparison.

    - NFKC normalization (compatibility decomposition + canonical composition)
    - Converts full-width to half-width
    - Normalizes different dash types
    - Normalizes quotes
    """
    if not text:
        return ""

    # NFKC handles full-width → half-width, ligatures, etc.
    text = unicodedata.normalize('NFKC', text)

    # Normalize various dash types to simple hyphen
    dashes = '\u2010\u2011\u2012\u2013\u2014\u2015\u2212\uFE58\uFE63\uFF0D'
    for dash in dashes:
        text = text.replace(dash, '-')

    # Normalize quotes
    text = text.replace('"', '"').replace('"', '"')
    text = text.replace(''', "'").replace(''', "'")
    text = text.replace('«', '"').replace('»', '"')

    return text

def normalize_for_comparison(text: str) -> str:
    """
    Normalize text for fuzzy comparison.

    - Lowercase
    - Remove accents (café → cafe)
    - Remove punctuation
    - Collapse whitespace
    """
    if not text:
        return ""

    text = normalize_unicode(text).lower()

    # Remove accents but keep base characters
    # NFD decomposes, then we strip combining marks
    text = unicodedata.normalize('NFD', text)
    text = ''.join(c for c in text if unicodedata.category(c) != 'Mn')

    # Remove punctuation except spaces
    text = re.sub(r'[^\w\s]', '', text)

    # Collapse whitespace
    text = ' '.join(text.split())

    return text

def normalize_for_cache_key(text: str, max_length: int = 50) -> str:
    """
    Normalize text for use in cache keys/filenames.

    - Lowercase
    - Only alphanumeric and hyphens
    - Truncate to max_length
    - Add hash suffix for collision resistance
    """
    if not text:
        return "unknown"

    import hashlib

    normalized = normalize_for_comparison(text)

    # Convert spaces to hyphens, keep only safe chars
    safe = re.sub(r'[^a-z0-9\s-]', '', normalized)
    safe = re.sub(r'\s+', '-', safe)
    safe = re.sub(r'-+', '-', safe).strip('-')

    if not safe:
        safe = "unknown"

    # Truncate but add hash for uniqueness
    if len(safe) > max_length - 8:  # Leave room for hash
        text_hash = hashlib.sha256(text.encode()).hexdigest()[:8]
        safe = f"{safe[:max_length - 9]}-{text_hash}"

    return safe[:max_length]

def titles_match(title1: str, title2: str) -> bool:
    """Check if two titles match after normalization."""
    return normalize_for_comparison(title1) == normalize_for_comparison(title2)

def title_similarity(title1: str, title2: str) -> float:
    """
    Calculate Jaccard similarity between normalized titles.
    Returns value between 0.0 and 1.0.
    """
    words1 = set(normalize_for_comparison(title1).split())
    words2 = set(normalize_for_comparison(title2).split())

    if not words1 or not words2:
        return 0.0

    intersection = words1 & words2
    union = words1 | words2

    return len(intersection) / len(union)

def sanitize_description(text: str) -> str:
    """
    Sanitize description text.

    - Strip HTML tags
    - Decode HTML entities
    - Normalize whitespace
    - Remove control characters
    """
    if not text:
        return ""

    import html

    # Decode HTML entities
    text = html.unescape(text)

    # Remove HTML tags
    text = re.sub(r'<[^>]+>', '', text)

    # Remove control characters (except newlines)
    text = ''.join(c for c in text if c == '\n' or unicodedata.category(c)[0] != 'C')

    # Normalize whitespace
    text = ' '.join(text.split())

    return text.strip()

def validate_rating_key(key: str) -> bool:
    """
    Validate rating key format to prevent path traversal.
    """
    if not key:
        return False

    # Must start with vpro-
    if not key.startswith('vpro-'):
        return False

    # No path separators or traversal
    if any(c in key for c in ['/', '\\', '..', '\x00']):
        return False

    # Reasonable length
    if len(key) > 200:
        return False

    # Only safe characters
    if not re.match(r'^vpro-[a-z0-9\-]+$', key):
        return False

    return True
```

---

## Phase 4: API Consolidation & Observability

### 4.1 Unified Metadata Handler

**In `vpro_metadata_provider.py`, create shared handler:**

```python
from dataclasses import dataclass
from typing import Optional
from enum import Enum

from constants import MediaType, PROVIDER_IDENTIFIER, PROVIDER_IDENTIFIER_TV
from text_utils import validate_rating_key

@dataclass
class MetadataRequest:
    """Unified metadata request."""
    rating_key: str
    provider_type: str  # "movie" or "tv"

    @property
    def identifier(self) -> str:
        return PROVIDER_IDENTIFIER_TV if self.provider_type == "tv" else PROVIDER_IDENTIFIER

    @property
    def base_path(self) -> str:
        return "/tv" if self.provider_type == "tv" else ""

def handle_metadata_request(req: MetadataRequest, cache: FileCache) -> dict:
    """
    Unified metadata handler for both movie and TV endpoints.

    This eliminates the code duplication between get_metadata() and get_metadata_tv().
    """
    logger.info(f"[{request_id}] Metadata request: {req.rating_key}")

    # Validate rating key
    if not validate_rating_key(req.rating_key):
        logger.warning(f"Invalid rating key: {req.rating_key}")
        return _empty_response(req.identifier)

    # Check cache
    cached = cache.read(req.rating_key)
    if cached:
        logger.info(f"[{request_id}] Cache hit for {req.rating_key}")
        return _build_response(req, cached)

    # Parse rating key
    parsed = parse_rating_key(req.rating_key)
    title = parsed.get("title")
    year = parsed.get("year")
    imdb_id = parsed.get("imdb_id")
    media_type = parsed.get("media_type", "film")

    if not title:
        logger.warning(f"Could not parse title from: {req.rating_key}")
        return _empty_response(req.identifier)

    logger.info(f"[{request_id}] Cache miss - searching: {title} ({year}) [{media_type}]")

    # Perform lookup
    try:
        film = get_vpro_description(
            title=title,
            year=year,
            imdb_id=imdb_id,
            media_type=media_type,
        )
    except Exception as e:
        logger.error(f"[{request_id}] Search error: {e}")
        film = None

    # Build and cache result
    if film and film.description:
        entry = CacheEntry(
            title=film.title,
            year=film.year,
            description=sanitize_description(film.description),
            url=film.url,
            imdb_id=film.imdb_id,
            vpro_id=film.vpro_id,
            media_type=film.media_type,
            status=CacheStatus.FOUND.value,
            fetched_at="",
            last_accessed="",
        )
        cache.write(req.rating_key, entry)
        logger.info(f"[{request_id}] Found: {film.title} ({film.year})")
    else:
        entry = CacheEntry(
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
        cache.write(req.rating_key, entry)
        logger.info(f"[{request_id}] Not found: {title}")

    return _build_response(req, entry)

def _build_response(req: MetadataRequest, entry: CacheEntry) -> dict:
    """Build Plex-compatible metadata response."""
    plex_type = MediaType(entry.media_type).to_plex_type_str()

    metadata = {
        "ratingKey": req.rating_key,
        "key": f"{req.base_path}/library/metadata/{req.rating_key}",
        "guid": f"{req.identifier}://{plex_type}/{req.rating_key}",
        "type": plex_type,
    }

    if entry.description:
        metadata["summary"] = entry.description

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

def _empty_response(identifier: str) -> dict:
    """Build empty response for errors/not-found."""
    return {
        "MediaContainer": {
            "offset": 0,
            "totalSize": 0,
            "identifier": identifier,
            "size": 0,
            "Metadata": []
        }
    }

# Then the endpoints become simple:

@app.route('/library/metadata/<rating_key>', methods=['GET'])
def get_metadata(rating_key: str):
    req = MetadataRequest(rating_key=rating_key, provider_type="movie")
    return jsonify(handle_metadata_request(req, cache))

@app.route('/tv/library/metadata/<rating_key>', methods=['GET'])
def get_metadata_tv(rating_key: str):
    req = MetadataRequest(rating_key=rating_key, provider_type="tv")
    return jsonify(handle_metadata_request(req, cache))
```

---

### 4.2 Structured Logging with Request IDs

**File:** `logging_config.py` (new)

```python
import logging
import sys
import uuid
import json
import threading
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Optional, Dict, Any

# Context variable for request ID (thread-safe, async-safe)
request_id_var: ContextVar[str] = ContextVar('request_id', default='system')

def get_request_id() -> str:
    """Get current request ID."""
    return request_id_var.get()

def set_request_id(request_id: str = None) -> str:
    """Set request ID for current context, returns the ID."""
    rid = request_id or str(uuid.uuid4())[:8]
    request_id_var.set(rid)
    return rid

class StructuredFormatter(logging.Formatter):
    """
    JSON-structured log formatter for production.

    Output format:
    {"timestamp": "...", "level": "INFO", "request_id": "abc123", "message": "...", ...}
    """

    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "request_id": get_request_id(),
            "message": record.getMessage(),
        }

        # Add exception info if present
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        # Add extra fields
        for key in ['duration_ms', 'cache_hit', 'title', 'year', 'media_type', 'status_code']:
            if hasattr(record, key):
                log_data[key] = getattr(record, key)

        return json.dumps(log_data)

class HumanFormatter(logging.Formatter):
    """Human-readable formatter for development."""

    def format(self, record: logging.LogRecord) -> str:
        request_id = get_request_id()
        prefix = f"[{request_id}] " if request_id != 'system' else ""
        return f"{record.levelname:8} {prefix}{record.getMessage()}"

def configure_logging(
    level: str = "INFO",
    structured: bool = False,
) -> None:
    """
    Configure application logging.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR)
        structured: Use JSON format (for production)
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove existing handlers
    root_logger.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)

    if structured:
        handler.setFormatter(StructuredFormatter())
    else:
        handler.setFormatter(HumanFormatter())

    root_logger.addHandler(handler)

    # Reduce noise from libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)

# Flask middleware for request ID injection
def setup_flask_request_id(app):
    """Add request ID middleware to Flask app."""
    from flask import request, g

    @app.before_request
    def inject_request_id():
        # Use X-Request-ID header if provided, else generate
        request_id = request.headers.get('X-Request-ID') or str(uuid.uuid4())[:8]
        set_request_id(request_id)
        g.request_id = request_id
        g.request_start = datetime.now(timezone.utc)

    @app.after_request
    def log_request(response):
        duration = (datetime.now(timezone.utc) - g.request_start).total_seconds() * 1000
        logger = logging.getLogger('http')
        logger.info(
            f"{request.method} {request.path} -> {response.status_code}",
            extra={
                'duration_ms': round(duration, 2),
                'status_code': response.status_code,
            }
        )
        response.headers['X-Request-ID'] = g.request_id
        return response
```

---

### 4.3 Metrics Collection

**File:** `metrics.py` (new)

```python
import time
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Optional
from contextlib import contextmanager

@dataclass
class MetricCounter:
    """Thread-safe counter."""
    value: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def increment(self, amount: int = 1) -> None:
        with self._lock:
            self.value += amount

@dataclass
class MetricHistogram:
    """Simple histogram for latency tracking."""
    count: int = 0
    total: float = 0.0
    min_value: float = float('inf')
    max_value: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def observe(self, value: float) -> None:
        with self._lock:
            self.count += 1
            self.total += value
            self.min_value = min(self.min_value, value)
            self.max_value = max(self.max_value, value)

    @property
    def avg(self) -> float:
        return self.total / self.count if self.count > 0 else 0.0

class Metrics:
    """
    Simple metrics collector.

    In production, replace with Prometheus client or StatsD.
    """

    _instance: Optional["Metrics"] = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self.counters: Dict[str, MetricCounter] = defaultdict(MetricCounter)
        self.histograms: Dict[str, MetricHistogram] = defaultdict(MetricHistogram)
        self._initialized = True

    def inc(self, name: str, amount: int = 1, labels: Dict[str, str] = None) -> None:
        """Increment a counter."""
        key = self._make_key(name, labels)
        self.counters[key].increment(amount)

    def observe(self, name: str, value: float, labels: Dict[str, str] = None) -> None:
        """Record a histogram observation."""
        key = self._make_key(name, labels)
        self.histograms[key].observe(value)

    @contextmanager
    def timer(self, name: str, labels: Dict[str, str] = None):
        """Context manager for timing operations."""
        start = time.monotonic()
        try:
            yield
        finally:
            duration = (time.monotonic() - start) * 1000  # ms
            self.observe(name, duration, labels)

    def _make_key(self, name: str, labels: Dict[str, str] = None) -> str:
        if not labels:
            return name
        label_str = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        return f"{name}{{{label_str}}}"

    def get_stats(self) -> dict:
        """Get all metrics as dict."""
        return {
            "counters": {k: v.value for k, v in self.counters.items()},
            "histograms": {
                k: {
                    "count": v.count,
                    "avg_ms": round(v.avg, 2),
                    "min_ms": round(v.min_value, 2) if v.min_value != float('inf') else 0,
                    "max_ms": round(v.max_value, 2),
                }
                for k, v in self.histograms.items()
            }
        }

    def reset(self) -> None:
        """Reset all metrics."""
        self.counters.clear()
        self.histograms.clear()

# Global instance
metrics = Metrics()

# Usage example:
# metrics.inc("vpro_searches_total", labels={"status": "found"})
# metrics.inc("cache_hits_total")
# with metrics.timer("vpro_search_duration_ms"):
#     result = search(...)
```

---

### 4.4 Deep Health Checks

**Add to `vpro_metadata_provider.py`:**

```python
@app.route('/health', methods=['GET'])
def health_check():
    """Shallow health check - just confirms app is running."""
    return jsonify({
        "status": "healthy",
        "version": PROVIDER_VERSION,
    })

@app.route('/health/ready', methods=['GET'])
def readiness_check():
    """
    Deep health check for Kubernetes readiness probe.

    Checks:
    - Cache directory writable
    - Credentials available
    - External APIs reachable (optional)
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

    # Overall status
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
    """
    Liveness probe - checks app isn't deadlocked.

    Simply returns 200 if the event loop is responding.
    """
    return jsonify({"status": "alive"}), 200
```

---

## Phase 5: Dependencies & Deployment

### 5.1 Pin Dependencies

**File:** `requirements.txt` (updated)

```
# Core dependencies - pinned for reproducibility
flask==3.0.0
requests==2.31.0
beautifulsoup4==4.12.2

# Production server
gunicorn==21.2.0

# Optional: better JSON performance
orjson==3.9.10
```

**File:** `requirements-dev.txt` (new)

```
# Development dependencies
pytest==7.4.3
pytest-cov==4.1.0
responses==0.24.1  # Mock requests
freezegun==1.2.2   # Mock time

# Linting
ruff==0.1.6
mypy==1.7.0

# Security scanning
safety==2.3.5
bandit==1.7.6
```

### 5.2 Security Scanning Config

**File:** `.github/workflows/security.yml` (new)

```yaml
name: Security Scan

on:
  push:
    branches: [main]
  pull_request:
  schedule:
    - cron: '0 0 * * 0'  # Weekly

jobs:
  security:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: |
          pip install safety bandit
          pip install -r requirements.txt

      - name: Check for vulnerable dependencies
        run: safety check -r requirements.txt

      - name: Run bandit security linter
        run: bandit -r . -x ./tests
```

---

## File Structure After Refactor

```
vpro-cinema-plex/
├── constants.py           # NEW: Enums, constants, configuration
├── credentials.py         # NEW: Thread-safe credential manager
├── cache.py               # NEW: Atomic file cache with LRU
├── http_client.py         # NEW: Rate-limited HTTP client
├── text_utils.py          # NEW: Unicode normalization, validation
├── logging_config.py      # NEW: Structured logging
├── metrics.py             # NEW: Basic metrics collection
├── vpro_cinema_scraper.py # MODIFIED: Use new modules
├── vpro_metadata_provider.py  # MODIFIED: Unified handlers
├── requirements.txt       # UPDATED: Pinned versions
├── requirements-dev.txt   # NEW: Dev dependencies
├── Dockerfile             # UPDATED: Multi-stage build
├── docker-compose.yml     # Unchanged
└── .github/
    └── workflows/
        └── security.yml   # NEW: Security scanning
```

---

## Migration Path

1. **Phase 1** can be deployed independently - new modules, no breaking changes
2. **Phase 2** requires updating imports in scraper
3. **Phase 3** requires updating metadata provider
4. **Phase 4** adds observability without changing behavior
5. **Phase 5** is config-only changes

Each phase can be a separate PR for easier review.

---

*closes laptop, puts on headphones*
