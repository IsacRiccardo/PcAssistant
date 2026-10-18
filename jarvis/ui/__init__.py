"""
The floating orb overlay. Only Status is imported here, so --no-ui and text mode never load Tk;
the window itself lives in ui.hud.
"""
from .status import Status

__all__ = ["Status"]
