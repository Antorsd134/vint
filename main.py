"""Entry point for the Vinted Autoresponder.

Starts the Telegram bot and per-account browser monitoring
loops concurrently using asyncio.
"""

import asyncio
import json
import logging
import random
import sys
from pathlib import Path

import aiohttp
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from dotenv import load_dotenv

load_dotenv()

import os  # noqa: E402  (after load_dotenv so env is ready)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


# ── Config helpers ───────────────────────────────────────────


def load_config() -> dict:
    """Load config.json or abort if missing."""
    path = Path("config.json")
    if not path.exists():
        logger.error(
            "config.json not found. "
            "Copy config.example.json and fill in settings."
        )
        sys.exit(1)
    return json.loads(path.read_text(encoding="utf-8"))


def save_config(config: dict) -> None:
    """Persist config to disk."""
    Path("config.json").write_text(
        json.dumps(config, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ── Per-account monitoring loop ─────────────────────────────


async def monitor_account(
    config: dict,
    http_session: aiohttp.ClientSession,
    bot: Bot,
    account_id: str,
    account_sessions: dict,
) -> None:
    """Run the infinite monitoring loop for one account.

    1. Connect browser via Vision CDP.
    2. Navigate to Vinted inbox.
    3. Check for unread conversations.
    4. For each new message, classify with Gemini.
    5. Auto-reply or notify admin on deal.
    6. Wait random interval, then repeat.
    """
    from browser_core import (
        AccountSession,
        ConversationState,
        connect_account,
        disconnect_account,
        get_unread_conversations,
        navigate_to_inbox,
        open_conversation,
        send_message,
    )
    from llm_handler import classify_and_reply
    from telegram_bot import notify_deal

    acc_data = config.get("accounts", {}).get(account_id, {})
    proxy = acc_data.get("proxy", "")
    cookie_file = acc_data.get("cookie_file", "")
    vision_url = config.get(
        "vision_api_url", "http://localhost:3000"
    )
    inbox_url = config.get(
        "vinted_inbox_url", "https://www.vinted.de/inbox"
    )
    admin_chat_id = os.getenv(
        "ADMIN_CHAT_ID",
        config.get("admin_chat_id", ""),
    )
    check_min = config.get("check_interval_min", 45)
    check_max = config.get("check_interval_max", 90)
    typing_min = config.get("typing_delay_min", 60)
    typing_max = config.get("typing_delay_max", 140)

    acc: AccountSession | None = None

    while True:
        try:
            # Connect if not yet connected
            if acc is None or acc.page is None:
                logger.info(
                    "Connecting account %s...", account_id
                )
                acc = await connect_account(
                    http_session,
                    account_id,
                    proxy,
                    cookie_file,
                    vision_url,
                )
                account_sessions[account_id] = acc

            page = acc.page
            if page is None:
                raise RuntimeError("Page is None after connect")

            # Navigate to inbox
            await navigate_to_inbox(page, inbox_url)

            # Get conversations
            convs = await get_unread_conversations(page)
            unread = [
                c for c in convs if c.get("unread") == "true"
            ]

            for conv_info in unread:
                conv_id = conv_info["conversation_id"]
                buyer_name = conv_info.get(
                    "buyer_name", "Unknown"
                )
                item_title = conv_info.get("item_title", "")

                # Check if conversation is paused
                if conv_id in acc.conversations:
                    cs = acc.conversations[conv_id]
                    if cs.paused:
                        logger.debug(
                            "Skipping paused conv %s", conv_id
                        )
                        continue
                else:
                    acc.conversations[conv_id] = (
                        ConversationState(
                            conversation_id=conv_id,
                            buyer_name=buyer_name,
                            item_title=item_title,
                        )
                    )

                # Open the conversation
                last_msg = await open_conversation(
                    page, conv_id, inbox_url
                )

                if not last_msg:
                    continue

                cs = acc.conversations[conv_id]

                # Skip if we already replied to this message
                if last_msg == cs.last_message:
                    continue
                cs.last_message = last_msg

                logger.info(
                    "[%s] New message from %s: %s",
                    account_id,
                    buyer_name,
                    last_msg[:80],
                )

                # Classify with Gemini
                result = await classify_and_reply(
                    conv_id, last_msg, item_title
                )

                status = result.get("status", "reply")

                if status == "deal_reached":
                    # Pause conversation and notify admin
                    cs.paused = True
                    acc_cfg = config.get(
                        "accounts", {}
                    ).get(account_id, {})
                    acc_cfg["deals_pending"] = (
                        acc_cfg.get("deals_pending", 0) + 1
                    )
                    save_config(config)

                    logger.info(
                        "[%s] Deal reached with %s!",
                        account_id,
                        buyer_name,
                    )

                    if admin_chat_id:
                        await notify_deal(
                            bot,
                            admin_chat_id,
                            account_id,
                            conv_id,
                            buyer_name,
                            last_msg,
                            item_title,
                        )
                else:
                    # Auto-reply
                    reply_text = result.get("text", "")
                    if reply_text:
                        sent = await send_message(
                            page,
                            reply_text,
                            typing_min,
                            typing_max,
                        )
                        if sent:
                            cs.reply_count += 1
                            acc.total_replies += 1
                            acc_cfg = config.get(
                                "accounts", {}
                            ).get(account_id, {})
                            acc_cfg["total_replies"] = (
                                acc_cfg.get(
                                    "total_replies", 0
                                )
                                + 1
                            )
                            save_config(config)
                            logger.info(
                                "[%s] Replied to %s (%d chars)",
                                account_id,
                                buyer_name,
                                len(reply_text),
                            )

            # Wait before next check
            wait_time = random.uniform(check_min, check_max)
            logger.info(
                "[%s] Next check in %.0f seconds",
                account_id,
                wait_time,
            )
            await asyncio.sleep(wait_time)

        except asyncio.CancelledError:
            logger.info(
                "Monitoring cancelled for %s", account_id
            )
            break

        except Exception as exc:
            logger.error(
                "[%s] Monitoring error: %s", account_id, exc,
                exc_info=True,
            )
            # Try to disconnect and reconnect
            if acc is not None:
                try:
                    await disconnect_account(
                        http_session, acc, vision_url
                    )
                except Exception:
                    pass
                acc = None
                account_sessions.pop(account_id, None)

            # Wait before retry
            retry_wait = random.uniform(30, 60)
            logger.info(
                "[%s] Retrying in %.0f seconds",
                account_id,
                retry_wait,
            )
            await asyncio.sleep(retry_wait)


# ── Account watcher ──────────────────────────────────────────


async def watch_new_accounts(
    config: dict,
    http_session: aiohttp.ClientSession,
    bot: Bot,
    monitoring_tasks: dict,
    account_sessions: dict,
) -> None:
    """Periodically check config for newly added accounts."""
    while True:
        try:
            accounts = config.get("accounts", {})
            for acc_id in accounts:
                if acc_id not in monitoring_tasks:
                    logger.info(
                        "Starting monitoring for new "
                        "account: %s",
                        acc_id,
                    )
                    task = asyncio.create_task(
                        monitor_account(
                            config,
                            http_session,
                            bot,
                            acc_id,
                            account_sessions,
                        ),
                        name=f"monitor_{acc_id}",
                    )
                    monitoring_tasks[acc_id] = task
        except Exception as exc:
            logger.error("Account watcher error: %s", exc)

        await asyncio.sleep(10)


# ── Main ─────────────────────────────────────────────────────


async def main() -> None:
    """Application entry point."""
    config = load_config()

    bot_token = os.getenv(
        "BOT_TOKEN", config.get("bot_token", "")
    )
    if not bot_token:
        logger.error(
            "BOT_TOKEN not set. "
            "Add it to .env or config.json."
        )
        sys.exit(1)

    bot = Bot(
        token=bot_token,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
        ),
    )

    from telegram_bot import create_dispatcher, set_shared_refs

    dp = create_dispatcher()

    async with aiohttp.ClientSession() as http_session:
        monitoring_tasks: dict[str, asyncio.Task] = {}
        account_sessions: dict = {}

        set_shared_refs(
            bot, config, http_session, account_sessions
        )

        # Start monitoring for existing accounts
        for acc_id in config.get("accounts", {}):
            task = asyncio.create_task(
                monitor_account(
                    config,
                    http_session,
                    bot,
                    acc_id,
                    account_sessions,
                ),
                name=f"monitor_{acc_id}",
            )
            monitoring_tasks[acc_id] = task

        # Start the account watcher
        asyncio.create_task(
            watch_new_accounts(
                config,
                http_session,
                bot,
                monitoring_tasks,
                account_sessions,
            ),
            name="account_watcher",
        )

        logger.info(
            "Starting Telegram bot polling "
            "(%d accounts configured)",
            len(config.get("accounts", {})),
        )

        try:
            await dp.start_polling(bot)
        finally:
            # Clean up all browser sessions
            vision_url = config.get(
                "vision_api_url", "http://localhost:3000"
            )
            for acc_id, task in monitoring_tasks.items():
                task.cancel()
            for acc in account_sessions.values():
                try:
                    from browser_core import disconnect_account
                    await disconnect_account(
                        http_session, acc, vision_url
                    )
                except Exception:
                    pass

            await bot.session.close()
            logger.info("Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
