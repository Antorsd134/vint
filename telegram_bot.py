"""Telegram bot interface for the Vinted autoresponder.

Provides admin controls via aiogram 3.x:
- Cookie file upload (auto-assigns proxy, registers account)
- Deal notifications with inline keyboards
- Dynamic PayPal / IBAN input via FSM
- Manual reply forwarding
- /stats command for per-account and farm-wide statistics
"""

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

if TYPE_CHECKING:
    import aiohttp

    from browser_core import AccountSession

logger = logging.getLogger(__name__)

router = Router()

# ── Shared references (set by main.py at startup) ───────────

_bot: Bot | None = None
_config: dict[str, Any] = {}
_http_session: "aiohttp.ClientSession | None" = None
_account_sessions: dict[str, "AccountSession"] = {}


def set_shared_refs(
    bot: Bot,
    config: dict[str, Any],
    http_session: "aiohttp.ClientSession",
    account_sessions: dict[str, "AccountSession"],
) -> None:
    """Store references that handlers need."""
    global _bot, _config, _http_session  # noqa: PLW0603
    global _account_sessions  # noqa: PLW0603
    _bot = bot
    _config = config
    _http_session = http_session
    _account_sessions = account_sessions


# ── FSM states ───────────────────────────────────────────────


class DealReplyStates(StatesGroup):
    """FSM for handling deal payment flow."""

    waiting_paypal = State()
    waiting_card = State()
    waiting_manual = State()


# ── Helper: save config ─────────────────────────────────────


def _save_config() -> None:
    """Persist current config to disk."""
    try:
        Path("config.json").write_text(
            json.dumps(_config, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.error("Failed to save config: %s", exc)


# ── /start and /help ────────────────────────────────────────


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    """Handle /start command."""
    admin_id = _config.get("admin_chat_id", "")
    if str(message.chat.id) != str(admin_id):
        await message.answer("⛔ Нет доступа.")
        return

    await message.answer(
        "🤖 Vinted Autoresponder Bot\n\n"
        "Команды:\n"
        "/stats — статистика по аккаунтам\n"
        "/help — справка\n\n"
        "Загрузите .json файл с куки для "
        "добавления нового аккаунта."
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """Handle /help command."""
    admin_id = _config.get("admin_chat_id", "")
    if str(message.chat.id) != str(admin_id):
        return

    await message.answer(
        "📖 Справка по боту\n\n"
        "1. Отправьте .json файл с куки Vinted — "
        "бот создаст новый профиль и начнёт мониторинг.\n"
        "2. При обнаружении сделки бот пришлёт "
        "уведомление с кнопками:\n"
        "   • [PayPal] — запросит email для PayPal\n"
        "   • [Карта/IBAN] — запросит реквизиты карты\n"
        "   • [Вручную] — ввести ответ вручную\n"
        "3. /stats — общая и поаккаунтная статистика\n\n"
        "Gemini запоминает стиль общения каждого "
        "покупателя и адаптируется к нему."
    )


# ── /stats ───────────────────────────────────────────────────


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    """Show per-account and total statistics."""
    admin_id = _config.get("admin_chat_id", "")
    if str(message.chat.id) != str(admin_id):
        return

    accounts = _config.get("accounts", {})
    if not accounts:
        await message.answer("📊 Нет активных аккаунтов.")
        return

    total_replies = 0
    total_deals = 0
    lines: list[str] = []

    for acc_id, acc_data in accounts.items():
        replies = acc_data.get("total_replies", 0)
        deals = acc_data.get("deals_count", 0)
        proxy = acc_data.get("proxy", "—")
        proxy_host = proxy.split(":")[0] if proxy else "—"
        total_replies += replies
        total_deals += deals
        lines.append(
            f"  • {acc_id}: {replies} ответов, "
            f"{deals} сделок (Прокси: {proxy_host})"
        )

    active = len(_account_sessions)
    text = (
        "📊 Общая статистика:\n"
        f"  Активных аккаунтов: {active}\n"
        f"  Отвечено сообщений: {total_replies}\n"
        f"  Сделок: {total_deals}\n"
        "---\n"
        + "\n".join(lines)
    )
    await message.answer(text)


# ── Cookie upload ────────────────────────────────────────────


@router.message(F.document)
async def handle_cookie_upload(message: Message) -> None:
    """Process uploaded cookie .json files."""
    admin_id = _config.get("admin_chat_id", "")
    if str(message.chat.id) != str(admin_id):
        return

    doc = message.document
    if not doc or not doc.file_name:
        return
    if not doc.file_name.endswith(".json"):
        await message.answer(
            "⚠️ Отправьте файл с расширением .json"
        )
        return

    # Download the file
    if not _bot:
        await message.answer("❌ Бот не инициализирован.")
        return

    file = await _bot.get_file(doc.file_id)
    if not file or not file.file_path:
        await message.answer("❌ Не удалось загрузить файл.")
        return

    cookies_dir = Path("data/cookies")
    cookies_dir.mkdir(parents=True, exist_ok=True)
    filename = doc.file_name
    dest = cookies_dir / filename
    await _bot.download_file(file.file_path, dest)

    # Find a free proxy
    from browser_core import load_proxies

    all_proxies = load_proxies("data/proxies.txt")
    used = {
        a.get("proxy", "")
        for a in _config.get("accounts", {}).values()
    }
    free = [p for p in all_proxies if p not in used]

    if not free:
        await message.answer(
            "⚠️ Нет свободных прокси в data/proxies.txt. "
            "Добавьте прокси и повторите."
        )
        return

    proxy = free[0]
    account_id = Path(filename).stem
    accounts = _config.setdefault("accounts", {})
    accounts[account_id] = {
        "proxy": proxy,
        "cookie_file": str(dest),
        "total_replies": 0,
        "deals_count": 0,
        "deals_pending": 0,
    }
    _save_config()

    logger.info(
        "New account added: %s (proxy: %s, cookies: %s)",
        account_id,
        proxy,
        filename,
    )

    await message.answer(
        f"✅ Аккаунт «{account_id}» добавлен!\n"
        f"Прокси: {proxy.split(':')[0]}\n"
        f"Куки: {filename}\n\n"
        "Мониторинг запустится автоматически."
    )


# ── Deal notification ────────────────────────────────────────


async def notify_deal(
    bot: Bot,
    admin_chat_id: int | str,
    account_id: str,
    conversation_id: str,
    buyer_name: str,
    buyer_message: str,
    item_title: str = "",
) -> None:
    """Send a deal-reached notification to the admin.

    Includes inline buttons for PayPal, Card/IBAN,
    and manual reply.
    """
    item_info = f"\nТовар: {item_title}" if item_title else ""
    text = (
        "🔥 Покупатель готов к сделке на Vinted!\n"
        f"Аккаунт: {account_id}\n"
        f"Покупатель: {buyer_name}{item_info}\n"
        f"Сообщение: {buyer_message}"
    )

    # Encode callback data with account + conversation IDs
    cb_prefix = f"{account_id}|{conversation_id}"

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💳 Отправить PayPal",
                    callback_data=f"deal_paypal|{cb_prefix}",
                ),
                InlineKeyboardButton(
                    text="🏦 Отправить Карту/IBAN",
                    callback_data=f"deal_card|{cb_prefix}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Ответить вручную",
                    callback_data=f"deal_manual|{cb_prefix}",
                ),
            ],
        ]
    )

    await bot.send_message(
        chat_id=int(admin_chat_id),
        text=text,
        reply_markup=keyboard,
    )


# ── Inline button callbacks ─────────────────────────────────


@router.callback_query(F.data.startswith("deal_paypal|"))
async def cb_deal_paypal(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Handle [Отправить PayPal] button."""
    parts = callback.data.split("|")  # type: ignore[union-attr]
    if len(parts) < 3:
        await callback.answer("❌ Ошибка данных")
        return

    account_id = parts[1]
    conversation_id = parts[2]

    await state.update_data(
        deal_account=account_id,
        deal_conversation=conversation_id,
    )
    await state.set_state(DealReplyStates.waiting_paypal)
    await callback.message.answer(  # type: ignore[union-attr]
        "📧 Введите PayPal email для отправки покупателю:"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("deal_card|"))
async def cb_deal_card(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Handle [Отправить Карту/IBAN] button."""
    parts = callback.data.split("|")  # type: ignore[union-attr]
    if len(parts) < 3:
        await callback.answer("❌ Ошибка данных")
        return

    account_id = parts[1]
    conversation_id = parts[2]

    await state.update_data(
        deal_account=account_id,
        deal_conversation=conversation_id,
    )
    await state.set_state(DealReplyStates.waiting_card)
    await callback.message.answer(  # type: ignore[union-attr]
        "🏦 Введите IBAN / реквизиты карты "
        "для отправки покупателю:"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("deal_manual|"))
async def cb_deal_manual(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Handle [Ответить вручную] button."""
    parts = callback.data.split("|")  # type: ignore[union-attr]
    if len(parts) < 3:
        await callback.answer("❌ Ошибка данных")
        return

    account_id = parts[1]
    conversation_id = parts[2]

    await state.update_data(
        deal_account=account_id,
        deal_conversation=conversation_id,
    )
    await state.set_state(DealReplyStates.waiting_manual)
    await callback.message.answer(  # type: ignore[union-attr]
        "✏️ Введите текст ответа покупателю:"
    )
    await callback.answer()


# ── FSM message handlers ────────────────────────────────────


@router.message(DealReplyStates.waiting_paypal)
async def handle_paypal_input(
    message: Message,
    state: FSMContext,
) -> None:
    """Receive PayPal email and send payment message."""
    data = await state.get_data()
    account_id = data.get("deal_account", "")
    conversation_id = data.get("deal_conversation", "")

    if not account_id or not conversation_id:
        await message.answer("❌ Данные сделки потеряны.")
        await state.clear()
        return

    paypal_email = message.text or ""
    if not paypal_email:
        await message.answer("⚠️ Отправьте email текстом.")
        return

    await message.answer("⏳ Генерирую сообщение и отправляю...")

    success = await _send_payment_to_buyer(
        account_id,
        conversation_id,
        "paypal",
        paypal_email,
    )

    if success:
        await message.answer(
            "PayPal отправлен покупателю."
        )
        _increment_deals(account_id)
    else:
        await message.answer(
            "❌ Не удалось отправить сообщение. "
            "Проверьте, что браузер активен."
        )

    _unpause_conversation(account_id, conversation_id)
    await state.clear()


@router.message(DealReplyStates.waiting_card)
async def handle_card_input(
    message: Message,
    state: FSMContext,
) -> None:
    """Receive IBAN/card details and send payment message."""
    data = await state.get_data()
    account_id = data.get("deal_account", "")
    conversation_id = data.get("deal_conversation", "")

    if not account_id or not conversation_id:
        await message.answer("❌ Данные сделки потеряны.")
        await state.clear()
        return

    card_data = message.text or ""
    if not card_data:
        await message.answer("⚠️ Отправьте реквизиты текстом.")
        return

    await message.answer("⏳ Генерирую сообщение и отправляю...")

    success = await _send_payment_to_buyer(
        account_id,
        conversation_id,
        "card",
        card_data,
    )

    if success:
        await message.answer(
            "IBAN отправлен покупателю."
        )
        _increment_deals(account_id)
    else:
        await message.answer(
            "❌ Не удалось отправить сообщение. "
            "Проверьте, что браузер активен."
        )

    _unpause_conversation(account_id, conversation_id)
    await state.clear()


@router.message(DealReplyStates.waiting_manual)
async def handle_manual_input(
    message: Message,
    state: FSMContext,
) -> None:
    """Receive manual text and forward to buyer."""
    data = await state.get_data()
    account_id = data.get("deal_account", "")
    conversation_id = data.get("deal_conversation", "")

    if not account_id or not conversation_id:
        await message.answer("❌ Данные сделки потеряны.")
        await state.clear()
        return

    manual_text = message.text or ""
    if not manual_text:
        await message.answer("⚠️ Отправьте текст сообщения.")
        return

    await message.answer("⏳ Отправляю сообщение покупателю...")

    success = await _send_manual_to_buyer(
        account_id,
        conversation_id,
        manual_text,
    )

    if success:
        await message.answer(
            "Сообщение отправлено покупателю."
        )
    else:
        await message.answer(
            "❌ Не удалось отправить. "
            "Проверьте, что браузер активен."
        )

    _unpause_conversation(account_id, conversation_id)
    await state.clear()


# ── Internal helpers ─────────────────────────────────────────


async def _send_payment_to_buyer(
    account_id: str,
    conversation_id: str,
    payment_type: str,
    payment_data: str,
) -> bool:
    """Generate a payment message via Gemini and send it."""
    from browser_core import open_conversation, send_message
    from llm_handler import generate_payment_message

    acc = _account_sessions.get(account_id)
    if not acc or not acc.page:
        logger.error(
            "Account %s not connected for payment send",
            account_id,
        )
        return False

    # Generate natural message via Gemini
    text = await generate_payment_message(
        conversation_id,
        payment_type,
        payment_data,
    )

    # Navigate to conversation and send
    inbox_url = _config.get(
        "vinted_inbox_url", "https://www.vinted.de/inbox"
    )
    await open_conversation(
        acc.page, conversation_id, inbox_url
    )

    typing_min = _config.get("typing_delay_min", 60)
    typing_max = _config.get("typing_delay_max", 140)
    return await send_message(
        acc.page, text, typing_min, typing_max
    )


async def _send_manual_to_buyer(
    account_id: str,
    conversation_id: str,
    text: str,
) -> bool:
    """Send a manually typed message to the buyer."""
    from browser_core import open_conversation, send_message
    from llm_handler import add_to_history

    acc = _account_sessions.get(account_id)
    if not acc or not acc.page:
        logger.error(
            "Account %s not connected for manual send",
            account_id,
        )
        return False

    inbox_url = _config.get(
        "vinted_inbox_url", "https://www.vinted.de/inbox"
    )
    await open_conversation(
        acc.page, conversation_id, inbox_url
    )

    typing_min = _config.get("typing_delay_min", 60)
    typing_max = _config.get("typing_delay_max", 140)
    success = await send_message(
        acc.page, text, typing_min, typing_max
    )

    if success:
        add_to_history(conversation_id, "model", text)

    return success


def _unpause_conversation(
    account_id: str,
    conversation_id: str,
) -> None:
    """Resume auto-replies for a paused conversation."""
    acc = _account_sessions.get(account_id)
    if acc and conversation_id in acc.conversations:
        acc.conversations[conversation_id].paused = False
        logger.info(
            "Unpaused conversation %s on account %s",
            conversation_id,
            account_id,
        )


def _increment_deals(account_id: str) -> None:
    """Increment the deal counter in config."""
    accounts = _config.get("accounts", {})
    if account_id in accounts:
        accounts[account_id]["deals_count"] = (
            accounts[account_id].get("deals_count", 0) + 1
        )
        pending = accounts[account_id].get("deals_pending", 0)
        accounts[account_id]["deals_pending"] = max(
            0, pending - 1
        )
        _save_config()


def create_dispatcher() -> Dispatcher:
    """Create and configure the aiogram dispatcher."""
    dp = Dispatcher()
    dp.include_router(router)
    return dp
