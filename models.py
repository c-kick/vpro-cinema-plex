"""
Shared data models for VPRO metadata lookup.

This module contains data classes used across the VPRO Cinema metadata provider,
kept separate to avoid circular imports between modules.
"""

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any


@dataclass
class VPROFilm:
    """Represents a film or series with VPRO Cinema metadata."""
    title: str
    year: Optional[int] = None
    director: Optional[str] = None
    description: Optional[str] = None
    url: Optional[str] = None
    imdb_id: Optional[str] = None
    vpro_id: Optional[str] = None
    genres: List[str] = field(default_factory=list)
    vpro_rating: Optional[int] = None
    media_type: str = "film"  # "film" or "series"
    # Lookup diagnostics
    lookup_method: Optional[str] = None  # "poms", "tmdb_alt", "web"
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
            'media_type': self.media_type,
            'lookup_method': self.lookup_method,
            'discovered_imdb': self.discovered_imdb,
        }
