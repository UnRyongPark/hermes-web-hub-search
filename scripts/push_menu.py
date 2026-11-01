"""Push the Hermes Telegram command menu (including plugin commands) without a gateway restart.

Mirrors hermes_cli.commands_platforms.telegram_menu_commands + the adapter's three
set_my_commands scopes, then reads back get_my_commands to prove it landed.
"""

import asyncio
import os
import sys

sys.path.insert(0, "/home/hermes/.hermes/hermes-agent")

from hermes_cli.commands_platforms import telegram_menu_commands, telegram_menu_max_commands
from hermes_cli.config import read_raw_config


def bot_token() -> str:
    """TELEGRAM_BOT_TOKEN from the profile .env (same source the gateway uses)."""
    profile = os.environ.get("HERMES_PROFILE", "family")
    env_path = os.path.expanduser(f"~/.hermes/profiles/{profile}/.env")
    token = ""
    with open(env_path, encoding="utf-8") as handle:
        for line in handle:
            key, _, value = line.strip().partition("=")
            if key == "TELEGRAM_BOT_TOKEN":
                token = value.strip().strip('"').strip("'")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN missing from the profile .env")
    return token


async def main() -> None:
    from telegram import Bot, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats, BotCommandScopeDefault

    cap = telegram_menu_max_commands()
    menu, hidden = telegram_menu_commands(cap)
    print(f"menu: {len(menu)} commands, {hidden} hidden")
    print("hub rows:", [n for n, _ in menu if "hubsearch" in n])

    bot = Bot(token=bot_token())
    commands = [__import__("telegram").BotCommand(command=n, description=d[:256]) for n, d in menu]
    for scope_cls in (BotCommandScopeDefault, BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats):
        try:
            await bot.set_my_commands(commands, scope=scope_cls())
            print(f"set_my_commands OK {scope_cls.__name__}: {len(commands)}")
        except Exception as exc:  # noqa: BLE001
            print(f"set_my_commands FAILED {scope_cls.__name__}: {exc}")

    mine = await bot.get_my_commands(scope=BotCommandScopeDefault())
    names = [c.command for c in mine]
    print(f"read back {len(names)} commands; hub present: {[n for n in names if 'hubsearch' in n]}")


if __name__ == "__main__":
    os.environ.setdefault("HERMES_PROFILE", sys.argv[1] if len(sys.argv) > 1 else "family")
    asyncio.run(main())
