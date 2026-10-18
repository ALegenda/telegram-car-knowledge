"""Run in the owner's SSH terminal; stores the BotFather token on this server only."""
import getpass
import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from pathlib import Path


def main():
    if os.geteuid() != 0:
        raise SystemExit("Run as root over your SSH terminal.")
    token = getpass.getpass("Token from BotFather (hidden): ").strip()
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{25,}", token):
        raise SystemExit("Invalid token format; no file was changed.")
    try:
        with urllib.request.urlopen("https://api.telegram.org/bot" + token + "/getMe", timeout=20) as response:
            info = json.load(response)
    except (urllib.error.URLError, ValueError):
        raise SystemExit("Telegram did not confirm the token; no file was changed.") from None
    if not info.get("ok") or info["result"].get("username", "").lower() != "tg_channel_parse_bot":
        raise SystemExit("Token is not for @tg_channel_parse_bot; no file was changed.")
    folder = Path("/etc/tg-knowledge-bot")
    folder.mkdir(parents=True, exist_ok=True, mode=0o750)
    fd, temporary = tempfile.mkstemp(prefix=".token-", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write("TELEGRAM_BOT_TOKEN=" + token + "\n")
        os.chmod(temporary, 0o600)
        Path(temporary).replace(folder / "telegram.env")
    finally:
        Path(temporary).unlink(missing_ok=True)
    print("Token saved on this server for @tg_channel_parse_bot. No messages were sent.")


if __name__ == "__main__":
    main()
