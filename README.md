# VPRO Cinema Metadata Provider for Plex

A custom metadata provider that supplies Dutch film and series descriptions
from [VPRO Cinema](https://www.vprogids.nl/cinema/) to Plex Media Server.

![Example of the Metadata Provider in action!](https://github.com/user-attachments/assets/002b61b3-c05c-4888-a1c6-c34bf38d6dd1)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Docker](https://img.shields.io/badge/docker-ready-blue.svg)](https://www.docker.com/)
[![Plex 1.40+](https://img.shields.io/badge/Plex-1.40%2B-E5A00D.svg?logo=plex&logoColor=white)](https://www.plex.tv/)

## Features

- 🇳🇱 Dutch film and series reviews/descriptions from VPRO Cinema's database
- 📺 Supports both movies and series
- 🔞 Kijkwijzer content ratings (Dutch age classification: AL, 6, 9, 12, 14, 16, 18)
- 🔍 Direct NPO POMS API access with automatic credential refresh
- 🌍 Smart title matching via TMDB — works in both directions (Translated → Original and Original → Translated)
- 💾 Persistent caching (with TTL for not-found entries)
- 🔧 Self-healing: auto-refreshes API credentials if authentication fails
- 🐳 Docker-ready with health checks
- 🔗 Combines with other providers (returns description + content rating by default)
- ⚙️ Configurable: optionally return VPRO images and/or ratings

## Background

For years I wanted to automatically pull the excellent Dutch film reviews
from [VPRO Cinema](https://www.vprogids.nl/cinema/) into Plex. I made several
attempts over the years, but without an official NPO API, I never got it to work. After getting tired of manually
copying descriptions into Plex — only to have them overwritten by the next metadata refresh — I teamed up with Claude to
build a proper solution. After some experimentation (first with scraping, then reverse-engineering the NPO's internal
POMS API), I finally got a working Plex agent! I decided to share it with the community, hoping it can help others too.

Feel free to use, fork, and contribute, but note that the API is not officially supported by NPO, so the approach is
technically a bit dodgy and may break at any time. Though it has been working excellently for me, so far!

## Prerequisites

**Required:**

- **Docker** and **Docker Compose** — [Install Docker](https://docs.docker.com/get-docker/)
- **Plex Media Server 1.40+** — Uses the new [Custom Metadata Providers API](https://developer.plex.tv/pms/)

**Recommended:**

- **TMDB API Key** — Enables smart alternate title lookup. Many films are indexed in VPRO Cinema under their original (
  often French, German, or Dutch) title rather than the English title. With a TMDB API key, the provider automatically
  discovers and tries alternate titles in both directions:
    - **English → Original**: "Downfall" → "Der Untergang" (via IMDB ID from Plex)
    - **Original → English**: "Der Untergang" → "Downfall" (via TMDB title search)

  Get your free API key at: https://www.themoviedb.org/settings/api

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/c-kick/vpro-cinema-plex.git
cd vpro-cinema-plex
cp env.example .env
```

Edit `.env` to add your TMDB API key (optional but recommended):

```bash
TMDB_API_KEY=your_tmdb_api_key_here
```

### 2. Build and run

```bash
docker-compose up -d
```

### 3. Verify

```bash
curl "http://localhost:5100/health"
curl "http://localhost:5100/test?title=Apocalypse+Now&year=1979"
```

<details>
<summary><strong>Alternative: Add to existing Docker stack (Portainer)</strong></summary>

Portainer often can't access local build contexts. Build the image on your server first:

```bash
cd /path/to/vpro-cinema-plex
docker build -t vpro-plex-provider:latest .
```

Add to your stack:

```yaml
vpro-plex-provider:
  image: vpro-plex-provider:latest
  pull_policy: never
  container_name: vpro-plex-provider
  restart: unless-stopped
  ports:
    - "5100:5100"
  environment:
    - TZ=Europe/Amsterdam
    - LOG_LEVEL=INFO
    - TMDB_API_KEY=your_tmdb_api_key_here  # Optional but recommended
    - CACHE_DIR=/app/cache
    - POMS_CACHE_FILE=/app/cache/credentials.json
    - VPRO_RETURN_SUMMARY=true        # Dutch descriptions (main feature)
    - VPRO_RETURN_CONTENT_RATING=true # Kijkwijzer age ratings
    - VPRO_RETURN_IMAGES=false        # Set to true to use VPRO posters
    - VPRO_RETURN_RATING=false        # Set to true to use VPRO ratings (1-10)
  volumes:
    - /path/to/vpro-cinema-plex/cache:/app/cache
  networks:
    - your-plex-network  # Must be on same network as Plex
```

Use container hostname for provider URLs: `http://vpro-cinema:5100/movies` and `http://vpro-cinema:5100/series`

</details>

## Plex Configuration

> **Important:** Replace `localhost` with your server's IP if Plex runs on a different host.

This provider exposes two separate endpoints (required by Plex's Custom Metadata Provider API for proper secondary
provider combining):

| Endpoint                       | Provider Name                          | Use For  |
|--------------------------------|----------------------------------------|----------|
| `http://localhost:5100/movies` | VPRO Cinema (Dutch Summaries) - Movies | Movies   |
| `http://localhost:5100/series` | VPRO Cinema (Dutch Summaries) - Series | TV Shows |

### Register the providers

1. In Plex, go to **Settings** → **Metadata Agents** → **Metadata Providers**
2. Click **+ Add Provider**, paste `http://localhost:5100/movies`, save
3. Click **+ Add Provider**, paste `http://localhost:5100/series`, save

<img width="1017" height="475" alt="image" src="https://github.com/user-attachments/assets/a0b4fbd4-ef0f-4ad7-a12b-15b724fa7faa" />

### Create the agents

**Movie Agent:**

1. Under **Metadata Agents**, click **+ Add Agent**
2. Title: "VPRO + Plex Movie"
3. Primary provider: `VPRO Cinema (Dutch Summaries) - Movies`
4. Add "Plex Movie" as additional provider (click **+**)
5. Optionally add "Plex Local Media"
6. Save

<img width="485" height="658" alt="image" src="https://github.com/user-attachments/assets/06040d4c-2d8a-41a2-95a1-1ac9a9aa25c4" />

**TV Show Agent:**

1. Click **+ Add Agent** again
2. Title: "VPRO + Plex Series"
3. Primary provider: `VPRO Cinema (Dutch Summaries) - Series`
4. Add "Plex Series" as additional provider
5. Optionally add "Plex Local Media"
6. Save

<img width="486" height="633" alt="image" src="https://github.com/user-attachments/assets/2e3e64b7-b946-4ac2-92f5-ad327b6abb56" />

### Configure your libraries

1. **Settings** → **Manage Libraries** → click `...` next to library → **Edit Library**
2. **Advanced** tab → **Agent** → select your new agent
3. Save and repeat for other libraries

### Refresh metadata

For existing content: Select items → `...` → **Refresh Metadata**

New content will automatically use the provider on scan.

## How It Works

```
┌─────────────────────────────────────────────────────────────────┐
│  Plex requests metadata for "Downfall" (2004)                   │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  1. POMS API Search (primary)                                   │
│     └─ NPO's film database with HMAC-SHA256 authentication      │
└─────────────────────────────────────────────────────────────────┘
                              │
                      No match found?
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  2. TMDB Alternate Titles (requires TMDB_API_KEY)               │
│     └─ With IMDB ID: fetch alternate titles directly            │
│     └─ Without IMDB ID: search TMDB by title+year first         │
│     └─ "Downfall" → "Der Untergang" → Found!                    │
└─────────────────────────────────────────────────────────────────┘
                              │
                      Still no match?
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  3. Web Search Fallback                                         │
│     └─ DuckDuckGo → Startpage → Scrape VPRO page                │
└─────────────────────────────────────────────────────────────────┘
```

## Testing & Debugging

### CLI search (no caching)

```bash
# Basic search
docker exec vpro-plex-provider python vpro_lookup.py "Apocalypse Now" --year 1979

# Filter by type
docker exec vpro-plex-provider python vpro_lookup.py "Adolescence" --year 2025 --type series

# With IMDB ID + verbose output
docker exec vpro-plex-provider python vpro_lookup.py "Downfall" --year 2004 --imdb tt0363163 -v
```

### HTTP endpoints (with caching)

```bash
# Test search
curl "http://localhost:5100/test?title=Le+dernier+métro&year=1980&type=film"

# Plex metadata endpoint
curl "http://localhost:5100/library/metadata/vpro-apocalypse-now-1979-tt0078788-m"

# Cache operations
curl "http://localhost:5100/cache"
curl "http://localhost:5100/cache?key=vpro-apocalypse-now-1979-tt0078788"
curl -X POST "http://localhost:5100/cache/clear"
curl -X POST "http://localhost:5100/cache/delete?key=vpro-apocalypse-now-1979-tt0078788-m"
curl -X POST "http://localhost:5100/cache/delete?pattern=apocalypse"
```

### Credential management

Credentials auto-refresh on auth failure. Manual options:

```bash
# Force refresh
docker exec vpro-plex-provider python vpro_lookup.py --refresh-credentials

# View cached credentials
docker exec vpro-plex-provider cat cache/credentials.json
```

> **Note:** `credentials.json` is created on first refresh. If missing, built-in defaults are used.

### Logs

```bash
docker-compose logs -f
```

## Environment Variables

| Variable                    | Default            | Description                                                 |
|-----------------------------|--------------------|-------------------------------------------------------------|
| `PORT`                      | 5100               | Server port                                                 |
| `LOG_LEVEL`                 | INFO               | DEBUG, INFO, WARNING, ERROR                                 |
| `CACHE_DIR`                 | ./cache            | Cache directory path                                        |
| `TMDB_API_KEY`              | *(none)*           | TMDB API key for alternate title lookup                     |
| `POMS_CACHE_FILE`           | ./credentials.json | Path to cached POMS credentials                             |
| `VPRO_RETURN_SUMMARY`       | true               | Return VPRO Dutch summary/description                       |
| `VPRO_RETURN_CONTENT_RATING`| true               | Return Kijkwijzer content rating (AL, 6, 9, 12, 14, 16, 18) |
| `VPRO_RETURN_IMAGES`        | false              | Return VPRO images (may override secondary agent)           |
| `VPRO_RETURN_RATING`        | false              | Return VPRO appreciation rating (1-10, may override secondary agent) |

## API Reference

| Endpoint                                | Method | Description                                    |
|-----------------------------------------|--------|------------------------------------------------|
| `/movies`                               | GET    | Movie provider info (type 1)                   |
| `/movies/library/metadata/<key>`        | GET    | Plex metadata lookup for movies                |
| `/movies/library/metadata/matches`      | POST   | Plex match endpoint for movies                 |
| `/movies/library/metadata/<key>/images` | GET    | Returns empty (no artwork)                     |
| `/movies/library/metadata/<key>/extras` | GET    | Returns empty (no extras)                      |
| `/series`                               | GET    | TV provider info (types 2, 3, 4)               |
| `/series/library/metadata/<key>`        | GET    | Plex metadata lookup for TV shows              |
| `/series/library/metadata/matches`      | POST   | Plex match endpoint for TV shows               |
| `/series/library/metadata/<key>/images` | GET    | Returns empty (no artwork)                     |
| `/series/library/metadata/<key>/extras` | GET    | Returns empty (no extras)                      |
| `/health`                               | GET    | Simple health check (version only)             |
| `/health/ready`                         | GET    | Detailed health with checks, cache stats, config |
| `/health/live`                          | GET    | Liveness probe (always returns ok)             |
| `/test`                                 | GET    | Test search: `?title=X&year=Y&imdb=ttZ&type=T` |
| `/cache`                                | GET    | List cached entries or view specific: `?key=X` |
| `/cache/clear`                          | POST   | Clear all cached entries (preserves credentials) |
| `/cache/delete`                         | POST   | Delete specific entries: `?key=X` or `?pattern=X` |

## File Structure

```
vpro-cinema-plex/
├── docker-compose.yml          # Docker Compose config
├── Dockerfile                  # Container definition
├── env.example                 # Environment template (copy to .env)
├── requirements.txt            # Python dependencies
│
├── vpro_metadata_provider.py   # Flask HTTP server for Plex
├── vpro_lookup.py              # Search orchestrator + CLI
├── poms_client.py              # NPO POMS API + TMDB clients
├── vpro_scraper.py             # Web search fallback + page scraper
├── models.py                   # Shared data models (VPROFilm)
│
├── cache.py                    # Disk cache with sharding
├── credentials.py              # POMS credential management
├── http_client.py              # HTTP session factory
├── text_utils.py               # Title matching utilities
├── logging_config.py           # Logging configuration
├── metrics.py                  # Simple metrics collection
├── constants.py                # Shared constants
│
├── LICENSE                     # MIT License
└── README.md                   # This file
```

## Troubleshooting

| Problem                                 | Solution                                                                                                                                                              |
|-----------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Provider not in Plex                    | Verify running: `curl http://localhost:5100/health`. Check network from Plex to provider.                                                                             |
| No Dutch descriptions                   | Test film exists: `docker exec vpro-plex-provider python vpro_lookup.py "TITLE" --year YEAR -v`. Check logs: `docker-compose logs --tail=100`. Clear cache and retry. |
| Metadata not updating after port change | Restart Plex server (known Plex bug with URL changes).                                                                                                                |
| POMS auth errors                        | Force refresh: `docker exec vpro-plex-provider python vpro_lookup.py --refresh-credentials`                                                                           |
| Film not found                          | Try original title: `"Der Untergang"` instead of `"Downfall"`. Or provide IMDB ID: `--imdb tt0363163`                                                                 |
| TMDB alternate titles not working       | Verify `"configured": true` and `"status": "ok"` under `tmdb` in `/health/ready` response.                                                                                                               |

## Updating

**Standalone docker-compose:**
```bash
git pull && docker-compose down && docker-compose build --no-cache && docker-compose up -d
```

**Portainer stack:**
```bash
cd /path/to/vpro-cinema-plex
git pull
docker build -t vpro-plex-provider:latest .
# Then redeploy the stack in Portainer
```

Verify: `curl http://localhost:5100/health`

Cache and `.env` are preserved during updates.

<details>
<summary><strong>Upgrade notes for specific versions</strong></summary>

### v3.1.0 — Breaking URL changes

| Old                        | New                            |
|----------------------------|--------------------------------|
| `http://localhost:5100/`   | `http://localhost:5100/movies` |
| `http://localhost:5100/tv` | `http://localhost:5100/series` |

Provider names also changed (added `- Movies` / `- Series` suffix).

**Migration:** Remove old providers in Plex, add new URLs, update agents, restart Plex.

### v3.0.0 — series support (two-provider architecture)

Single provider → two providers (`/movies` and `/series`). Required by Plex API for proper secondary provider combining.

**Migration:** Remove old provider, add both new URLs, create separate TV Show agent.

</details>

## Changelog

### v3.3.0
- **Kijkwijzer content ratings** — Dutch age classification (AL, 6, 9, 12, 14, 16, 18) now extracted from POMS API
- **Configurable metadata fields** — New environment variables to control what metadata is returned:
  - `VPRO_RETURN_SUMMARY` (default: true) — Dutch descriptions
  - `VPRO_RETURN_CONTENT_RATING` (default: true) — Kijkwijzer ratings
  - `VPRO_RETURN_IMAGES` (default: false) — VPRO poster images
  - `VPRO_RETURN_RATING` (default: false) — VPRO appreciation ratings (1-10)
- **Fix Match thumbnails** — Images now display in Plex's Fix Match dialog when `VPRO_RETURN_IMAGES=true`
- **Health endpoint improvements** — `/health/ready` now shows configured feature flags
- **Selective cache deletion** — New `/cache/delete` endpoint for targeted cache management

### v3.2.0
- Added debug logging for troubleshooting
- Docker environment variable passthrough improvements

### v3.1.0
- Breaking URL changes: `/` → `/movies`, `/tv` → `/series`
- Provider name suffix changes (`- Movies` / `- Series`)

### v3.0.0
- Two-provider architecture for proper Plex secondary agent combining
- Added series/TV show support

## Limitations

- **POMS API is undocumented** — Not officially supported by NPO; may change without notice
- **Not all content covered** — Only films/series reviewed by VPRO Cinema
- **Artwork optional** — Disabled by default; enable `VPRO_RETURN_IMAGES` or use Plex Movie fallback
- **Web search fallback** — May hit rate limits or CAPTCHAs

## License

MIT — Do whatever you want with it.

## Credits

- [VPRO Cinema](https://vprogids.nl/cinema) for the Dutch film reviews
- [TMDB](https://www.themoviedb.org/) for alternate title data
- Klaas (c_kick/hnldesign) — Original idea and development
- Claude (Anthropic) — Implementation assistance
