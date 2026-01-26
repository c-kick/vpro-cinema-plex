"""
Thread-safe credential manager for POMS API authentication.

Provides credential management with:
- Singleton pattern with thread-safe initialization
- Cooldown to prevent hammering on failures
- Atomic file operations for cache persistence
- Graceful fallback to defaults

Note: The credential extraction from vprogids.nl is currently broken
(the source URL returns 404 after the migration to cinema.nl). However,
the default credentials still work with the POMS API. The refresh
mechanism is retained for potential future use.
"""

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

import requests
from bs4 import BeautifulSoup

from constants import (
    VPRO_CREDENTIAL_URL,
    DEFAULT_POMS_API_KEY,
    DEFAULT_POMS_API_SECRET,
    CREDENTIAL_REFRESH_COOLDOWN,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Credentials:
    """
    Immutable credential holder.

    Using frozen=True ensures credentials can't be accidentally modified
    after creation, which is important for thread safety.
    """
    api_key: str
    api_secret: str
    fetched_at: datetime
    source: str


class CredentialManager:
    """
    Thread-safe credential manager with automatic refresh.

    Uses a singleton pattern to ensure only one instance manages credentials.
    Implements cooldown to prevent excessive refresh attempts on failures.

    Credential sources (in priority order):
        1. Cached credentials from file
        2. Fresh extraction from vprogids.nl (currently broken - returns 404)
        3. Hardcoded fallback defaults (these still work with POMS API)
    """

    # Patterns to find credentials in JavaScript (most specific first)
    CREDENTIAL_PATTERNS = [
        (r'vpronlApiKey\s*[=:]\s*["\']([a-z0-9]{8,15})["\']',
         r'vpronlSecret\s*[=:]\s*["\']([a-z0-9]{8,15})["\']'),
        (r'"apiKey"\s*:\s*"([a-z0-9]{8,15})"',
         r'"(?:apiSecret|secret)"\s*:\s*"([a-z0-9]{8,15})"'),
        (r'apiKey\s*[=:]\s*["\']([a-z0-9]{8,15})["\']',
         r'(?:apiSecret|secret)\s*[=:]\s*["\']([a-z0-9]{8,15})["\']'),
    ]

    _instance: Optional["CredentialManager"] = None
    _instance_lock = threading.Lock()

    def __new__(cls, cache_file: str = None):
        """Singleton pattern with thread-safe initialization."""
        if cls._instance is None:
            with cls._instance_lock:
                # Double-check locking pattern
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._initialized = False
                    cls._instance = instance
        return cls._instance

    def __init__(self, cache_file: str = None):
        """
        Initialize the credential manager.

        Args:
            cache_file: Path to credential cache file. Only used on first init.
        """
        if getattr(self, '_initialized', False):
            return

        self._lock = threading.RLock()  # Reentrant for nested calls
        self._credentials: Optional[Credentials] = None
        self._cache_file = Path(
            cache_file or
            os.environ.get("POMS_CACHE_FILE", "./cache/credentials.json")
        )
        self._refresh_in_progress = False
        self._last_refresh_attempt = 0.0

        self._load_cached()
        self._initialized = True

    def _load_cached(self) -> bool:
        """
        Load credentials from cache file.

        Called during init, assumes no concurrent access yet.

        Returns:
            True if credentials were loaded successfully
        """
        if not self._cache_file.exists():
            return False

        try:
            data = json.loads(self._cache_file.read_text())

            fetched_at_str = data.get("fetched_at", "2000-01-01T00:00:00+00:00")
            try:
                fetched_at = datetime.fromisoformat(fetched_at_str.replace('Z', '+00:00'))
            except ValueError:
                fetched_at = datetime.now(timezone.utc)

            self._credentials = Credentials(
                api_key=data["api_key"],
                api_secret=data["api_secret"],
                fetched_at=fetched_at,
                source=data.get("source", "cache")
            )
            logger.debug(f"Loaded cached credentials from {self._cache_file}")
            return True

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning(f"Failed to load cached credentials: {e}")
            return False

    def _save_cache(self, creds: Credentials) -> None:
        """
        Save credentials to cache file atomically.

        Uses temp file + rename pattern to prevent corruption.
        """
        try:
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)

            # Write to temp file first
            temp_file = self._cache_file.with_suffix('.tmp')
            temp_file.write_text(json.dumps({
                "api_key": creds.api_key,
                "api_secret": creds.api_secret,
                "fetched_at": creds.fetched_at.isoformat(),
                "source": creds.source,
            }, indent=2))

            # Atomic rename (on POSIX systems)
            temp_file.replace(self._cache_file)
            logger.debug(f"Saved credentials to {self._cache_file}")

        except OSError as e:
            logger.warning(f"Failed to save credentials cache: {e}")

    @property
    def api_key(self) -> str:
        """Get current API key, falling back to default."""
        with self._lock:
            return self._credentials.api_key if self._credentials else DEFAULT_POMS_API_KEY

    @property
    def api_secret(self) -> str:
        """Get current API secret, falling back to default."""
        with self._lock:
            return self._credentials.api_secret if self._credentials else DEFAULT_POMS_API_SECRET

    def get_credentials(self) -> Tuple[str, str]:
        """
        Get current credentials as tuple.

        Returns:
            Tuple of (api_key, api_secret)
        """
        with self._lock:
            if self._credentials:
                return (self._credentials.api_key, self._credentials.api_secret)
            return (DEFAULT_POMS_API_KEY, DEFAULT_POMS_API_SECRET)

    def invalidate_and_refresh(self) -> bool:
        """
        Invalidate current credentials and attempt to fetch fresh ones.

        Thread-safe with cooldown to prevent hammering the source.

        Returns:
            True if refresh succeeded and new credentials are available
        """
        with self._lock:
            now = time.monotonic()

            # Check cooldown
            if now - self._last_refresh_attempt < CREDENTIAL_REFRESH_COOLDOWN:
                logger.debug("Credential refresh on cooldown, skipping")
                return self._credentials is not None

            # Check if another thread is already refreshing
            if self._refresh_in_progress:
                logger.debug("Credential refresh already in progress")
                return self._credentials is not None

            self._refresh_in_progress = True
            self._last_refresh_attempt = now

        try:
            # Perform fetch outside the lock to avoid blocking other threads
            new_creds = self._fetch_fresh_credentials()

            with self._lock:
                if new_creds:
                    self._credentials = new_creds
                    self._save_cache(new_creds)
                    return True
                return self._credentials is not None

        finally:
            with self._lock:
                self._refresh_in_progress = False

    def _fetch_fresh_credentials(self) -> Optional[Credentials]:
        """
        Attempt to fetch fresh credentials from vprogids.nl.

        Note: This method is currently broken as vprogids.nl/cinema has
        migrated to cinema.nl and the credential URL returns 404. The
        default credentials still work with the POMS API.

        Scrapes the search page and linked JavaScript files
        for API credentials.

        Returns:
            New Credentials object or None if extraction failed
        """
        logger.info("Fetching fresh API credentials from vprogids.nl...")

        try:
            session = requests.Session()
            session.headers.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'text/html,application/xhtml+xml',
                'Accept-Language': 'nl-NL,nl;q=0.9',
            })

            response = session.get(VPRO_CREDENTIAL_URL, timeout=15)
            response.raise_for_status()

            # Try to extract from main page
            api_key, api_secret = self._extract_credentials(response.text)

            # Also check linked JavaScript files if not found
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
                    source=VPRO_CREDENTIAL_URL
                )

            logger.warning("Could not extract credentials from vprogids.nl")
            return None

        except requests.RequestException as e:
            logger.error(f"Failed to fetch credentials: {e}")
            return None

    def _extract_credentials(self, text: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Extract API credentials from page text using regex patterns.

        Args:
            text: HTML or JavaScript text to search

        Returns:
            Tuple of (api_key, api_secret), either may be None
        """
        api_key = None
        api_secret = None

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

    def delete_cache(self) -> None:
        """Delete the cached credentials file."""
        with self._lock:
            self._credentials = None
            try:
                self._cache_file.unlink(missing_ok=True)
            except OSError:
                pass


def get_credential_manager() -> CredentialManager:
    """
    Get the singleton credential manager instance.

    Returns:
        The global CredentialManager instance
    """
    return CredentialManager()
