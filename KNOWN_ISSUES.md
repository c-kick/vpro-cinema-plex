# Known Issues & Investigation Notes

## Plex "images" Feature Declaration Prevents Secondary Agent Fallback

**Status:** Worked around (v4.1.1), root cause in Plex not fully understood
**Date:** 2026-02-05

### Problem

When the provider declared the `"images"` feature in its manifest, Plex treated it as the
authoritative image source. For movies not on Cinema.nl, the provider returned empty images,
and Plex did **not** fall back to secondary agents (Plex Movie) or Local Media Assets (LMA)
for poster artwork — even though `poster.jpg` and `folder.jpg` existed locally.

Curiously, metadata merging (description, cast, genres) from secondary agents worked fine.
Only images were affected.

### What we tried

1. **Conditional images feature** (v4.1.0): Only declare `"images"` when `VPRO_RETURN_IMAGES=true`.
   Result: Worked for toggling between VPRO images and secondary agent images, but didn't
   solve the case where VPRO_RETURN_IMAGES=true AND the movie isn't on Cinema.nl.

2. **Removing images feature entirely** (v4.1.1): Never declare `"images"` in the manifest.
   VPRO images are embedded directly in the metadata response (thumb, art, Image array)
   which works without the feature declaration.
   Result: Secondary agent provided _some_ image, but it was a still frame instead of the
   TMDB poster. Local `poster.jpg` was also not picked up by LMA.

3. **TMDB fallback images** (v4.1.1): When VPRO lookup fails and we fall back to TMDB for
   basic metadata, also include the TMDB poster/backdrop URLs in the response.
   Result: This works reliably. The proper TMDB poster now shows in Plex.

### Remaining questions

- **Why didn't Plex Movie provide the TMDB poster as secondary agent?**
  Plex Movie DID provide description, cast, and genres correctly. But for images it returned
  a still frame instead of the TMDB poster. The movie (The Great Arch / tt32398150) has a
  proper poster on TMDB. Unknown whether this is a Plex Movie bug, a caching issue, or
  a priority/merge behavior difference between metadata and images.

- **Why didn't Local Media Assets pick up poster.jpg?**
  The movie folder contained both `poster.jpg` and `folder.jpg`. LMA is declared in our
  Source array (`com.plexapp.agents.localmedia`). Subtitles via LMA work fine. But local
  poster artwork was not loaded. Possible causes:
  - LMA might handle images differently than subtitles in the new custom provider system
  - The Source array might not be sufficient for image handling (the TMDB example provider
    has no Source array at all)
  - Plex might need "Plex Local Media" added as a provider in the agent group, not just
    declared in the Source array

- **Is the Source array needed at all?**
  The TMDB example provider doesn't have one. We added it for subtitle/sidecar detection.
  It works for subtitles. Unclear if it has any effect on images. Worth testing removal to
  see if subtitles still work (they might be handled by the scanner, not the agent).

### Current workaround

- Never declare `"images"` feature in the manifest
- Embed VPRO images directly in metadata response when available (Cinema.nl movies)
- For TMDB fallback movies, include TMDB poster/backdrop in the metadata response
- This ensures every movie gets poster artwork regardless of Cinema.nl coverage
