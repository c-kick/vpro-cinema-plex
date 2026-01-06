# VPRO Cinema Metadata Provider for Plex

![Example of the Metadata Provider in action!](https://github.com/user-attachments/assets/002b61b3-c05c-4888-a1c6-c34bf38d6dd1)

A custom metadata provider that supplies Dutch film and TV series descriptions from [VPRO Cinema](https://www.vprogids.nl/cinema/) to Plex Media Server.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Docker](https://img.shields.io/badge/docker-ready-blue.svg)](https://www.docker.com/)
[![Plex 1.40+](https://img.shields.io/badge/Plex-1.40%2B-E5A00D.svg?logo=plex&logoColor=white)](https://www.plex.tv/)

## Features

- 🇳🇱 Dutch film and TV series reviews/descriptions from VPRO Cinema's database
- 📺 Supports both movies and TV series
- 🔍 Direct NPO POMS API access with automatic credential refresh
- 🌍 TMDB alternate title lookup (enables matching titles in other languages)
- 💾 Persistent caching with TTL for not-found entries
- 🔧 Self-healing: auto-refreshes API credentials if authentication fails
- 🐳 Docker-ready with health checks
- 🔗 Combines with other providers (as it only returns the `description` metadata)

## Background

For years I wanted to automatically pull the excellent Dutch film reviews from VPRO Cinema into Plex. I made several
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

- **TMDB API Key** — Enables automatic alternate language title lookup

  Many films are indexed in VPRO Cinema under their original (often French, German, or Dutch) title rather than the
  English title. Without a TMDB API key, searching for "Downfall" will fail, but with it, the provider automatically
  discovers and tries "Der Untergang".

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

**Important:** The provider URL must be reachable from your Plex server.

1. Log into the Plex web interface
2. Go to **Settings** → **Metadata Agents** (not the legacy one!)
3. Under *Metadata Providers* click **+ Add Provider**
4. Paste the URL to the agent, example: `http://localhost:5100/` and click **Save**

#### Create a Movie Agent

5. Under *Metadata Agents* click **+ Add Agent**
6. Give the agent a title, example: "VPRO Cinema + Plex Movie"
7. Select `VPRO Cinema (Dutch Summaries)` as the primary metadata provider
8. A section 'additional providers' appears, pick "Plex Movie" and click the **+** button
9. Optionally add "Plex Local Media" from the dropdown
10. Click **Save**

#### Create a TV Show Agent (optional)

11. Under *Metadata Agents* click **+ Add Agent** again
12. Give the agent a title, example: "VPRO Cinema + Plex Series"
13. Select `VPRO Cinema (Dutch Summaries)` as the primary metadata provider
14. Pick "Plex Series" and click the **+** button
15. Optionally add "Plex Local Media"
16. Click **Save**

Done! The agents are now configured to first search for Dutch summaries on VPRO Cinema, falling back to Plex
Movie/Series for remaining metadata.

### 5. Configure your libraries

1. Go to **Plex Settings** → **Manage Libraries**
2. Click the `...` next to your movie library → **Edit Library**
3. Go to **Advanced** tab
4. Under **Agent**, select the movie agent you created ("VPRO Cinema + Plex Movie")
5. Click **Save Changes**
6. Repeat for your TV show library, selecting the TV show agent ("VPRO Cinema + Plex Series")

### 6. Refresh metadata

For existing content: Select items → `...` → **Refresh Metadata**

New movies and TV shows will automatically use the provider on scan.

## How It Works

```
┌─────────────────────────────────────────────────────────────────┐
│  Plex requests metadata for "Downfall" (2004)             │
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
│     └─ "Downfall" → "Der Untergang" → Found!           │
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

> **Note:** The CLI scraper (`vpro_cinema_scraper.py`) does not cache results (in `cache/`) — it only searches and
> returns data. Caching is handled by the HTTP provider (`vpro_metadata_provider.py`). To test with caching, use the HTTP
`/library/metadata` endpoints below.

```bash
# Basic search (searches both films and series by default)
docker exec vpro-plex-provider python vpro_cinema_scraper.py "Apocalypse Now" --year 1979

# Search for TV series only
docker exec vpro-plex-provider python vpro_cinema_scraper.py "Adolescence" --year 2025 --type series

# Search for films only
docker exec vpro-plex-provider python vpro_cinema_scraper.py "Downfall" --year 2004 --type film

# With IMDB ID (enables TMDB alternate title lookup) in verbose mode (showing full search flow)
docker exec vpro-plex-provider python vpro_cinema_scraper.py "Downfall" --year 2004 --imdb tt0363163 -v
```

Example output:

```
Searching VPRO Cinema for: Downfall (2004)
------------------------------------------------------------
Searching VPRO: 'Downfall' (2004) [tt0363163]
POMS: Rejecting title match 'Downfall' (1964) - year diff 40
POMS: Rejecting 'Downfall' (1964) - year diff 40
No POMS match for 'Downfall' - fetching alternate titles from TMDB...
TMDB alternate titles for tt0363163: ['Der Untergang', 'A Queda! As Últimas Horas de Hitler', '몰락 - 히틀러와 제3제국의 종말', 'Undergången - Hitler och Tredje Rikets fall', 'Der Untergang - det tredje rikets siste dager']
Trying alternate title: 'Der Untergang'
POMS: Exact match - Der Untergang (2004)
Found via alternate title 'Der Untergang': Der Untergang

✓ Found: Der Untergang
  Year: 2004
  Director: Oliver Hirschbiegel
  Rating: 8/10
  VPRO ID: 536405
  URL: https://www.vprogids.nl/cinema/films/film~536405~der-untergang~.html
  Genres: Historische film, Oorlogsfilm, Drama

  Description (578 chars):
  In Der Untergang - over de laatste dagen van de Führer - wordt nauwgezet in beeld gebracht hoe Hitler (een geniale Ganz) aanvankelijk nog aardige kantjes had, bijvoorbeeld voor zijn secretaresse Traudl Junge, op wier memoires de film is gebaseerd. Eenmaal in het nauw gedreven door de geallieerden wordt hij steeds open­­lijker onaangenaam. Hitler en zijn intieme kring worden neergezet als personen die geen gevoel (meer) hebben voor de rauwe werkelijkheid. Die de kijker overigens regelmatig te zie...
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
docker exec vpro-plex-provider python vpro_cinema_scraper.py --refresh-credentials

# View cached credentials
docker exec vpro-plex-provider cat cache/credentials.json

# Simulate auth failure to test auto-refresh
docker exec vpro-plex-provider sh -c 'echo "{\"api_key\":\"bad\",\"api_secret\":\"bad\"}" > cache/credentials.json'
docker exec vpro-plex-provider python vpro_cinema_scraper.py "The Matrix" --year 1999 -v
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

| Endpoint                         | Method | Description                                    |
|----------------------------------|--------|------------------------------------------------|
| `/`                              | GET    | Provider info (identifier, version, features)  |
| `/health`                        | GET    | Health check with version and config status    |
| `/test`                          | GET    | Test search: `?title=X&year=Y&imdb=ttZ&type=T` |
| `/cache`                         | GET    | List cached entries or view specific: `?key=X` |
| `/cache/clear`                   | POST   | Clear cached entries (preserves credentials)   |
| `/library/metadata/<key>`        | GET    | Plex metadata lookup                           |
| `/library/metadata/matches`      | POST   | Plex match endpoint                            |
| `/library/metadata/<key>/images` | GET    | Returns empty (no artwork)                     |
| `/library/metadata/<key>/extras` | GET    | Returns empty (no extras)                      |

## File Structure

```
vpro-cinema-plex/
├── docker-compose.yml          # Docker Compose config
├── Dockerfile                  # Container definition
├── env.example                 # Environment template (copy to .env)
├── gitignore                   # Git ignore rules (rename to .gitignore)
├── requirements.txt            # Python dependencies
├── vpro_cinema_scraper.py      # Core search/scrape logic (v3.0.0)
├── vpro_metadata_provider.py   # Flask server for Plex (v3.0.0)
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

### Metadata no longer updating in Plex after changing the TCP port (from the default port 5100 to something else).

There's a bug in Plex's metadata provider API that, once a metadata provider is registered, changes to its URL are not
applied, and Plex keeps trying on the old port. 

**Restarting the Plex server** fixes this issue.

### POMS API authentication errors

The provider auto-refreshes credentials, but you can force it:

```bash
docker exec vpro-plex-provider python vpro_cinema_scraper.py --refresh-credentials
```

### Search not finding films

Try with the original (non-English) title:

```bash
# Instead of "Downfall", try:
docker exec vpro-plex-provider python vpro_cinema_scraper.py "Der Untergang" --year 2004
```

Or provide the IMDB ID for automatic alternate title lookup (requires TMDB_API_KEY):

```bash
docker exec vpro-plex-provider python vpro_cinema_scraper.py "Downfall" --year 2004 --imdb tt0363163
```

### TMDB alternate titles not working

1. Verify your API key is set:
   ```bash
   curl http://localhost:5100/health
   # Check "tmdb_configured": true
   ```

2. Ensure you're passing an IMDB ID — alternate title lookup requires it

## Updating

To update to the latest version:

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
- **No Plex reconfiguration needed** — The provider URL stays the same, so Plex continues to work without changes

### Upgrading to v3.0.0 (TV series support)

If you're upgrading from v2.x to v3.0.0, Plex may not recognize the new TV series capabilities until you
**remove and re-add the provider** in Plex Settings → Metadata Agents. This is because Plex caches provider
capabilities. After re-adding, you can create a TV show agent as described in the Quick Start section.

## Limitations

- **POMS API is undocumented** — Not officially supported by NPO. You may get rate-limited, blocked, or the API may
  change without notice
- **Not all content covered** — Only films and series reviewed by VPRO Cinema are available
- **No documentaries** — Documentaries are not yet supported
- **No artwork** — Use the recommended agent setup, which falls back to Plex Movie/Series for posters
- **Web search fallback** — May occasionally hit rate limits or CAPTCHAs

## License

MIT — Do whatever you want with it.

## Credits

- [VPRO Cinema](https://vprogids.nl/cinema) for the Dutch film reviews
- [TMDB](https://www.themoviedb.org/) for alternate title data
- Klaas (c_kick/hnldesign) — Original idea and development
- Claude (Anthropic) — Implementation assistance
