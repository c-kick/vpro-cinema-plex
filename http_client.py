"""
HTTP client with connection pooling, retries, and rate limiting.

Provides a robust HTTP session that:
- Pools connections for better performance
- Retries failed requests with exponential backoff
- Rate limits requests per host to avoid abuse
- Handles timeouts gracefully
"""

import time
import threading
import logging
from dataclasses import dataclass
from typing import Dict, Optional
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from constants import (
    MAX_RETRIES,
    RETRY_BACKOFF_BASE,
    RATE_LIMIT_POMS,
    RATE_LIMIT_TMDB,
    RATE_LIMIT_WEB_SEARCH,
    RATE_LIMIT_VPRO,
    RATE_LIMIT_CINEMA,
)

logger = logging.getLogger(__name__)


@dataclass
class RateLimitConfig:
    """Configuration for rate limiting."""
    requests_per_second: float
    burst_size: int = 1


class TokenBucketRateLimiter:
    """
    Thread-safe token bucket rate limiter.

    Allows bursting up to `burst_size` requests, then enforces
    the `requests_per_second` rate.

    This implementation is non-blocking for the burst, then
    blocks until tokens are available.
    """

    def __init__(self, requests_per_second: float, burst_size: int = 1):
        """
        Initialize rate limiter.

        Args:
            requests_per_second: Maximum sustained request rate
            burst_size: Maximum burst of requests allowed
        """
        self.rate = requests_per_second
        self.burst_size = burst_size
        self.tokens = float(burst_size)
        self.last_update = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, timeout: float = 30.0) -> bool:
        """
        Acquire a token, blocking until available or timeout.

        Args:
            timeout: Maximum time to wait for a token

        Returns:
            True if token acquired, False if timeout
        """
        deadline = time.monotonic() + timeout

        while True:
            with self._lock:
                now = time.monotonic()

                # Refill tokens based on elapsed time
                elapsed = now - self.last_update
                self.tokens = min(
                    self.burst_size,
                    self.tokens + elapsed * self.rate
                )
                self.last_update = now

                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return True

                # Calculate wait time for next token
                wait_time = (1.0 - self.tokens) / self.rate

            # Check timeout
            if time.monotonic() + wait_time > deadline:
                logger.warning("Rate limit timeout exceeded")
                return False

            # Sleep in small increments to allow for cancellation
            time.sleep(min(wait_time, 0.1))


class RateLimitedSession:
    """
    Requests session wrapper with rate limiting, retries, and pooling.

    Features:
    - Connection pooling via HTTPAdapter
    - Automatic retries with exponential backoff
    - Per-host rate limiting
    - Configurable timeouts

    Usage:
        with RateLimitedSession() as session:
            response = session.get("https://api.example.com/data")
    """

    # Class-level rate limiters shared across all instances
    _rate_limiters: Dict[str, TokenBucketRateLimiter] = {}
    _rate_limiter_lock = threading.Lock()

    # Default rate limits by domain pattern
    DEFAULT_RATE_LIMITS: Dict[str, RateLimitConfig] = {
        "poms.omroep.nl": RateLimitConfig(RATE_LIMIT_POMS, burst_size=3),
        "api.themoviedb.org": RateLimitConfig(RATE_LIMIT_TMDB, burst_size=5),
        "duckduckgo.com": RateLimitConfig(RATE_LIMIT_WEB_SEARCH, burst_size=2),
        "startpage.com": RateLimitConfig(RATE_LIMIT_WEB_SEARCH, burst_size=1),
        "vprogids.nl": RateLimitConfig(RATE_LIMIT_VPRO, burst_size=3),
        "cinema.nl": RateLimitConfig(RATE_LIMIT_CINEMA, burst_size=3),
    }

    def __init__(
        self,
        timeout: float = 30.0,
        max_retries: int = MAX_RETRIES,
        backoff_factor: float = RETRY_BACKOFF_BASE,
        user_agent: str = None,
    ):
        """
        Initialize rate-limited session.

        Args:
            timeout: Default request timeout in seconds
            max_retries: Maximum number of retry attempts
            backoff_factor: Base for exponential backoff
            user_agent: Custom user agent string
        """
        self.timeout = timeout
        self.session = requests.Session()

        # Configure retry strategy
        retry_strategy = Retry(
            total=max_retries,
            backoff_factor=backoff_factor,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
            raise_on_status=False,
        )

        # Configure connection pooling
        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=10,
            pool_maxsize=20,
        )
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        # Set default headers
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
        """
        Get or create rate limiter for the URL's host.

        Args:
            url: URL to get rate limiter for

        Returns:
            Rate limiter for the host, or None if no limit configured
        """
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
        """
        Block until rate limit allows request.

        Args:
            url: URL being requested

        Raises:
            requests.exceptions.Timeout: If rate limit wait times out
        """
        limiter = self._get_rate_limiter(url)
        if limiter:
            if not limiter.acquire(timeout=60.0):
                raise requests.exceptions.Timeout(
                    f"Rate limit timeout for {urlparse(url).netloc}"
                )

    def get(self, url: str, **kwargs) -> requests.Response:
        """
        Make a rate-limited GET request.

        Args:
            url: URL to request
            **kwargs: Additional arguments passed to requests.get

        Returns:
            Response object
        """
        self._apply_rate_limit(url)
        kwargs.setdefault("timeout", self.timeout)
        return self.session.get(url, **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        """
        Make a rate-limited POST request.

        Args:
            url: URL to request
            **kwargs: Additional arguments passed to requests.post

        Returns:
            Response object
        """
        self._apply_rate_limit(url)
        kwargs.setdefault("timeout", self.timeout)
        return self.session.post(url, **kwargs)

    def close(self) -> None:
        """Close the session and release resources."""
        self.session.close()

    def __enter__(self) -> "RateLimitedSession":
        return self

    def __exit__(self, *args) -> None:
        self.close()


def create_session(
    timeout: float = 30.0,
    user_agent: str = None,
) -> RateLimitedSession:
    """
    Create a configured rate-limited session.

    Convenience function for creating sessions with default settings.

    Args:
        timeout: Request timeout in seconds
        user_agent: Optional custom user agent

    Returns:
        Configured RateLimitedSession
    """
    return RateLimitedSession(timeout=timeout, user_agent=user_agent)


class SessionAwareComponent:
    """
    Mixin for components that optionally manage HTTP sessions.

    Provides standardized session ownership tracking and cleanup.
    Use this mixin to avoid duplicating the session management pattern
    across multiple classes.

    Usage:
        class MyClient(SessionAwareComponent):
            def __init__(self, session=None):
                self.init_session(session, timeout=15.0)

            def do_something(self):
                response = self.session.get(...)
    """

    session: RateLimitedSession
    _owns_session: bool

    def init_session(
        self,
        session: RateLimitedSession = None,
        timeout: float = 30.0,
    ) -> None:
        """
        Initialize session with ownership tracking.

        Args:
            session: Optional existing session to use
            timeout: Timeout for new session if created
        """
        self.session = session or create_session(timeout=timeout)
        self._owns_session = session is None

    def close(self) -> None:
        """Close session if we own it."""
        if self._owns_session:
            self.session.close()
