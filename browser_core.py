"""Vision anti-detect browser integration + Vinted inbox parsing.

Handles Vision API profile lifecycle (create / start / stop),
Playwright CDP connections, and all Vinted page interactions
including reading conversations and sending messages with
human-like typing.
"""

import asyncio
import json
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
from playwright.async_api import Browser, BrowserContext, Page

logger = logging.getLogger(__name__)


# ── Data classes ─────────────────────────────────────────────


@dataclass
class ConversationState:
    """Tracks per-conversation state."""

    conversation_id: str
    buyer_name: str = ""
    item_title: str = ""
    last_message: str = ""
    reply_count: int = 0
    paused: bool = False


@dataclass
class AccountSession:
    """Holds browser session data for one Vinted account."""

    account_id: str
    proxy: str = ""
    cookie_file: str = ""
    vision_profile_id: str = ""
    browser: Browser | None = None
    context: BrowserContext | None = None
    page: Page | None = None
    conversations: dict[str, ConversationState] = field(
        default_factory=dict,
    )
    total_replies: int = 0


# ── Proxy helpers ────────────────────────────────────────────


def parse_proxy(proxy_line: str) -> dict[str, str]:
    """Parse ``ip:port:login:password`` into a dict."""
    parts = proxy_line.strip().split(":")
    if len(parts) != 4:
        raise ValueError(
            "Invalid proxy format "
            f"(expected ip:port:login:pass): "
            f"{proxy_line!r}"
        )
    return {
        "host": parts[0],
        "port": parts[1],
        "login": parts[2],
        "password": parts[3],
    }


def load_proxies(
    proxies_file: str = "data/proxies.txt",
) -> list[str]:
    """Load proxy lines from file, skip blanks/comments."""
    path = Path(proxies_file)
    if not path.exists():
        logger.warning("Proxies file not found: %s", proxies_file)
        return []
    lines: list[str] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


# ── Vision API helpers ───────────────────────────────────────


async def create_vision_profile(
    session: aiohttp.ClientSession,
    vision_api_url: str,
    proxy_line: str,
    profile_name: str = "",
) -> str:
    """Create a new Vision browser profile with proxy.

    Returns the profile ID.
    """
    proxy = parse_proxy(proxy_line)
    create_url = (
        f"{vision_api_url.rstrip('/')}/api/v1/profile/create"
    )

    payload: dict[str, Any] = {
        "name": profile_name or f"vinted_{proxy['host']}",
        "proxy": {
            "type": "http",
            "host": proxy["host"],
            "port": int(proxy["port"]),
            "login": proxy["login"],
            "password": proxy["password"],
        },
        "os": "win",
        "browser": "chrome",
    }

    async with session.post(create_url, json=payload) as resp:
        if resp.status not in (200, 201):
            body = await resp.text()
            raise RuntimeError(
                "Vision profile creation failed "
                f"({resp.status}): {body[:300]}"
            )
        data = await resp.json()
        profile_id = (
            data.get("id")
            or data.get("profile_id")
            or data.get("uuid", "")
        )
        if not profile_id:
            raise RuntimeError(
                f"Vision returned no profile ID: {data}"
            )
        logger.info(
            "Created Vision profile %s for proxy %s",
            profile_id,
            proxy["host"],
        )
        return str(profile_id)


async def start_vision_profile(
    session: aiohttp.ClientSession,
    vision_api_url: str,
    profile_id: str,
) -> str:
    """Start a Vision profile and return the WS debugger URL."""
    start_url = (
        f"{vision_api_url.rstrip('/')}"
        f"/api/v1/profile/start/{profile_id}"
    )

    async with session.get(start_url) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise RuntimeError(
                "Vision profile start failed "
                f"({resp.status}): {body[:300]}"
            )
        data = await resp.json()
        ws_url = (
            data.get("wsEndpoint")
            or data.get("ws", {}).get("puppeteer", "")
        )
        if not ws_url:
            for key in ("automation", "webSocket", "debugger"):
                candidate = data.get("ws", {}).get(key, "")
                if candidate:
                    ws_url = candidate
                    break
        if not ws_url:
            raise RuntimeError(
                "No websocketDebuggerUrl in Vision "
                f"response: {data}"
            )
        logger.info(
            "Vision profile %s started, ws=%s",
            profile_id,
            ws_url[:80],
        )
        return ws_url


async def stop_vision_profile(
    session: aiohttp.ClientSession,
    vision_api_url: str,
    profile_id: str,
) -> None:
    """Stop a running Vision browser profile."""
    stop_url = (
        f"{vision_api_url.rstrip('/')}"
        f"/api/v1/profile/stop/{profile_id}"
    )
    try:
        async with session.get(stop_url) as resp:
            logger.info(
                "Stopped Vision profile %s (status %d)",
                profile_id,
                resp.status,
            )
    except Exception as exc:
        logger.warning(
            "Error stopping Vision profile %s: %s",
            profile_id,
            exc,
        )


# ── Cookie injection ────────────────────────────────────────


async def inject_cookies(
    context: BrowserContext,
    cookie_file: str,
) -> None:
    """Load cookies from a JSON file into the browser."""
    path = Path(cookie_file)
    if not path.exists():
        logger.warning("Cookie file not found: %s", cookie_file)
        return

    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        logger.error(
            "Invalid JSON in cookie file %s: %s",
            cookie_file,
            exc,
        )
        return

    cookies: list[dict[str, Any]] = []
    items = raw if isinstance(raw, list) else raw.get("cookies", [])
    for c in items:
        cookie: dict[str, Any] = {
            "name": c.get("name", ""),
            "value": c.get("value", ""),
            "domain": c.get("domain", ".vinted.de"),
            "path": c.get("path", "/"),
        }
        if c.get("expirationDate"):
            cookie["expires"] = float(c["expirationDate"])
        if c.get("sameSite"):
            val = str(c["sameSite"]).capitalize()
            if val in ("Strict", "Lax", "None"):
                cookie["sameSite"] = val
        cookie["httpOnly"] = bool(c.get("httpOnly", False))
        cookie["secure"] = bool(c.get("secure", False))
        cookies.append(cookie)

    if cookies:
        await context.add_cookies(cookies)
        logger.info(
            "Injected %d cookies from %s",
            len(cookies),
            cookie_file,
        )


# ── Human-like helpers ───────────────────────────────────────


async def human_type(
    page: Page,
    selector: str,
    text: str,
    delay_min: int = 60,
    delay_max: int = 140,
) -> None:
    """Type text character by character with random delays."""
    await page.click(selector)
    await asyncio.sleep(random.uniform(0.3, 0.8))
    await page.type(
        selector,
        text,
        delay=random.randint(delay_min, delay_max),
    )


async def random_pause(
    min_sec: float = 1.0,
    max_sec: float = 3.0,
) -> None:
    """Sleep for a random duration."""
    await asyncio.sleep(random.uniform(min_sec, max_sec))


# ── Vinted page interactions ─────────────────────────────────


async def navigate_to_inbox(
    page: Page,
    inbox_url: str = "https://www.vinted.de/inbox",
) -> None:
    """Navigate to the Vinted inbox page."""
    await page.goto(inbox_url, wait_until="domcontentloaded")
    await random_pause(2.0, 5.0)

    # Accept cookie/consent banner if present
    try:
        consent_btn = page.locator(
            "#onetrust-accept-btn-handler, "
            "[data-testid='cookie-consent-accept'], "
            "button:has-text('Alle akzeptieren')"
        )
        if await consent_btn.first.is_visible(timeout=3000):
            await consent_btn.first.click()
            await random_pause(1.0, 2.0)
    except Exception:
        pass


async def get_unread_conversations(
    page: Page,
) -> list[dict[str, str]]:
    """Parse the Vinted inbox for conversations with new messages.

    Returns a list of dicts with keys:
    - ``conversation_id``: unique identifier
    - ``buyer_name``: display name
    - ``preview``: last message preview text
    - ``item_title``: item name if available
    - ``unread``: whether the conversation has unread messages

    .. note::
        Vinted's DOM structure may change. The selectors
        below are best-effort and may need adjustment.
        Look for ``TODO`` comments for tuning points.
    """
    conversations: list[dict[str, str]] = []

    await random_pause(1.0, 2.0)

    try:
        # Wait for inbox message list to appear
        # TODO: adjust selector to match current Vinted DOM
        await page.wait_for_selector(
            "[class*='inbox'], "
            "[class*='Inbox'], "
            "[data-testid='inbox-list'], "
            "[class*='MessageList'], "
            "[class*='conversation-list']",
            timeout=15000,
        )
    except Exception:
        logger.warning(
            "Inbox list not found — page may not have loaded"
        )
        return conversations

    # Gather conversation items
    # TODO: adjust selector for conversation list items
    item_selectors = [
        "[data-testid='conversation-item']",
        "[class*='Cell__Cell']",
        "[class*='inbox-thread']",
        "[class*='conversation-list'] > div",
        "[class*='InboxList'] li",
        "[class*='MessageThread']",
    ]

    items = []
    for sel in item_selectors:
        items = await page.query_selector_all(sel)
        if items:
            logger.debug(
                "Found %d conversations with selector: %s",
                len(items),
                sel,
            )
            break

    if not items:
        logger.debug("No conversation items found in inbox")
        return conversations

    for item in items:
        try:
            # Check for unread indicator
            # TODO: adjust unread badge/dot selector
            unread_el = await item.query_selector(
                "[class*='unread'], "
                "[class*='Unread'], "
                "[class*='badge'], "
                "[class*='dot'], "
                "[class*='new-message']"
            )
            is_unread = unread_el is not None

            # Extract buyer name
            # TODO: adjust username selector
            name_sel = (
                "[class*='username'], "
                "[class*='UserName'], "
                "[class*='title'], "
                "[data-testid*='user']"
            )
            name_el = await item.query_selector(name_sel)
            buyer_name = (
                (await name_el.inner_text()).strip()
                if name_el
                else "Unknown"
            )

            # Extract message preview
            # TODO: adjust preview text selector
            preview_sel = (
                "[class*='preview'], "
                "[class*='Preview'], "
                "[class*='body'], "
                "[class*='subtitle'], "
                "[class*='last-message']"
            )
            preview_el = await item.query_selector(preview_sel)
            preview = (
                (await preview_el.inner_text()).strip()
                if preview_el
                else ""
            )

            # Extract item title if visible
            # TODO: adjust item title selector
            title_sel = (
                "[class*='item-title'], "
                "[class*='ItemTitle'], "
                "[class*='product-title']"
            )
            title_el = await item.query_selector(title_sel)
            item_title = (
                (await title_el.inner_text()).strip()
                if title_el
                else ""
            )

            # Build a conversation ID from link or element
            conv_id = ""
            link_el = await item.query_selector("a[href]")
            if link_el:
                href = await link_el.get_attribute("href")
                if href:
                    # e.g. /inbox/12345
                    parts = href.rstrip("/").split("/")
                    conv_id = parts[-1] if parts else ""

            if not conv_id:
                conv_id = f"conv_{buyer_name}_{id(item)}"

            conversations.append({
                "conversation_id": conv_id,
                "buyer_name": buyer_name,
                "preview": preview,
                "item_title": item_title,
                "unread": "true" if is_unread else "false",
            })

        except Exception as exc:
            logger.debug(
                "Error parsing conversation item: %s", exc
            )
            continue

    unread_count = sum(
        1 for c in conversations if c["unread"] == "true"
    )
    logger.info(
        "Found %d conversations (%d unread)",
        len(conversations),
        unread_count,
    )
    return conversations


async def open_conversation(
    page: Page,
    conversation_id: str,
    inbox_url: str = "https://www.vinted.de/inbox",
) -> str:
    """Open a specific conversation and return the last message.

    Navigates to the conversation page and extracts the most
    recent buyer message.
    """
    conv_url = f"{inbox_url.rstrip('/')}/{conversation_id}"
    await page.goto(conv_url, wait_until="domcontentloaded")
    await random_pause(2.0, 4.0)

    # Wait for messages to load
    # TODO: adjust message container selector
    try:
        await page.wait_for_selector(
            "[class*='message'], "
            "[class*='Message'], "
            "[data-testid*='message'], "
            "[class*='ChatMessage']",
            timeout=10000,
        )
    except Exception:
        logger.warning(
            "Messages not loaded for conversation %s",
            conversation_id,
        )
        return ""

    # Find all messages in the conversation
    # TODO: adjust individual message selectors
    msg_selectors = [
        "[data-testid='message-content']",
        "[class*='MessageBody']",
        "[class*='message-text']",
        "[class*='ChatMessage'] [class*='content']",
        "[class*='msg-body']",
    ]

    messages = []
    for sel in msg_selectors:
        messages = await page.query_selector_all(sel)
        if messages:
            break

    if not messages:
        logger.debug(
            "No messages found in conversation %s",
            conversation_id,
        )
        return ""

    # Get the last message (most recent)
    last_msg_el = messages[-1]
    try:
        last_text = (await last_msg_el.inner_text()).strip()
    except Exception:
        last_text = ""

    # Check if the last message is from the buyer (not us)
    # TODO: adjust sender indicator selectors
    try:
        parent = await last_msg_el.evaluate_handle(
            "el => el.closest('[class*=\"message\"]')"
        )
        if parent:
            class_attr = await parent.evaluate(
                "el => el.className || ''"
            )
            # If the message container has "own" or "self"
            # class, it's ours
            class_lower = str(class_attr).lower()
            if "own" in class_lower or "self" in class_lower:
                logger.debug(
                    "Last message is ours in conv %s, skip",
                    conversation_id,
                )
                return ""
    except Exception:
        pass

    return last_text


async def send_message(
    page: Page,
    text: str,
    typing_delay_min: int = 60,
    typing_delay_max: int = 140,
) -> bool:
    """Type and send a message in the currently open conversation.

    Returns True if the message was sent successfully.
    """
    # Find the message input field
    # TODO: adjust input selector for Vinted chat
    input_selectors = [
        "textarea[data-testid='message-input']",
        "textarea[class*='message']",
        "textarea[class*='Message']",
        "[class*='ChatInput'] textarea",
        "[class*='input'] textarea",
        "textarea[placeholder]",
        "[contenteditable='true']",
    ]

    input_el = None
    for sel in input_selectors:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=2000):
                input_el = sel
                break
        except Exception:
            continue

    if not input_el:
        logger.error("Message input field not found")
        return False

    try:
        # Simulate human-like focus + pause
        await random_pause(0.5, 1.5)
        await human_type(
            page,
            input_el,
            text,
            typing_delay_min,
            typing_delay_max,
        )
        await random_pause(0.3, 1.0)

        # Find and click the send button
        # TODO: adjust send button selector
        send_selectors = [
            "button[data-testid='send-message']",
            "button[class*='send']",
            "button[class*='Send']",
            "[class*='ChatInput'] button[type='submit']",
            "button[aria-label*='send' i]",
            "button[aria-label*='Send' i]",
            "form button[type='submit']",
        ]

        sent = False
        for sel in send_selectors:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    sent = True
                    break
            except Exception:
                continue

        if not sent:
            # Try pressing Enter as a fallback
            await page.keyboard.press("Enter")
            sent = True

        await random_pause(1.0, 2.0)
        logger.info("Message sent (%d chars)", len(text))
        return sent

    except Exception as exc:
        logger.error("Failed to send message: %s", exc)
        return False


# ── Account lifecycle ────────────────────────────────────────


async def connect_account(
    session: aiohttp.ClientSession,
    account_id: str,
    proxy_line: str,
    cookie_file: str,
    vision_api_url: str,
) -> AccountSession:
    """Set up a full browser session for one Vinted account.

    1. Create or re-use a Vision profile.
    2. Start the profile and get the WS endpoint.
    3. Connect Playwright via CDP.
    4. Inject cookies.
    """
    from playwright.async_api import async_playwright

    acc = AccountSession(
        account_id=account_id,
        proxy=proxy_line,
        cookie_file=cookie_file,
    )

    # Create Vision profile
    profile_id = await create_vision_profile(
        session,
        vision_api_url,
        proxy_line,
        profile_name=f"vinted_{account_id}",
    )
    acc.vision_profile_id = profile_id

    # Start profile and get WS URL
    ws_url = await start_vision_profile(
        session, vision_api_url, profile_id
    )

    # Connect Playwright
    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp(ws_url)
    acc.browser = browser

    contexts = browser.contexts
    if contexts:
        acc.context = contexts[0]
    else:
        acc.context = await browser.new_context()

    pages = acc.context.pages
    if pages:
        acc.page = pages[0]
    else:
        acc.page = await acc.context.new_page()

    # Inject cookies
    if cookie_file:
        await inject_cookies(acc.context, cookie_file)

    logger.info(
        "Account %s connected (profile=%s, proxy=%s)",
        account_id,
        profile_id,
        proxy_line.split(":")[0],
    )
    return acc


async def disconnect_account(
    session: aiohttp.ClientSession,
    acc: AccountSession,
    vision_api_url: str,
) -> None:
    """Close browser and stop Vision profile."""
    try:
        if acc.browser:
            await acc.browser.close()
    except Exception as exc:
        logger.warning(
            "Error closing browser for %s: %s",
            acc.account_id,
            exc,
        )

    if acc.vision_profile_id:
        await stop_vision_profile(
            session, vision_api_url, acc.vision_profile_id
        )

    logger.info("Account %s disconnected", acc.account_id)
