"""
Shared data models for VPRO metadata lookup.

This module contains data classes used across the VPRO Cinema metadata provider,
kept separate to avoid circular imports between modules.

NOTE: This provider only supports MOVIES. TV series support has been removed.
"""

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any


@dataclass
class VPROFilm:
    """Represents a film with VPRO Cinema metadata.

    Note: Despite the name, this only represents movies. TV series support
    has been removed. The media_type field is kept for backward compatibility.
    """
    title: str
    year: Optional[int] = None
    director: Optional[str] = None
    description: Optional[str] = None
    url: Optional[str] = None
    imdb_id: Optional[str] = None
    vpro_id: Optional[str] = None
    genres: List[str] = field(default_factory=list)
    vpro_rating: Optional[int] = None
    content_rating: Optional[str] = None  # Kijkwijzer age rating (AL, 6, 9, 12, 14, 16, 18)
    images: List[Dict[str, str]] = field(default_factory=list)  # [{type, url, title}]
    media_type: str = "film"  # Always "film" (kept for backward compatibility)
    # Lookup diagnostics
    lookup_method: Optional[str] = None  # "poms", "tmdb_alt", "web", "tmdb_fallback"
    discovered_imdb: Optional[str] = None  # IMDB found via TMDB lookup

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            'title': self.title,
            'year': self.year,
            'director': self.director,
            'description': self.description,
            'url': self.url,
            'imdb_id': self.imdb_id,
            'vpro_id': self.vpro_id,
            'genres': self.genres,
            'vpro_rating': self.vpro_rating,
            'content_rating': self.content_rating,
            'images': self.images,
            'media_type': self.media_type,
            'lookup_method': self.lookup_method,
            'discovered_imdb': self.discovered_imdb,
        }
