FROM python:3.11-slim

LABEL maintainer="Klaas (c_kick/hnldesign)"
LABEL description="VPRO Cinema Metadata Provider for Plex"
LABEL version="3.1.3"

WORKDIR /app

# Install dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy all application modules
COPY constants.py ./
COPY credentials.py ./
COPY cache.py ./
COPY http_client.py ./
COPY text_utils.py ./
COPY logging_config.py ./
COPY metrics.py ./
COPY models.py ./
COPY vpro_scraper.py ./
COPY poms_client.py ./
COPY vpro_lookup.py ./
COPY vpro_metadata_provider.py ./

# Environment defaults
ENV PORT=5100
ENV LOG_LEVEL=INFO
ENV CACHE_DIR=/app/cache
ENV POMS_CACHE_FILE=/app/cache/credentials.json
# TMDB_API_KEY should be set via docker-compose or .env

EXPOSE 5100

# Cache directory for metadata and credentials
VOLUME ["/app/cache"]

# Healthcheck
HEALTHCHECK --interval=60s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5100/health')" || exit 1

CMD ["python", "vpro_metadata_provider.py"]
