"""
Hermes Agent plugin registration.

Registers:
  - on_session_start hook: drain inbox via peek+ack
  - on_session_end hook: flush outbox queue
  - send_agentlair_message tool: explicit async messaging
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from hermes_agentlair.client import AgentLairClient, InboxMessage

logger = logging.getLogger("hermes_agentlair")

# Module-level state — shared across hooks and tools within one session.
_client: AgentLairClient | None = None
_pending_acks: list[InboxMessage] = []
_outbox_queue: list[dict[str, Any]] = []


def _get_client() -> AgentLairClient:
    """Lazy-init the client singleton."""
    global _client
    if _client is None:
        _client = AgentLairClient()
    return _client


def _is_configured() -> bool:
    """Check if required env vars are set."""
    return bool(
        os.environ.get("AGENTLAIR_API_KEY")
        and os.environ.get("AGENTLAIR_ADDRESS")
    )


# ── Lifecycle hooks ──────────────────────────────────────────────


async def on_session_start(**kwargs: Any) -> dict[str, Any] | None:
    """
    Drain inbox on session startup (peek phase of peek+ack).

    Fetches all unread messages, formats them as context, and injects
    into the session. Messages are NOT marked as read yet — that happens
    in on_session_end after successful processing.

    Returns:
        Dict with 'context' key to inject messages into the conversation,
        or None if no messages.
    """
    if not _is_configured():
        logger.debug("AgentLair not configured, skipping inbox drain")
        return None

    global _pending_acks
    client = _get_client()

    try:
        messages = client.drain_inbox()
    except Exception as e:
        logger.error(f"Failed to drain AgentLair inbox: {e}")
        return None

    if not messages:
        logger.debug("AgentLair inbox empty")
        return None

    # Store for ack on session end
    _pending_acks = messages

    # Format messages as context for the LLM
    lines = [f"[AgentLair] {len(messages)} message(s) received:"]
    for i, msg in enumerate(messages, 1):
        lines.append(f"\n--- Message {i} ---")
        lines.append(f"From: {msg.from_addr}")
        lines.append(f"Subject: {msg.subject}")
        if msg.received_at:
            lines.append(f"Received: {msg.received_at}")
        if msg.body:
            lines.append(f"Body:\n{msg.body}")
        else:
            lines.append("(Body not available)")

    context = "\n".join(lines)
    logger.info(f"Injecting {len(messages)} AgentLair message(s) into session")

    return {"context": context}


async def on_session_end(**kwargs: Any) -> None:
    """
    Ack processed messages and flush outbox on session end.

    This is the 'ack' phase of peek+ack. Only called if the session
    ends normally — if it crashes, messages stay unread for next startup.
    """
    if not _is_configured():
        return

    global _pending_acks, _outbox_queue
    client = _get_client()

    # Ack all messages that were injected at session start
    for msg in _pending_acks:
        try:
            client.ack(msg)
            logger.debug(f"Acked message {msg.message_id}")
        except Exception as e:
            logger.error(f"Failed to ack message {msg.message_id}: {e}")

    _pending_acks = []

    # Flush outbox queue
    for item in _outbox_queue:
        try:
            client.send_message(
                to=item["to"],
                subject=item["subject"],
                text=item["text"],
                in_reply_to=item.get("in_reply_to"),
            )
            logger.debug(f"Sent queued message to {item['to']}")
        except Exception as e:
            logger.error(f"Failed to send queued message to {item['to']}: {e}")

    _outbox_queue = []

    # Cleanup
    client.close()


# ── Tool ─────────────────────────────────────────────────────────


SEND_TOOL_SCHEMA = {
    "name": "send_agentlair_message",
    "description": (
        "Send an async message to another agent or human via AgentLair email. "
        "Messages are delivered to the recipient's AgentLair inbox. "
        "Use this for cross-platform, cross-framework agent communication."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "to": {
                "type": "string",
                "description": "Recipient email address (e.g. 'agent@agentlair.dev' or any email)",
            },
            "subject": {
                "type": "string",
                "description": "Message subject line",
            },
            "text": {
                "type": "string",
                "description": "Message body (plain text)",
            },
            "in_reply_to": {
                "type": "string",
                "description": "Optional message ID to reply to (for threading)",
            },
            "queue": {
                "type": "boolean",
                "description": "If true, queue for session-end flush instead of sending immediately. Default: false.",
            },
        },
        "required": ["to", "subject", "text"],
    },
}


def handle_send_tool(args: dict[str, Any], **kwargs: Any) -> str:
    """Handle the send_agentlair_message tool call."""
    if not _is_configured():
        return json.dumps({
            "error": "AgentLair not configured. Set AGENTLAIR_API_KEY and AGENTLAIR_ADDRESS.",
        })

    to = args["to"]
    subject = args["subject"]
    text = args["text"]
    in_reply_to = args.get("in_reply_to")
    queue = args.get("queue", False)

    if queue:
        _outbox_queue.append({
            "to": to,
            "subject": subject,
            "text": text,
            "in_reply_to": in_reply_to,
        })
        return json.dumps({
            "status": "queued",
            "message": f"Message to {to} queued for session-end flush",
            "queue_size": len(_outbox_queue),
        })

    client = _get_client()
    try:
        result = client.send_message(
            to=to,
            subject=subject,
            text=text,
            in_reply_to=in_reply_to,
        )
        return json.dumps({
            "status": "sent",
            "id": result.get("id"),
            "sent_at": result.get("sent_at"),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Delegate fallback (mail drop) ────────────────────────────────


def delegate_fallback(
    to: str,
    subject: str,
    text: str,
    in_reply_to: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """
    Fallback handler for cross-agent delegation.

    When a Hermes agent tries to delegate a task to another agent but
    that agent is unreachable (offline, different framework, unknown
    protocol), this drops a message into their AgentLair inbox instead.

    This is the "mail drop" pattern: async, reliable, framework-agnostic.
    The receiving agent picks it up on their next session start via
    the peek+ack lifecycle hook.

    Returns:
        Dict with delivery status and message details.
    """
    if not _is_configured():
        return {
            "status": "error",
            "error": "AgentLair not configured. Set AGENTLAIR_API_KEY and AGENTLAIR_ADDRESS.",
        }

    client = _get_client()
    try:
        result = client.send_message(
            to=to,
            subject=f"[delegate] {subject}" if not subject.startswith("[delegate]") else subject,
            text=text,
            in_reply_to=in_reply_to,
        )
        logger.info(f"Delegate fallback: dropped message for {to}")
        return {
            "status": "dropped",
            "method": "agentlair_mail_drop",
            "id": result.get("id"),
            "message": f"Target agent unreachable via direct protocol. Message dropped to {to}'s AgentLair inbox for async pickup.",
        }
    except Exception as e:
        logger.error(f"Delegate fallback failed for {to}: {e}")
        return {"status": "error", "error": str(e)}


# ── Check function ───────────────────────────────────────────────


def check_agentlair() -> bool:
    """Check if AgentLair is available (env vars set)."""
    return _is_configured()


# ── Plugin registration ──────────────────────────────────────────


def register(ctx: Any) -> None:
    """
    Register the AgentLair plugin with Hermes Agent.

    Called by the Hermes plugin system (via entry point or directory discovery).
    ctx is a PluginContext providing register_tool() and register_hook().
    """
    # Register lifecycle hooks
    # on_session_start: peek inbox, inject messages as context
    # Note: pre_llm_call would also work (called before each LLM turn),
    # but on_session_start fires once — correct for inbox drain.
    ctx.register_hook("on_session_start", on_session_start)
    ctx.register_hook("on_session_end", on_session_end)

    # Register the send tool
    ctx.register_tool(
        name="send_agentlair_message",
        toolset="communication",
        schema=SEND_TOOL_SCHEMA,
        handler=handle_send_tool,
        check_fn=check_agentlair,
        requires_env=["AGENTLAIR_API_KEY", "AGENTLAIR_ADDRESS"],
        description="Send async messages via AgentLair",
    )

    # Register delegate fallback
    # When another agent is unreachable, Hermes can fall back to
    # dropping a message in their AgentLair inbox.
    if hasattr(ctx, "register_delegate_fallback"):
        ctx.register_delegate_fallback(
            name="agentlair_mail_drop",
            handler=delegate_fallback,
            description="Drop message to agent's AgentLair inbox when direct delegation fails",
            check_fn=check_agentlair,
        )

    logger.info("AgentLair plugin registered")
