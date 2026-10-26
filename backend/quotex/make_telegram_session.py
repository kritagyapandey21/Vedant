"""Make the TELEGRAM_SESSION_STRING for the Quotex backend. Run it once on YOUR computer, not on the server:

    pip install telethon
    python make_telegram_session.py

It asks for the api id and api hash (from https://my.telegram.org > API development tools), then your phone number and
the login code Telegram sends you. It prints one long string: put it in the server's backend/quotex/.env as
TELEGRAM_SESSION_STRING. That string logs in as your Telegram account, so keep it as secret as a password.
"""

import asyncio
import getpass

from telethon import TelegramClient
from telethon.sessions import StringSession


async def main() -> None:
    api_id = int(input("api id: ").strip())
    api_hash = getpass.getpass("api hash (hidden): ").strip()
    async with TelegramClient(StringSession(), api_id, api_hash) as client:
        await client.start()                 # asks for the phone number, the code and the 2FA password if set
        print("\nTELEGRAM_SESSION_STRING=" + client.session.save())


if __name__ == "__main__":
    asyncio.run(main())
