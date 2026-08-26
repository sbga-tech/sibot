"""Application entry point for sibot."""

import nonebot
from nonebot.adapters.onebot.v11 import Adapter


def main() -> None:
    nonebot.init()

    driver = nonebot.get_driver()
    driver.register_adapter(Adapter)

    nonebot.load_from_toml("pyproject.toml")
    nonebot.run()


if __name__ == "__main__":
    main()
