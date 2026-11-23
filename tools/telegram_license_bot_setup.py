#!/usr/bin/env python3
"""Configure the Telegram admin bot for the activation Worker.

The bot logic lives in cloudflare/license-worker/worker.js (route
``POST /telegram/webhook``). This helper only talks to the Telegram Bot API — it
registers/removes the webhook and reveals the chat ids you must allow-list.

The token is read from the environment (never commit it):

    export TELEGRAM_TOKEN=...
    export TELEGRAM_WEBHOOK_SECRET=$(python -c 'import secrets;print(secrets.token_urlsafe(32))')

    python tools/telegram_license_bot_setup.py getme
    python tools/telegram_license_bot_setup.py updates           # find your id
    python tools/telegram_license_bot_setup.py set \
        https://license.manhal-almasriiii199119.workers.dev/telegram/webhook
    python tools/telegram_license_bot_setup.py info
    python tools/telegram_license_bot_setup.py delete
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request


def _call(token: str, method: str, body=None) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Configure the activation Telegram bot")
    parser.add_argument(
        "action",
        choices=["getme", "set", "delete", "info", "updates"],
        help="set/delete require a webhook URL for 'set'",
    )
    parser.add_argument("url", nargs="?", help="webhook URL (for 'set')")
    args = parser.parse_args(argv)

    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        print("error: set TELEGRAM_TOKEN in the environment", file=sys.stderr)
        return 2

    if args.action == "getme":
        result = _call(token, "getMe")
    elif args.action == "info":
        result = _call(token, "getWebhookInfo")
    elif args.action == "updates":
        result = _call(token, "getUpdates", {"limit": 20})
        users = {}
        for update in result.get("result", []):
            message = update.get("message") or {}
            sender = message.get("from") or {}
            if sender.get("id"):
                users[sender["id"]] = sender.get("username") or sender.get("first_name")
        if users:
            print("Telegram user ids seen (add the admin(s) to TELEGRAM_ADMIN_IDS):")
            for user_id, name in users.items():
                print(f"  {user_id}  {name}")
        else:
            print("no messages yet — send /start to the bot, then re-run 'updates'")
        return 0
    elif args.action == "delete":
        result = _call(token, "deleteWebhook", {"drop_pending_updates": True})
    else:  # set
        if not args.url:
            print("error: 'set' needs the webhook URL", file=sys.stderr)
            return 2
        body = {"url": args.url, "allowed_updates": ["message", "edited_message"]}
        secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
        if secret:
            body["secret_token"] = secret
        result = _call(token, "setWebhook", body)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
