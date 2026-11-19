"""Application entry point for sibot."""

from importlib import import_module

import nonebot
from nonebot.config import Env

# Adapters register only when their connection setting is present, so a
# deployment enables a platform by configuring it.
_ADAPTERS = (
    ("onebot_v11_ws_urls", "nonebot.adapters.onebot.v11"),
    ("discord_bots", "nonebot.adapters.discord"),
    ("telegram_bots", "nonebot.adapters.telegram"),
)


def main() -> None:
    environment = Env().environment
    nonebot.init(_env_file=("defaults.env", ".env", f".env.{environment}"))

    driver = nonebot.get_driver()
    for setting, module in _ADAPTERS:
        if getattr(driver.config, setting, None):
            driver.register_adapter(import_module(module).Adapter)

    nonebot.load_from_toml("pyproject.toml")
    nonebot.run()


if __name__ == "__main__":
    main()
