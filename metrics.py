"""
Simple metrics collection for observability.

Provides thread-safe counters and histograms for tracking
application performance and behavior.

In production, consider replacing with Prometheus client or StatsD.
"""

import time
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Any, Optional
from contextlib import contextmanager


@dataclass
class MetricCounter:
    """Thread-safe counter metric."""
    _value: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def value(self) -> int:
        with self._lock:
            return self._value

    def increment(self, amount: int = 1) -> None:
        """Increment counter by amount."""
        with self._lock:
            self._value += amount

    def reset(self) -> None:
        """Reset counter to zero."""
        with self._lock:
            self._value = 0


@dataclass
class MetricHistogram:
    """
    Simple histogram for latency/duration tracking.

    Tracks count, total, min, max for computing averages.
    """
    _count: int = 0
    _total: float = 0.0
    _min: float = float('inf')
    _max: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def observe(self, value: float) -> None:
        """Record an observation."""
        with self._lock:
            self._count += 1
            self._total += value
            self._min = min(self._min, value)
            self._max = max(self._max, value)

    @property
    def count(self) -> int:
        with self._lock:
            return self._count

    @property
    def avg(self) -> float:
        with self._lock:
            return self._total / self._count if self._count > 0 else 0.0

    @property
    def min(self) -> float:
        with self._lock:
            return self._min if self._min != float('inf') else 0.0

    @property
    def max(self) -> float:
        with self._lock:
            return self._max

    def stats(self) -> Dict[str, float]:
        """Get all stats as dict."""
        with self._lock:
            return {
                "count": self._count,
                "avg": round(self._total / self._count, 2) if self._count > 0 else 0.0,
                "min": round(self._min, 2) if self._min != float('inf') else 0.0,
                "max": round(self._max, 2),
            }

    def reset(self) -> None:
        """Reset histogram."""
        with self._lock:
            self._count = 0
            self._total = 0.0
            self._min = float('inf')
            self._max = 0.0


class Metrics:
    """
    Thread-safe metrics collector singleton.

    Usage:
        metrics.inc("requests_total", labels={"endpoint": "/search"})
        metrics.inc("cache_hits")

        with metrics.timer("search_duration_ms"):
            result = do_search()

        stats = metrics.get_stats()
    """

    _instance: Optional["Metrics"] = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._initialized = False
                    cls._instance = instance
        return cls._instance

    def __init__(self):
        if getattr(self, '_initialized', False):
            return

        self._counters: Dict[str, MetricCounter] = defaultdict(MetricCounter)
        self._histograms: Dict[str, MetricHistogram] = defaultdict(MetricHistogram)
        self._initialized = True

    def _make_key(self, name: str, labels: Optional[Dict[str, str]] = None) -> str:
        """Create metric key with optional labels."""
        if not labels:
            return name
        label_str = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        return f"{name}{{{label_str}}}"

    def inc(self, name: str, amount: int = 1, labels: Optional[Dict[str, str]] = None) -> None:
        """
        Increment a counter.

        Args:
            name: Counter name
            amount: Amount to increment by (default 1)
            labels: Optional labels dict
        """
        key = self._make_key(name, labels)
        self._counters[key].increment(amount)

    def observe(self, name: str, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        """
        Record a histogram observation.

        Args:
            name: Histogram name
            value: Value to record
            labels: Optional labels dict
        """
        key = self._make_key(name, labels)
        self._histograms[key].observe(value)

    @contextmanager
    def timer(self, name: str, labels: Optional[Dict[str, str]] = None):
        """
        Context manager for timing operations.

        Args:
            name: Histogram name for the timing
            labels: Optional labels dict

        Usage:
            with metrics.timer("search_duration_ms"):
                result = do_search()
        """
        start = time.monotonic()
        try:
            yield
        finally:
            duration_ms = (time.monotonic() - start) * 1000
            self.observe(name, duration_ms, labels)

    def get_stats(self) -> Dict[str, Any]:
        """
        Get all metrics as a dictionary.

        Returns:
            Dict with counters and histograms
        """
        return {
            "counters": {k: v.value for k, v in self._counters.items()},
            "histograms": {k: v.stats() for k, v in self._histograms.items()},
        }

    def get_counter(self, name: str, labels: Optional[Dict[str, str]] = None) -> int:
        """Get current value of a counter."""
        key = self._make_key(name, labels)
        return self._counters[key].value

    def reset(self) -> None:
        """Reset all metrics."""
        for counter in self._counters.values():
            counter.reset()
        for histogram in self._histograms.values():
            histogram.reset()


# Global instance
metrics = Metrics()
