from pathlib import Path

import reflex as rx
from reflex_base.plugins.sitemap import SitemapPlugin

config = rx.Config(
    app_name="pustakam",
    app_module_import="pustakam",
    telemetry_enabled=False,
    default_color_mode="light",
    bun_path=Path("/home/surya/.bun/bin/bun"),
    plugins=[rx.plugins.RadixThemesPlugin()],
    disable_plugins=[SitemapPlugin],
)
