"""Hearing and speaking. Only imported in voice mode, so text mode starts without the audio libraries."""
from .ears import Ears
from .mouth import Mouth

__all__ = ["Ears", "Mouth"]
