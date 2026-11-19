"""Application entry point for sibot."""

import nonebot
from nonebot.adapters.discord import Adapter as DiscordAdapter
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
from nonebot.adapters.telegram import Adapter as TelegramAdapter
from nonebot.config import Env


def main() -> None:
    environment = Env().environment
    nonebot.init(_env_file=("defaults.env", ".env", f".env.{environment}"))

    # An adapter without configured bots stays idle.
    driver = nonebot.get_driver()
    driver.register_adapter(OneBotV11Adapter)
    driver.register_adapter(DiscordAdapter)
    driver.register_adapter(TelegramAdapter)

    nonebot.load_from_toml("pyproject.toml")
    nonebot.run()


if __name__ == "__main__":
    main()
