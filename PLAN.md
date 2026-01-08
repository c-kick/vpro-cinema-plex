# Implementation Plan: Bug Fixes and Edge Cases

## Context
This is a private home server API for Plex. Traffic is ~1-2 requests/day with occasional
full library refreshes. Performance and DoS concerns are deprioritized.

---

## Phase 1: Critical Bugs (Will Break Functionality)

### 1.1 Fix Rating Key Parsing for Hyphenated Titles
**Problem**: Titles like "Die-Hard", "Spider-Man: No Way Home" break parsing because
hyphens are used as delimiters.

**Solution**: Use a different delimiter (double-underscore `__`) or encode the title
to avoid hyphens.

**Files**:
- `vpro_metadata_provider.py` (generate_rating_key, parse_rating_key)
- `cache.py` (_find_by_title_year)

**Approach**:
- Replace hyphens in title with underscores during key generation
- Keep `-` as field delimiter: `vpro__title_with_underscores__1988__tt1234567__m`
- Update parsing to split on `__` instead of `-`
- Add migration note for existing cache entries

---

### 1.2 Fix IMDB ID Validation for 9+ Digit IDs
**Problem**: IMDB now issues 9-digit IDs. Current regex only allows 7-8.

**Solution**: Update regex to allow 7-10 digits.

**Files**: `text_utils.py` (validate_imdb_id, extract_imdb_from_text)

**Change**:
```python
# Before
r'^tt\d{7,8}$'
# After
r'^tt\d{7,10}$'
```

---

### 1.3 Fix Year Extraction to Prefer Parenthesized Years
**Problem**: "2001: A Space Odyssey (1968)" extracts 2001 instead of 1968.

**Solution**: Already tries parenthesized years first, but page scraper doesn't.
Fix `vpro_scraper.py` to use `extract_year_from_text` instead of naive regex.

**Files**: `vpro_scraper.py` (VPROPageScraper.scrape)

---

### 1.4 Fix Title Similarity Threshold
**Problem**: 0.3 Jaccard similarity matches completely different movies.

**Solution**: Raise to 0.6 minimum. Also consider using word order for short titles.

**Files**: `constants.py`

**Analysis**:
- "The Matrix" vs "Matrix Reloaded": Jaccard = 2/4 = 0.5 (would match at 0.3)
- "Alien" vs "Aliens": Jaccard = 0.5 (would match at 0.3)
- At 0.6 threshold: only "The Matrix" vs "The Matrix Returns" type matches

---

## Phase 2: Important Improvements

### 2.1 Add Negative Caching with Short TTL
**Problem**: Not-found entries retry all APIs every request. Full library refresh
hammers APIs for obscure films.

**Solution**: Cache "not found" entries for 7 days (vs 30 days for found).

**Files**:
- `constants.py` (add DEFAULT_CACHE_TTL_NOT_FOUND)
- `cache.py` (CacheEntry.is_expired)
- `vpro_metadata_provider.py` (handle_metadata_request)

**Approach**:
- Store not-found entries with status="not_found"
- Use shorter TTL (7 days)
- Return empty response from cache for not-found
- Add /cache/clear?not_found=true to clear only not-found entries

---

### 2.2 Remove Hardcoded Credentials from Code
**Problem**: API credentials committed to git history.

**Solution**:
- Remove DEFAULT_POMS_API_KEY and DEFAULT_POMS_API_SECRET from constants.py
- Make credential extraction mandatory
- Fail loudly if credentials can't be fetched

**Files**: `constants.py`, `credentials.py`

**Note**: Git history still contains them. Add note to README about credential rotation.

---

### 2.3 Fix Match Log Race Condition
**Problem**: Non-atomic log rotation under concurrent access.

**Solution**: Use append-only with external rotation, or atomic rewrite with temp file.

**Files**: `vpro_metadata_provider.py` (log_match_request)

**Approach**:
- Write to temp file, atomic rename (like cache.py does)
- Or simpler: just let it grow, cap at 10MB not line count

---

## Phase 3: Code Quality (Nice to Have)

### 3.1 Improve Credential Extraction Reliability
**Problem**: Regex-based extraction breaks if VPRO changes site format.

**Solution**:
- Add multiple fallback patterns
- Log clear warning when extraction fails
- Test credential validity with a probe request before using

**Files**: `credentials.py`

---

### 3.2 Better Error Handling
**Problem**: Exception swallowing with `except Exception` makes debugging hard.

**Solution**:
- Create custom exception hierarchy
- Log full tracebacks at DEBUG level
- Return structured error info

**Files**: All files with `except Exception as e: logger.warning`

---

### 3.3 Add Basic Unit Tests
**Problem**: No tests beyond "does it import".

**Solution**: Add tests for:
- Rating key generation/parsing (especially edge cases)
- Title matching with various inputs
- IMDB extraction from different formats
- Cache read/write cycle

**Files**: New `tests/` directory

---

## Migration Notes

### Cache Compatibility
- Old cache entries with `-` delimiter will fail to parse with new `__` delimiter
- Option A: Clear cache on upgrade (simple, users rarely have large caches)
- Option B: Support both formats in parsing (more complex)

Recommend Option A with note in release notes.

### Breaking Changes
- Rating key format changes (cache invalidation)
- Similarity threshold increase (may return fewer matches, but more accurate)

---

## Implementation Order

1. **1.2** - IMDB regex (5 min, zero risk)
2. **1.4** - Similarity threshold (5 min, zero risk)
3. **1.3** - Year extraction (15 min, low risk)
4. **1.1** - Rating key format (30 min, medium risk - cache invalidation)
5. **2.1** - Negative caching (30 min, low risk)
6. **2.2** - Remove hardcoded creds (15 min, low risk)
7. **2.3** - Match log fix (15 min, zero risk)

Total estimated: ~2 hours

---

## Out of Scope (Private API)

These were identified but are not worth fixing for home server use:
- Request body size limits
- Rate limiting on /test endpoint
- Gunicorn/production WSGI server
- Prometheus metrics format
- Graceful shutdown handling
- Circuit breaker pattern
- Async I/O
- Memory leak in rate limiters (will never matter)
