"""Zero-cost procedural SVG paper-doll wardrobe studio provider."""
from ..plugin_api import StudioProvider


class SvgWardrobeStudio(StudioProvider):
    provider_id = "svg_wardrobe"

    def step(self, host) -> None:
        return None
