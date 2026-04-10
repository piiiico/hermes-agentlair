"""
Thin HTTP client for AgentLair email/messaging API.

Handles authentication, inbox polling, message reading, sending,
and the peek+ack pattern for crash-safe inbox draining.

No AgentLair SDK dependency — just httpx against the REST API.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger("hermes_agentlair")

BASE_URL = "https://agentlair.dev"
DEFAULT_TIMEOUT = 30.0


@dataclass
class InboxMessage:
    """A message from the AgentLair inbox."""

    message_id: str
    from_addr: str
    subject: str
    body: str | None = None
    received_at: str | None = None
    thread_id: str | None = None
    raw: dict = field(default_factory=dict)

    @property
    def clean_id(self) -> str:
        """Strip RFC 2822 angle brackets from message_id for API calls."""
        return self.message_id.strip("<>")

    @property
    def encoded_id(self) -> str:
        """URL-encoded clean message ID for API path segments."""
        return quote(self.clean_id, safe="")


class AgentLairClient:
    """
    Minimal AgentLair REST client.

    Supports the peek+ack pattern:
      1. peek() — fetch unread messages WITHOUT marking them read
      2. ack(message_id) — mark a message as read after successful processing

    If the agent crashes between peek and ack, messages stay unread
    and will be re-fetched on next startup.
    """

    def __init__(
        self,
        api_key: str | None = None,
        address: str | None = None,
        base_url: str = BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.api_key = api_key or os.environ.get("AGENTLAIR_API_KEY", "")
        self.address = address or os.environ.get("AGENTLAIR_ADDRESS", "")
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=timeout,
        )

        if not self.api_key:
            logger.warning("AGENTLAIR_API_KEY not set — API calls will fail")
        if not self.address:
            logger.warning("AGENTLAIR_ADDRESS not set — inbox operations will fail")

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    # ── Inbox (peek) ─────────────────────────────────────────────

    def peek_inbox(self, limit: int = 20) -> list[InboxMessage]:
        """
        Fetch unread inbox messages WITHOUT marking them as read.

        This is the 'peek' half of peek+ack. Messages stay unread
        until explicitly ack'd via mark_read().
        """
        resp = self._client.get(
            "/v1/email/inbox",
            params={"address": self.address, "limit": limit},
        )
        resp.raise_for_status()
        data = resp.json()

        messages = []
        for msg in data.get("messages", []):
            if msg.get("read"):
                continue  # Only unread messages
            messages.append(
                InboxMessage(
                    message_id=msg["message_id"],
                    from_addr=msg.get("from", ""),
                    subject=msg.get("subject", ""),
                    received_at=msg.get("received_at"),
                    thread_id=msg.get("thread_id"),
                    raw=msg,
                )
            )
        return messages

    def read_message(self, message: InboxMessage) -> InboxMessage:
        """
        Fetch the full body of a message. Does NOT mark it as read.

        Returns a new InboxMessage with the body populated.
        """
        resp = self._client.get(
            f"/v1/email/messages/{message.encoded_id}",
            params={"address": self.address},
        )
        resp.raise_for_status()
        data = resp.json()

        return InboxMessage(
            message_id=message.message_id,
            from_addr=data.get("from", message.from_addr),
            subject=data.get("subject", message.subject),
            body=data.get("text") or data.get("body") or data.get("html", ""),
            received_at=data.get("received_at", message.received_at),
            thread_id=data.get("thread_id", message.thread_id),
            raw=data,
        )

    # ── Ack ──────────────────────────────────────────────────────

    def ack(self, message: InboxMessage) -> bool:
        """
        Mark a message as read (the 'ack' in peek+ack).

        Call this ONLY after the message has been successfully processed.
        """
        return self.mark_read(message.encoded_id)

    def mark_read(self, encoded_message_id: str) -> bool:
        """Mark a message as read by its URL-encoded ID."""
        resp = self._client.patch(
            f"/v1/email/messages/{encoded_message_id}",
            params={"address": self.address},
            json={"read": True},
        )
        resp.raise_for_status()
        return resp.json().get("updated", False)

    # ── Send ─────────────────────────────────────────────────────

    def send_message(
        self,
        to: str | list[str],
        subject: str,
        text: str,
        in_reply_to: str | None = None,
    ) -> dict[str, Any]:
        """
        Send an email via AgentLair.

        Args:
            to: Recipient address(es).
            subject: Email subject line.
            text: Plain text body. Use 'text', not 'body' (API requirement).
            in_reply_to: Optional message ID for threading.

        Returns:
            API response dict with 'id', 'status', 'sent_at', etc.
        """
        recipients = [to] if isinstance(to, str) else to
        payload: dict[str, Any] = {
            "from": self.address,
            "to": recipients,
            "subject": subject,
            "text": text,
        }
        if in_reply_to:
            payload["in_reply_to"] = in_reply_to

        resp = self._client.post("/v1/email/send", json=payload)
        resp.raise_for_status()
        return resp.json()

    # ── Convenience ──────────────────────────────────────────────

    def drain_inbox(self) -> list[InboxMessage]:
        """
        Peek all unread messages and fetch their full bodies.

        Returns messages with bodies populated. Caller must ack()
        each message after processing to complete the peek+ack cycle.
        """
        messages = self.peek_inbox()
        full_messages = []
        for msg in messages:
            try:
                full = self.read_message(msg)
                full_messages.append(full)
            except httpx.HTTPStatusError as e:
                logger.error(f"Failed to read message {msg.message_id}: {e}")
                # Include the header-only message so caller knows it exists
                full_messages.append(msg)
        return full_messages
