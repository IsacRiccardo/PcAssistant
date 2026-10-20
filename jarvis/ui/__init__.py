"""
The floating orb overlay. Only Status is imported here, so --no-ui and text mode never load Qt;
the motion lives in ui.orb, and the window that draws it in ui.hud.
"""
from .status import Status

__all__ = ["Status"]
