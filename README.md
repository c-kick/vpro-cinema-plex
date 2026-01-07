# VPRO Cinema Metadata Provider for Plex

A custom metadata provider that supplies Dutch film and TV series descriptions from [VPRO Cinema](https://www.vprogids.nl/cinema/) to Plex Media Server.

![Example of the Metadata Provider in action!](https://github.com/user-attachments/assets/002b61b3-c05c-4888-a1c6-c34bf38d6dd1)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Docker](https://img.shields.io/badge/docker-ready-blue.svg)](https://www.docker.com/)
[![Plex 1.40+](https://img.shields.io/badge/Plex-1.40%2B-E5A00D.svg?logo=plex&logoColor=white)](https://www.plex.tv/)

## Features

- 🇳🇱 Dutch film and TV series reviews/descriptions from VPRO Cinema's database
- 📺 Supports both movies and TV series
- 🔍 Direct NPO POMS API access with automatic credential refresh
- 🌍 Smart title matching via TMDB — works in both directions (Translated → Original and Original → Translated)
- 💾 Persistent caching with TTL for not-found entries
- 🔧 Self-healing: auto-refreshes API credentials if authentication fails
- 🐳 Docker-ready with health checks
- 🔗 Combines with other providers (as it only returns the `description` metadata)

## Background

For years I wanted to automatically pull the excellent Dutch film reviews from [VPRO Cinema](https://www.vprogids.nl/cinema/) into Plex. I made several
attempts over the years, but without an official NPO API, I never got it to work. After getting tired of manually
copying descriptions into Plex — only to have them overwritten by the next metadata refresh — I teamed up with Claude to
build a proper solution. After some experimentation (first with scraping, then reverse-engineering the NPO's internal
POMS API), I finally got a working Plex agent! I decided to share it with the community, hoping it can help others too.

Feel free to use, fork, and contribute, but note that the API is not officially supported by NPO, so the approach is 
technically a bit dodgy and may break at any time. Though it has been working excellently for me, so far!

## Prerequisites

### Required

- **Docker** and **Docker Compose** — [Install Docker](https://docs.docker.com/get-docker/)
- **Plex Media Server 1.40+** — Uses the new [Custom Metadata Providers API](https://developer.plex.tv/pms/)

### Recommended

- **TMDB API Key** — Enables smart alternate title lookup

  Many films are indexed in VPRO Cinema under their original (often French, German, or Dutch) title rather than the
  English title. With a TMDB API key, the provider automatically discovers and tries alternate titles in both directions:

  - **English → Original**: "Downfall" → "Der Untergang" (via IMDB ID from Plex)
  - **Original → English**: "Der Untergang" → "Downfall" (via TMDB title search)

  Get your free API key at: https://www.themoviedb.org/settings/api

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/c-kick/vpro-cinema-plex.git
cd vpro-cinema-plex

# Optionally, you can copy and edit environment file if you want to change the host port, or the log_level
cp env.example .env
```

#### Optionally add TMDB API key for multi-language title searches

Edit the `.env` file (`cp env.example .env` first, if it's not there yet) and add your TMDB API key:

```bash
TMDB_API_KEY=your_tmdb_api_key_here
```

### 2. Build and run

```bash
docker-compose up -d
```

<details>
<summary><strong>Alternative: Add to existing Docker stack (Portainer)</strong></summary>

If you're running Plex in an existing Docker Compose stack (e.g., via Portainer), you can integrate the provider directly.

#### 1. Build the image manually

Portainer often can't access local build contexts. Build the image on your server first:

```bash
cd /path/to/vpro-cinema-plex
docker build -t vpro-cinema:latest .
```

#### 2. Add the service to your stack

```yaml
vpro-cinema:
  image: vpro-cinema:latest
  pull_policy: never  # Use local image, don't pull from Docker Hub
  container_name: vpro-cinema
  restart: unless-stopped
  ports:
    - "5100:5100"  # Optional: expose for external testing
  environment:
    - TZ=Europe/Amsterdam
    - TMDB_API_KEY=${TMDB_API_KEY:-}
    - LOG_LEVEL=INFO
  volumes:
    - /path/to/vpro-cinema-plex/cache:/app/cache
  networks:
    - your-plex-network  # Must be on same network as Plex
```

#### 3. Configure Plex

Use the container hostname for provider URLs:
- Movies: `http://vpro-cinema:5100/movies`
- Series: `http://vpro-cinema:5100/series`

#### Rebuilding after updates

```bash
cd /path/to/vpro-cinema-plex
git pull
docker build -t vpro-cinema:latest .
# Then redeploy the stack in Portainer
```

</details>

### 3. Verify it's running

```bash
# Health check
curl "http://localhost:5100/health"

# Test a search
curl "http://localhost:5100/test?title=Apocalypse+Now&year=1979"

# Check logs
docker-compose logs -f
```

### 4. Register with Plex

>**Important:** The provider URL must be reachable from your Plex server. Replace `localhost` in the examples below
with the IP of the server you have the provider running on, if not on the same server as Plex itself.

This provider exposes **two separate endpoints** — one for movies and one for TV shows. This is required by Plex's
Custom Metadata Provider API to allow combining with secondary providers like "Plex Movie" and "Plex Series".

| Endpoint | Provider Name | Use For |
|----------|---------------|---------|
| `http://localhost:5100/movies` | VPRO Cinema (Dutch Summaries) - Movies | Movies |
| `http://localhost:5100/series` | VPRO Cinema (Dutch Summaries) - Series | TV Shows |

1. Log into the Plex web interface
2. Go to **Settings** → **Metadata Agents** (not the legacy one!)
3. Under *Metadata Providers* click **+ Add Provider**
4. Paste the **movie provider URL**: `http://localhost:5100/movies` and click **Save**
5. Click **+ Add Provider** again
6. Paste the **TV provider URL**: `http://localhost:5100/series` and click **Save**

You should now see both "VPRO Cinema (Dutch Summaries) - Movies" and "VPRO Cinema (Dutch Summaries) - Series" in the providers list.

<img width="1017" height="475" alt="image" src="https://github.com/user-attachments/assets/a0b4fbd4-ef0f-4ad7-a12b-15b724fa7faa" />

#### Create a Movie Agent

7. Under *Metadata Agents* click **+ Add Agent**
8. Give the agent a title, example: "VPRO + Plex Movie"
9. Select `VPRO Cinema (Dutch Summaries) - Movies` as the primary metadata provider
10. A section 'additional providers' appears, pick "Plex Movie" and click the **+** button
11. Optionally add "Plex Local Media" from the dropdown (don't forget to click the **+** button)
12. Click **Save**

<img width="485" height="658" alt="image" src="https://github.com/user-attachments/assets/06040d4c-2d8a-41a2-95a1-1ac9a9aa25c4" />

#### Create a TV Show Agent

13. Under *Metadata Agents* click **+ Add Agent** again
14. Give the agent a title, example: "VPRO + Plex Series"
15. Select `VPRO Cinema (Dutch Summaries) - Series` as the primary metadata provider
16. Pick "Plex Series" and click the **+** button
17. Optionally add "Plex Local Media" (don't forget to click the **+** button)
18. Click **Save**

<img width="486" height="633" alt="image" src="https://github.com/user-attachments/assets/2e3e64b7-b946-4ac2-92f5-ad327b6abb56" />

Done! The agents are now configured to first search for Dutch summaries on VPRO Cinema, falling back to Plex
Movie/Series for remaining metadata (artwork, cast, etc.).

### 5. Configure your libraries

1. Go to **Plex Settings** → **Manage Libraries**
2. Click the `...` next to your movie library → **Edit Library**
3. Go to **Advanced** tab
4. Under **Agent**, select the movie agent you created ("VPRO + Plex Movie")
5. Click **Save Changes**
6. Repeat for your TV show library, selecting the TV show agent ("VPRO + Plex Series")

### 6. Refresh metadata

For existing content: Select items → `...` → **Refresh Metadata**

New movies and TV shows will automatically use the provider on scan.

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

### Test searches directly

> **Note:** The CLI tool (`vpro_lookup.py`) does not cache results — it only searches and returns data. Caching is
> handled by the HTTP provider (`vpro_metadata_provider.py`). To test with caching, use the HTTP `/library/metadata`
> endpoints below.

```bash
# Basic search (searches both films and series by default)
docker exec vpro-plex-provider python vpro_lookup.py "Apocalypse Now" --year 1979

# Search for TV series only
docker exec vpro-plex-provider python vpro_lookup.py "Adolescence" --year 2025 --type series

# Search for films only
docker exec vpro-plex-provider python vpro_lookup.py "Downfall" --year 2004 --type film

# With IMDB ID (enables TMDB alternate title lookup) in verbose mode (showing full search flow)
docker exec vpro-plex-provider python vpro_lookup.py "Downfall" --year 2004 --imdb tt0363163 -v
```

Example output:

```
Searching VPRO Cinema for: Downfall (2004)
------------------------------------------------------------
Searching VPRO: 'Downfall' (2004) [tt0363163]
POMS: Rejecting title match 'Downfall' (1964) - year diff 40
No POMS match for 'Downfall' - fetching alternate titles by IMDB...
TMDB alternate titles for tt0363163: ['Der Untergang', 'A Queda!', '몰락', ...]
Trying alternate title: 'Der Untergang'
POMS: Exact match - Der Untergang (2004)
Found via alternate title 'Der Untergang': Der Untergang

Found (Film): Der Untergang
  Year: 2004
  Type: film
  Director: Oliver Hirschbiegel
  Rating: 8/10
  VPRO ID: 536405
  URL: https://www.vprogids.nl/cinema/films/film~536405~der-untergang~.html
  Genres: Historische film, Oorlogsfilm, Drama

  Description (578 chars):
  In Der Untergang - over de laatste dagen van de Führer - wordt nauwgezet
  in beeld gebracht hoe Hitler (een geniale Ganz) aanvankelijk nog aardige
  kantjes had, bijvoorbeeld voor zijn secretaresse Traudl Junge...
```

### Test via HTTP endpoints

```bash
# Test endpoint with JSON response (film)
curl "http://localhost:5100/test?title=Le+dernier+métro&year=1980"

# Test endpoint for TV series
curl "http://localhost:5100/test?title=Adolescence&year=2025&type=series"

# Test the actual Plex metadata endpoint (this will produce cached results)
curl "http://localhost:5100/library/metadata/vpro-apocalypse-now-1979-tt0078788-m"

# View cache status
curl "http://localhost:5100/cache"

# View specific cached item
curl "http://localhost:5100/cache?key=vpro-apocalypse-now-1979-tt0078788"
```

### Credential management

The provider automatically refreshes POMS API credentials if authentication fails. You can also manage them manually:

```bash
# Force refresh credentials from vprogids.nl
docker exec vpro-plex-provider python vpro_lookup.py --refresh-credentials

# View cached credentials
docker exec vpro-plex-provider cat cache/credentials.json

# Simulate auth failure to test auto-refresh
docker exec vpro-plex-provider sh -c 'echo "{\"api_key\":\"bad\",\"api_secret\":\"bad\"}" > cache/credentials.json'
docker exec vpro-plex-provider python vpro_lookup.py "The Matrix" --year 1999 -v
# Should show: "auth failed, refreshing credentials..."
```

> **Note**: the `credentials.json` file is only created after a credential refresh (manual or automatic). If the file
> doesn't exist, the provider uses built-in default credentials. This is normal — the file will be created automatically
> if the defaults ever stop working.

### Cache management

```bash
# View all cached entries
curl "http://localhost:5100/cache"

# Clear all cache (preserves credentials.json)
curl -X POST "http://localhost:5100/cache/clear"

# Clear cache via filesystem (preserve credentials)
docker exec vpro-plex-provider sh -c 'find cache -name "*.json" ! -name "credentials.json" -delete'
```

### View logs

```bash
# Follow logs
docker-compose logs -f

# Or directly
docker logs -f vpro-plex-provider
```

## Environment Variables

| Variable          | Default            | Description                                               |
|-------------------|--------------------|-----------------------------------------------------------|
| `PORT`            | 5100               | Server port                                               |
| `LOG_LEVEL`       | INFO               | Logging level (DEBUG, INFO, WARNING, ERROR)               |
| `CACHE_DIR`       | ./cache            | Cache directory path                                      |
| `TMDB_API_KEY`    | *(none)*           | TMDB API key for alternate title lookup (**recommended**) |
| `POMS_CACHE_FILE` | ./credentials.json | Path to cached POMS credentials                           |

## API Endpoints

### Movie Provider (`/movies`)

| Endpoint                              | Method | Description                                 |
|---------------------------------------|--------|---------------------------------------------|
| `/movies`                             | GET    | Movie provider info (type 1)                |
| `/movies/library/metadata/<key>`      | GET    | Plex metadata lookup for movies             |
| `/movies/library/metadata/matches`    | POST   | Plex match endpoint for movies              |
| `/movies/library/metadata/<key>/images` | GET  | Returns empty (no artwork)                  |
| `/movies/library/metadata/<key>/extras` | GET  | Returns empty (no extras)                   |

### TV Provider (`/series`)

| Endpoint                               | Method | Description                                |
|----------------------------------------|--------|--------------------------------------------|
| `/series`                              | GET    | TV provider info (types 2, 3, 4)           |
| `/series/library/metadata/<key>`       | GET    | Plex metadata lookup for TV shows          |
| `/series/library/metadata/matches`     | POST   | Plex match endpoint for TV shows           |
| `/series/library/metadata/<key>/images` | GET   | Returns empty (no artwork)                 |
| `/series/library/metadata/<key>/extras` | GET   | Returns empty (no extras)                  |

### Shared Endpoints

| Endpoint                         | Method | Description                                    |
|----------------------------------|--------|------------------------------------------------|
| `/health`                        | GET    | Health check with version and config status    |
| `/test`                          | GET    | Test search: `?title=X&year=Y&imdb=ttZ&type=T` |
| `/cache`                         | GET    | List cached entries or view specific: `?key=X` |
| `/cache/clear`                   | POST   | Clear cached entries (preserves credentials)   |

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

### Provider not showing in Plex

1. Verify provider is running:
   ```bash
   curl http://localhost:5100/health
   ```

2. Check registration — in Plex, go to Settings → Metadata Agents and verify the provider appears

3. Verify network connectivity from Plex to provider:
   ```bash
   # From your Plex server/container
   curl http://PROVIDER_IP:5100/health
   ```

### No Dutch descriptions appearing

1. Test if the film exists in VPRO's database:
   ```bash
   docker exec vpro-plex-provider python vpro_lookup.py "FILM TITLE" --year YEAR -v
   ```

2. Check provider logs for errors:
   ```bash
   docker-compose logs --tail=100
   ```

3. Clear cache and retry:
   ```bash
   curl -X POST "http://localhost:5100/cache/clear"
   ```

### Metadata no longer updating in Plex after changing the TCP port (from the default port 5100 to something else).

There's a bug in Plex's metadata provider API that, once a metadata provider is registered, changes to its URL are not
applied, and Plex keeps trying on the old port. 

**Restarting the Plex server** fixes this issue.

### POMS API authentication errors

The provider auto-refreshes credentials, but you can force it:

```bash
docker exec vpro-plex-provider python vpro_lookup.py --refresh-credentials
```

### Search not finding films

Try with the original (non-English) title:

```bash
# Instead of "Downfall", try:
docker exec vpro-plex-provider python vpro_lookup.py "Der Untergang" --year 2004
```

Or provide the IMDB ID for automatic alternate title lookup (requires TMDB_API_KEY):

```bash
docker exec vpro-plex-provider python vpro_lookup.py "Downfall" --year 2004 --imdb tt0363163
```

### TMDB alternate titles not working

1. Verify your API key is set:
   ```bash
   curl http://localhost:5100/health
   # Check "tmdb_configured": true
   ```

2. If you have an IMDB ID, pass it via `--imdb` for direct alternate title lookup. Without an IMDB ID, the
   provider will search TMDB by title+year, which works but may be less accurate for ambiguous titles.

## Updating

To update to the latest version (assuming you are in the folder where you initially ran `git clone ...`):

```bash
# 1. Pull the latest changes
git pull

# 2. Stop the running container
docker-compose down

# 3. Rebuild with the latest code (--no-cache ensures fresh build)
docker-compose build --no-cache

# 4. Start the updated container
docker-compose up -d
```

#### Or, alternatively, in one go:

```bash
git pull && docker-compose down && docker-compose build --no-cache && docker-compose up -d
```

### Verify the update

After updating, verify the new version is running:

```bash
# Check the health endpoint for version info
curl http://localhost:5100/health

# Check the logs for any startup issues
docker-compose logs --tail=50
```

### Notes

- **Cache is preserved** — Your cached movie data and credentials remain intact during updates
- **Configuration preserved** — Your `.env` file is not overwritten by `git pull`
- **Check release notes** — Some updates may require Plex reconfiguration (see upgrade sections below)

### Upgrading to v3.1.0

Version 3.1.0 includes **breaking URL changes** — you must re-register providers in Plex:

**URL Changes:**
| Old URL | New URL |
|---------|---------|
| `http://localhost:5100/` | `http://localhost:5100/movies` |
| `http://localhost:5100/tv` | `http://localhost:5100/series` |

**Provider Name Changes:**
| Old Name | New Name |
|----------|----------|
| VPRO Cinema (Dutch Summaries) | VPRO Cinema (Dutch Summaries) - Movies |
| VPRO Cinema TV (Dutch Summaries) | VPRO Cinema (Dutch Summaries) - Series |

**Migration steps:**
1. Remove old provider URLs in Plex Settings → Metadata Agents → Metadata Providers
2. Add new URLs: `/movies` for films, `/series` for TV shows
3. Update your agents to use the new provider names
4. Restart Plex if metadata requests fail (known Plex bug with URL changes)

**Other improvements in v3.1.0:**
- Bidirectional TMDB lookup (finds alternate titles even without IMDB ID)
- Modular codebase for better maintainability
- Enhanced cache diagnostics

Your cache and `.env` configuration are preserved.

### Upgrading from v2.x to v3.x (TV series support)

Version 3.0.0 introduced TV series support with a **two-provider architecture**:

1. **Remove** the old provider URL in Plex Settings → Metadata Agents
2. **Add two provider URLs**:
   - `http://localhost:5100/movies` — for movies
   - `http://localhost:5100/series` — for TV shows
3. **Create a new TV Show agent** using "VPRO Cinema (Dutch Summaries) - Series" as primary
4. Update your movie agent to use "VPRO Cinema (Dutch Summaries) - Movies"

This split is required by Plex's Custom Metadata Provider API — a single provider cannot properly combine with
both "Plex Movie" and "Plex Series" as secondary providers. See the Quick Start section for detailed setup steps.

## Limitations

- **POMS API is undocumented** — Not officially supported by NPO. You may get rate-limited, blocked, or the API may
  change without notice
- **Not all content covered** — Only films and series reviewed by VPRO Cinema are available
- **No artwork** — Use the recommended agent setup, which falls back to Plex Movie/Series for posters
- **Web search fallback** — May occasionally hit rate limits or CAPTCHAs

## License

MIT — Do whatever you want with it.

## Credits

- [VPRO Cinema](https://vprogids.nl/cinema) for the Dutch film reviews
- [TMDB](https://www.themoviedb.org/) for alternate title data
- Klaas (c_kick/hnldesign) — Original idea and development
- Claude (Anthropic) — Implementation assistance
