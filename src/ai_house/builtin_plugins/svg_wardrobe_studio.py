"""Zero-cost procedural SVG paper-doll wardrobe studio provider."""
from ..plugin_api import PluginDefinition, PluginManifest, StudioProvider


class SvgWardrobeStudio(StudioProvider):
    provider_id = "svg_wardrobe"

    def step(self, host) -> None:
        return None


PLUGIN_DEF = PluginDefinition(
    manifest=PluginManifest(
        plugin_id="svg_wardrobe",
        name="矢量衣柜渲染",
        kind="provider",
        version="0.3.0",
        description="零开销本地矢量衣柜预览与换装立绘辅助提供方。",
        default_enabled=True,
    ),
    provider_factory=SvgWardrobeStudio,
)
