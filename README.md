# VPRO Cinema Metadata Provider for Plex

A custom metadata provider that supplies Dutch film descriptions from VPRO Cinema (vprogids.nl) to Plex Media Server.

## Features

- 🇳🇱 Dutch film reviews/descriptions from VPRO Cinema's database
- 🔍 Direct NPO POMS API access with automatic credential refresh
- 🌍 TMDB alternate title lookup (finds French/Dutch/German titles automatically)
- 💾 Persistent caching with TTL for not-found entries
- 🔧 Self-healing: auto-refreshes API credentials if authentication fails
- 🐳 Docker-ready with health checks
- 🔗 Combines with other providers (as it only returns the `description` metadata)

## Prerequisites

### Required

- **Docker** and **Docker Compose** — [Install Docker](https://docs.docker.com/get-docker/)
- **Plex Media Server** — With access to Settings → Metadata Agents

### Recommended

- **TMDB API Key** — Enables automatic alternate language title lookup
  
  Many films are indexed in VPRO Cinema under their original (often French, German, or Dutch) title rather than the English title. Without a TMDB API key, searching for "The Last Metro" will fail, but with it, the provider automatically discovers and tries "Le dernier métro".

  Get your free API key at: https://www.themoviedb.org/settings/api

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/c-kick/vpro-cinema-plex.git
cd vpro-cinema-plex

# Copy and edit environment file
cp .env.example .env
```

#### Optionally: 
Edit `.env` and add your TMDB API key:
```bash
TMDB_API_KEY=your_tmdb_api_key_here
```

### 2. Build and run

```bash
docker-compose up -d

# Check logs
docker-compose logs -f
```

### 3. Verify it's running

```bash
# Health check
curl "http://localhost:5100/health"

# Test a search
curl "http://localhost:5100/test?title=Apocalypse+Now&year=1979"
```

### 4. Register with Plex

**Important:** The provider URL must be reachable from your Plex server.

1. Log into the Plex web interface
2. Go to **Settings** → **Metadata Agents** (not the legacy one!)
3. Under *Metadata Providers* click **+ Add Provider**
4. Paste the URL to the agent, example: `http://localhost:5100/` and click **Save**
5. Under *Metadata Agents* click **+ Add Agent**
6. Give the agent a title, example: "VPRO Cinema + Plex Movie"
7. Select `VPRO Cinema (Dutch Summaries)` as the primary metadata provider
8. A section 'additional providers' appears, pick "Plex Movie" and click the **+** button
9. Lastly, pick "Plex Local Media" from the dropdown and click the **+** button
10. Click **Save**

Done! The agent is now configured to first search for Dutch movie summaries on VPRO Cinema, and get the rest of the metadata from Plex Movie, falling back to local media. If no summary is found on VPRO Cinema, it falls back to Plex Movie (and then to local media).

### 5. Configure your library

1. Go to **Plex Settings** → **Manage Libraries**
2. Click the `...` next to your movie library → **Edit Library**
3. Go to **Advanced** tab
4. Under **Agent**, select the agent you just created ("VPRO Cinema + Plex Movie")
5. Click **Save Changes**

### 6. Refresh metadata

For existing movies: Select movies → `...` → **Refresh Metadata**

New movies will automatically use the provider on scan.

## How It Works

```
┌─────────────────────────────────────────────────────────────────┐
│  Plex requests metadata for "The Last Metro" (1980)             │
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
│  2. TMDB Alternate Titles (requires TMDB_API_KEY + IMDB ID)     │
│     └─ Fetches French/Dutch/German titles, retries search       │
│     └─ "The Last Metro" → "Le dernier métro" → Found!           │
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

> **Note:** The CLI scraper (`vpro_cinema_scraper.py`) does not cache results (in `cache/`) — it only searches and returns data. Caching is handled by the HTTP provider (`vpro_metadata_provider.py`). To test with caching, use the HTTP `/library/metadata` endpoints below.

```bash
# Basic search
docker exec vpro-plex-provider python vpro_cinema_scraper.py "Apocalypse Now" --year 1979

# With IMDB ID (enables TMDB alternate title lookup)
docker exec vpro-plex-provider python vpro_cinema_scraper.py "The Last Metro" --year 1980 --imdb tt0080610

# Verbose mode (see full search flow)
docker exec vpro-plex-provider python vpro_cinema_scraper.py "The Matrix" --year 1999 -v
```

### Test via HTTP endpoints

```bash
# Test endpoint with JSON response
curl "http://localhost:5100/test?title=Le+dernier+métro&year=1980"

# Test the actual Plex metadata endpoint (this will produce cached results)
curl "http://localhost:5100/library/metadata/vpro-apocalypse-now-1979-tt0078788"

# View cache status
curl "http://localhost:5100/cache"

# View specific cached item
curl "http://localhost:5100/cache?key=vpro-apocalypse-now-1979-tt0078788"
```

### Credential management

The provider automatically refreshes POMS API credentials if authentication fails. You can also manage them manually:

```bash
# Force refresh credentials from vprogids.nl
docker exec vpro-plex-provider python vpro_cinema_scraper.py --refresh-credentials

# View cached credentials
docker exec vpro-plex-provider cat credentials.json

# Simulate auth failure to test auto-refresh
docker exec vpro-plex-provider sh -c 'echo "{\"api_key\":\"bad\",\"api_secret\":\"bad\"}" > credentials.json'
docker exec vpro-plex-provider python vpro_cinema_scraper.py "The Matrix" --year 1999 -v
# Should show: "auth failed, refreshing credentials..."
```
> **Note**: the `credentials.json` file is only created after a credential refresh (manual or automatic). If the file doesn't exist, the provider uses built-in default credentials. This is normal — the file will be created automatically if the defaults ever stop working.
### Cache management

```bash
# View all cached entries
curl "http://localhost:5100/cache"

# Clear all cache
curl -X POST "http://localhost:5100/cache/clear"

# Clear cache via filesystem
docker exec vpro-plex-provider rm -f cache/*.json
```

### View logs

```bash
# Follow logs
docker-compose logs -f

# Or directly
docker logs -f vpro-plex-provider
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | 5100 | Server port |
| `LOG_LEVEL` | INFO | Logging level (DEBUG, INFO, WARNING, ERROR) |
| `CACHE_DIR` | ./cache | Cache directory path |
| `TMDB_API_KEY` | *(none)* | TMDB API key for alternate title lookup (**recommended**) |
| `VPRO_CREDENTIALS_FILE` | ./credentials.json | Path to cached POMS credentials |

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Provider info (identifier, version, features) |
| `/health` | GET | Health check with version and config status |
| `/test` | GET | Test search: `?title=X&year=Y&imdb=ttZ` |
| `/cache` | GET | List cached entries or view specific: `?key=X` |
| `/cache/clear` | POST | Clear all cached entries |
| `/library/metadata/<key>` | GET | Plex metadata lookup |
| `/library/metadata/matches` | POST | Plex match endpoint |
| `/library/metadata/<key>/images` | GET | Returns empty (no artwork) |

## File Structure

```
vpro-cinema-plex/
├── docker-compose.yml          # Docker Compose config
├── Dockerfile                  # Container definition
├── .env.example                # Environment template
├── .gitignore
├── requirements.txt            # Python dependencies
├── vpro_cinema_scraper.py      # Core search/scrape logic (v2.5.0)
├── vpro_metadata_provider.py   # Flask server for Plex (v2.4.0)
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
   docker exec vpro-plex-provider python vpro_cinema_scraper.py "FILM TITLE" --year YEAR -v
   ```

2. Check provider logs for errors:
   ```bash
   docker-compose logs --tail=100
   ```

3. Clear cache and retry:
   ```bash
   curl -X POST "http://localhost:5100/cache/clear"
   ```

### POMS API authentication errors

The provider auto-refreshes credentials, but you can force it:

```bash
docker exec vpro-plex-provider python vpro_cinema_scraper.py --refresh-credentials
```

### Search not finding films

Try with the original (non-English) title:
```bash
# Instead of "The Last Metro", try:
docker exec vpro-plex-provider python vpro_cinema_scraper.py "Le dernier métro" --year 1980
```

Or provide the IMDB ID for automatic alternate title lookup (requires TMDB_API_KEY):
```bash
docker exec vpro-plex-provider python vpro_cinema_scraper.py "The Last Metro" --year 1980 --imdb tt0080610
```

### TMDB alternate titles not working

1. Verify your API key is set:
   ```bash
   curl http://localhost:5100/health
   # Check "tmdb_configured": true
   ```

2. Ensure you're passing an IMDB ID — alternate title lookup requires it

## Updating

```bash
git pull
docker-compose down
docker-compose build --no-cache
docker-compose up -d
```

## Limitations

- **POMS API is undocumented** — Not officially supported by NPO. You may get rate-limited, blocked, or the API may change without notice
- **Movies only** — No TV shows or documentaries (yet)
- **Not all films covered** — Only films reviewed by VPRO Cinema are available
- **No artwork** — Use the recommended agent setup, which falls back to Plex Movie for posters
- **Web search fallback** — May occasionally hit rate limits or CAPTCHAs

## License

MIT — Do whatever you want with it.

## Credits

- [VPRO Cinema](https://vprogids.nl/cinema) for the Dutch film reviews
- [TMDB](https://www.themoviedb.org/) for alternate title data
- Klaas (c_kick/hnldesign) — Original idea and development
- Claude (Anthropic) — Implementation assistance
