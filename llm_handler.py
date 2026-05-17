"""Google Gemini integration for Vinted autoresponder.

Uses gemini-1.5-flash with strict JSON mode for intent detection
and automatic reply generation. Maintains per-conversation history
so the model adapts to each buyer's communication style.
"""

import json
import logging
import os
from collections import defaultdict
from typing import Any

import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Du bist ein privater Verkäufer auf Vinted. "
    "Du verkaufst gebrauchte Kleidung/Sachen. "
    "Analysiere die Nachricht des Käufers. "
    "Antworte immer kurz, freundlich, im Chat-Stil "
    "(1-2 Sätze) auf Deutsch "
    "(oder auf der Sprache des Käufers). "
    "Wenn der Käufer Fragen stellt, verhandelt oder "
    "nach Versandkosten fragt, antworte passend und "
    'gib im JSON aus: {"status": "reply", '
    '"text": "deine Antwort"}. '
    "Wenn der Käufer explizit sagt, dass er den Artikel "
    "kaufen möchte, nach den Bankdaten/PayPal fragt oder "
    "die Transaktion abschließen will, gib im JSON aus: "
    '{"status": "deal_reached", '
    '"payment_method_guess": "paypal" oder "card"}.'
)

# Per-conversation chat history: conv_id -> list of messages
_conversation_history: dict[str, list[dict[str, str]]] = defaultdict(list)

_model: Any = None


def _get_model() -> Any:
    """Lazily initialise the Gemini model."""
    global _model  # noqa: PLW0603
    if _model is not None:
        return _model

    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY not set. "
            "Add it to your .env file."
        )

    genai.configure(api_key=api_key)
    _model = genai.GenerativeModel(
        model_name="gemini-1.5-flash",
        system_instruction=SYSTEM_PROMPT,
        generation_config={
            "response_mime_type": "application/json",
            "temperature": 0.7,
            "max_output_tokens": 512,
        },
    )
    logger.info("Gemini model initialised (gemini-1.5-flash)")
    return _model


def _build_chat_history(
    conversation_id: str,
) -> list[dict[str, Any]]:
    """Build the Gemini-compatible history list."""
    history: list[dict[str, Any]] = []
    for msg in _conversation_history[conversation_id]:
        role = msg["role"]
        history.append({
            "role": role,
            "parts": [msg["text"]],
        })
    return history


def add_to_history(
    conversation_id: str,
    role: str,
    text: str,
) -> None:
    """Append a message to conversation memory.

    Parameters
    ----------
    conversation_id:
        Unique identifier for the conversation.
    role:
        Either ``"user"`` (buyer message) or ``"model"``
        (our reply).
    text:
        The message text.
    """
    _conversation_history[conversation_id].append({
        "role": role,
        "text": text,
    })
    # Keep last 40 messages to avoid token overflow
    if len(_conversation_history[conversation_id]) > 40:
        _conversation_history[conversation_id] = (
            _conversation_history[conversation_id][-40:]
        )


def clear_history(conversation_id: str) -> None:
    """Remove all stored messages for a conversation."""
    _conversation_history.pop(conversation_id, None)


def get_history_length(conversation_id: str) -> int:
    """Return the number of stored messages."""
    return len(_conversation_history.get(conversation_id, []))


async def classify_and_reply(
    conversation_id: str,
    buyer_message: str,
    item_title: str = "",
) -> dict[str, str]:
    """Send *buyer_message* to Gemini and return parsed JSON.

    The model receives the full conversation history so it can
    adapt to the buyer's tone and vocabulary over time.

    Returns
    -------
    dict
        ``{"status": "reply", "text": "..."}`` or
        ``{"status": "deal_reached",
           "payment_method_guess": "paypal"|"card"}``.
    """
    model = _get_model()

    # Store the buyer message in history
    context_prefix = ""
    if item_title:
        context_prefix = (
            f"[Artikelname: {item_title}] "
        )
    user_text = f"{context_prefix}{buyer_message}"
    add_to_history(conversation_id, "user", user_text)

    # Build chat with history
    history = _build_chat_history(conversation_id)
    # Remove the last user message from history because
    # we pass it as send_message content
    if history and history[-1]["role"] == "user":
        history = history[:-1]

    try:
        chat = model.start_chat(history=history)
        response = chat.send_message(user_text)
        raw = response.text.strip()
    except Exception as exc:
        logger.error(
            "Gemini API error for conv %s: %s",
            conversation_id,
            exc,
        )
        # Remove the failed user message from history
        if _conversation_history[conversation_id]:
            _conversation_history[conversation_id].pop()
        return {
            "status": "reply",
            "text": "Einen Moment bitte, ich melde mich gleich!",
        }

    parsed = _parse_json_response(raw)

    # Store the model reply in history
    model_text = parsed.get("text", raw)
    add_to_history(conversation_id, "model", model_text)

    return parsed


def _parse_json_response(raw: str) -> dict[str, str]:
    """Extract a valid JSON object from the model output.

    Handles cases where the model wraps JSON in markdown
    fences or adds extra text around the object.
    """
    # Direct parse
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and "status" in data:
            return data
    except json.JSONDecodeError:
        pass

    # Try to find JSON inside markdown fences
    for fence in ("```json", "```"):
        if fence in raw:
            start = raw.index(fence) + len(fence)
            end = raw.find("```", start)
            if end == -1:
                end = len(raw)
            snippet = raw[start:end].strip()
            try:
                data = json.loads(snippet)
                if isinstance(data, dict) and "status" in data:
                    return data
            except json.JSONDecodeError:
                pass

    # Try to find a JSON object with braces
    brace_start = raw.find("{")
    brace_end = raw.rfind("}")
    if brace_start != -1 and brace_end > brace_start:
        snippet = raw[brace_start : brace_end + 1]
        try:
            data = json.loads(snippet)
            if isinstance(data, dict) and "status" in data:
                return data
        except json.JSONDecodeError:
            pass

    logger.warning("Could not parse Gemini response: %s", raw[:200])
    return {
        "status": "reply",
        "text": "Einen Moment bitte, ich melde mich gleich!",
    }


async def generate_payment_message(
    conversation_id: str,
    payment_type: str,
    payment_data: str,
) -> str:
    """Generate a natural payment-info message.

    Parameters
    ----------
    conversation_id:
        Conversation identifier for context.
    payment_type:
        ``"paypal"`` or ``"card"``.
    payment_data:
        The PayPal email or IBAN value.

    Returns
    -------
    str
        Human-like message text ready to send.
    """
    # Ensure Gemini is configured
    _get_model()

    if payment_type == "paypal":
        prompt = (
            "Der Käufer möchte kaufen. "
            "Schreibe eine sehr kurze, freundliche Nachricht "
            "(1-2 Sätze), in der du sagst, dass er über PayPal "
            f"an {payment_data} bezahlen soll. "
            "Antworte NUR mit dem Nachrichtentext, kein JSON."
        )
    else:
        prompt = (
            "Der Käufer möchte kaufen. "
            "Schreibe eine sehr kurze, freundliche Nachricht "
            "(1-2 Sätze), in der du die Bankdaten/IBAN "
            f"mitteilst: {payment_data}. "
            "Antworte NUR mit dem Nachrichtentext, kein JSON."
        )

    try:
        # Use a fresh model without JSON mode for plain text
        text_model = genai.GenerativeModel(
            model_name="gemini-1.5-flash",
            generation_config={
                "temperature": 0.7,
                "max_output_tokens": 256,
            },
        )
        response = text_model.generate_content(prompt)
        result = response.text.strip()
        # Strip any accidental JSON wrapping
        if result.startswith("{"):
            try:
                data = json.loads(result)
                result = data.get("text", result)
            except json.JSONDecodeError:
                pass
        add_to_history(conversation_id, "model", result)
        return result
    except Exception as exc:
        logger.error("Gemini payment message error: %s", exc)
        if payment_type == "paypal":
            fallback = (
                f"Hier ist mein PayPal: {payment_data} — "
                "bitte überweise den Betrag. Danke! 😊"
            )
        else:
            fallback = (
                f"Hier sind meine Bankdaten: {payment_data} — "
                "bitte überweise den Betrag. Danke! 😊"
            )
        add_to_history(conversation_id, "model", fallback)
        return fallback
