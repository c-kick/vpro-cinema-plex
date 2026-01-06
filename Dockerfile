FROM python:3.11-slim

LABEL maintainer="Klaas (c_kick/hnldesign)"
LABEL description="VPRO Cinema Metadata Provider for Plex"
LABEL version="2.5.0"

WORKDIR /app

# Install dependencies
RUN pip install --no-cache-dir flask requests beautifulsoup4

# Copy application files
COPY vpro_cinema_scraper.py vpro_metadata_provider.py ./

# Environment defaults
ENV PORT=5100
ENV LOG_LEVEL=INFO
ENV CACHE_DIR=/app/cache
ENV VPRO_CREDENTIALS_FILE=/app/cache/credentials.json
# TMDB_API_KEY should be set via docker-compose or .env

EXPOSE 5100

# Cache directory for metadata and credentials
VOLUME ["/app/cache"]

# Healthcheck
HEALTHCHECK --interval=60s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5100/health')" || exit 1

CMD ["python", "vpro_metadata_provider.py"]
