"""
Structured logging configuration with request ID tracking.

Provides:
- Request ID propagation via context variables
- Structured JSON logging for production
- Human-readable logging for development
- Flask middleware for automatic request tracking
"""

import logging
import sys
import uuid
import json
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Optional

# Context variable for request ID (thread-safe and async-safe)
request_id_var: ContextVar[str] = ContextVar('request_id', default='system')


def get_request_id() -> str:
    """
    Get the current request ID.

    Returns:
        Current request ID or 'system' if not in a request context
    """
    return request_id_var.get()


def set_request_id(request_id: str = None) -> str:
    """
    Set the request ID for the current context.

    Args:
        request_id: Request ID to set. If None, generates a new one.

    Returns:
        The request ID that was set
    """
    rid = request_id or str(uuid.uuid4())[:8]
    request_id_var.set(rid)
    return rid


class StructuredFormatter(logging.Formatter):
    """
    JSON structured log formatter for production.

    Output format:
    {"timestamp": "...", "level": "INFO", "request_id": "abc123", "message": "..."}

    Includes additional context fields when available.
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

        # Add extra fields commonly used
        extra_fields = [
            'duration_ms', 'cache_hit', 'title', 'year',
            'media_type', 'status_code', 'endpoint', 'method'
        ]
        for key in extra_fields:
            if hasattr(record, key):
                log_data[key] = getattr(record, key)

        return json.dumps(log_data, ensure_ascii=False)


class HumanFormatter(logging.Formatter):
    """
    Human-readable log formatter for development.

    Output format:
    INFO     [abc123] Message here

    Includes request ID prefix when in a request context.
    """

    COLORS = {
        'DEBUG': '\033[36m',     # Cyan
        'INFO': '\033[32m',      # Green
        'WARNING': '\033[33m',   # Yellow
        'ERROR': '\033[31m',     # Red
        'CRITICAL': '\033[35m',  # Magenta
    }
    RESET = '\033[0m'

    def __init__(self, use_colors: bool = True):
        super().__init__()
        self.use_colors = use_colors and sys.stderr.isatty()

    def format(self, record: logging.LogRecord) -> str:
        request_id = get_request_id()
        prefix = f"[{request_id}] " if request_id != 'system' else ""

        level = record.levelname
        if self.use_colors:
            color = self.COLORS.get(level, '')
            level = f"{color}{level:8}{self.RESET}"
        else:
            level = f"{level:8}"

        message = record.getMessage()

        # Add exception if present
        if record.exc_info:
            message += f"\n{self.formatException(record.exc_info)}"

        return f"{level} {prefix}{message}"


def configure_logging(
    level: str = "INFO",
    structured: bool = False,
    use_colors: bool = True,
) -> None:
    """
    Configure application logging.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR)
        structured: Use JSON format for production
        use_colors: Use colored output (only for human format)
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove existing handlers
    root_logger.handlers.clear()

    # Create handler
    handler = logging.StreamHandler(sys.stdout)

    if structured:
        handler.setFormatter(StructuredFormatter())
    else:
        handler.setFormatter(HumanFormatter(use_colors=use_colors))

    root_logger.addHandler(handler)

    # Reduce noise from third-party libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def setup_flask_request_id(app) -> None:
    """
    Add request ID middleware to a Flask app.

    This middleware:
    - Extracts or generates request ID for each request
    - Stores request start time for duration logging
    - Logs request completion with duration
    - Adds X-Request-ID header to responses

    Args:
        app: Flask application instance
    """
    from flask import request, g
    from datetime import datetime, timezone

    @app.before_request
    def inject_request_id():
        """Inject request ID into context before each request."""
        # Use X-Request-ID header if provided, otherwise generate
        request_id = request.headers.get('X-Request-ID') or str(uuid.uuid4())[:8]
        set_request_id(request_id)
        g.request_id = request_id
        g.request_start = datetime.now(timezone.utc)

    @app.after_request
    def log_request(response):
        """Log request completion and add request ID header."""
        duration_ms = (
            datetime.now(timezone.utc) - g.request_start
        ).total_seconds() * 1000

        logger = logging.getLogger('http')
        logger.info(
            f"{request.method} {request.path} -> {response.status_code} ({duration_ms:.0f}ms)",
            extra={
                'method': request.method,
                'endpoint': request.path,
                'status_code': response.status_code,
                'duration_ms': round(duration_ms, 2),
            }
        )

        response.headers['X-Request-ID'] = g.request_id
        return response


class RequestContextAdapter(logging.LoggerAdapter):
    """
    Logger adapter that automatically includes request context.

    Usage:
        logger = RequestContextAdapter(logging.getLogger(__name__))
        logger.info("Processing item", extra={"item_id": 123})
    """

    def process(self, msg, kwargs):
        extra = kwargs.get('extra', {})
        extra['request_id'] = get_request_id()
        kwargs['extra'] = extra
        return msg, kwargs
