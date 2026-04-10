"""Tests for the Hermes Agent plugin registration and hooks."""

import json
import os
import pytest
import httpx
import respx
from unittest.mock import MagicMock, AsyncMock

import hermes_agentlair.plugin as plugin_mod
from hermes_agentlair.plugin import (
    register,
    on_session_start,
    on_session_end,
    handle_send_tool,
    delegate_fallback,
)


BASE = "https://agentlair.dev"
TEST_KEY = "al_live_test_key_000000000000000"
TEST_ADDR = "test-agent@agentlair.dev"


@pytest.fixture(autouse=True)
def reset_plugin_state(monkeypatch):
    """Reset module-level state and set env vars for each test."""
    monkeypatch.setenv("AGENTLAIR_API_KEY", TEST_KEY)
    monkeypatch.setenv("AGENTLAIR_ADDRESS", TEST_ADDR)

    # Reset module state
    plugin_mod._client = None
    plugin_mod._pending_acks = []
    plugin_mod._outbox_queue = []

    yield

    # Cleanup client
    if plugin_mod._client:
        plugin_mod._client.close()
        plugin_mod._client = None


class TestRegister:
    def test_registers_hooks_and_tool(self):
        ctx = MagicMock()
        register(ctx)

        # Should register two hooks
        hook_calls = ctx.register_hook.call_args_list
        hook_names = [call[0][0] for call in hook_calls]
        assert "on_session_start" in hook_names
        assert "on_session_end" in hook_names

        # Should register one tool
        ctx.register_tool.assert_called_once()
        tool_call = ctx.register_tool.call_args
        assert tool_call[1]["name"] == "send_agentlair_message"
        assert tool_call[1]["toolset"] == "communication"


class TestOnSessionStart:
    @respx.mock
    @pytest.mark.asyncio
    async def test_injects_messages_as_context(self):
        respx.get(f"{BASE}/v1/email/inbox").mock(
            return_value=httpx.Response(200, json={
                "messages": [
                    {
                        "message_id": "<msg1@test>",
                        "from": "alice@example.com",
                        "subject": "Task for you",
                        "read": False,
                    },
                ],
                "count": 1,
            })
        )
        respx.get(f"{BASE}/v1/email/messages/msg1%40test").mock(
            return_value=httpx.Response(200, json={
                "from": "alice@example.com",
                "subject": "Task for you",
                "text": "Please do the thing",
            })
        )

        result = await on_session_start(session_id="test-session")

        assert result is not None
        assert "context" in result
        assert "alice@example.com" in result["context"]
        assert "Task for you" in result["context"]
        assert "Please do the thing" in result["context"]

        # Messages should be queued for ack
        assert len(plugin_mod._pending_acks) == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_returns_none_on_empty_inbox(self):
        respx.get(f"{BASE}/v1/email/inbox").mock(
            return_value=httpx.Response(200, json={"messages": [], "count": 0})
        )

        result = await on_session_start(session_id="test-session")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_not_configured(self, monkeypatch):
        monkeypatch.delenv("AGENTLAIR_API_KEY")
        result = await on_session_start(session_id="test-session")
        assert result is None


class TestOnSessionEnd:
    @respx.mock
    @pytest.mark.asyncio
    async def test_acks_pending_messages(self):
        from hermes_agentlair.client import InboxMessage

        msg = InboxMessage(
            message_id="<msg1@test>",
            from_addr="alice@example.com",
            subject="Test",
            body="Body",
        )
        plugin_mod._pending_acks = [msg]

        respx.patch(f"{BASE}/v1/email/messages/msg1%40test").mock(
            return_value=httpx.Response(200, json={
                "updated": True,
                "read": True,
            })
        )

        await on_session_end(session_id="test-session")

        assert len(plugin_mod._pending_acks) == 0

    @respx.mock
    @pytest.mark.asyncio
    async def test_flushes_outbox_queue(self):
        plugin_mod._outbox_queue = [
            {
                "to": "bob@example.com",
                "subject": "Queued msg",
                "text": "Queued body",
            },
        ]

        respx.post(f"{BASE}/v1/email/send").mock(
            return_value=httpx.Response(200, json={
                "id": "out_queued",
                "status": "sent",
                "sent_at": "2026-04-10T00:00:00Z",
            })
        )

        await on_session_end(session_id="test-session")

        assert len(plugin_mod._outbox_queue) == 0


class TestSendTool:
    @respx.mock
    def test_send_immediate(self):
        respx.post(f"{BASE}/v1/email/send").mock(
            return_value=httpx.Response(200, json={
                "id": "out_tool",
                "status": "sent",
                "sent_at": "2026-04-10T00:00:00Z",
            })
        )

        result = json.loads(handle_send_tool({
            "to": "recipient@example.com",
            "subject": "From tool",
            "text": "Tool body",
        }))

        assert result["status"] == "sent"

    def test_send_queued(self):
        result = json.loads(handle_send_tool({
            "to": "recipient@example.com",
            "subject": "Queued",
            "text": "Queued body",
            "queue": True,
        }))

        assert result["status"] == "queued"
        assert len(plugin_mod._outbox_queue) == 1

    def test_send_not_configured(self, monkeypatch):
        monkeypatch.delenv("AGENTLAIR_API_KEY")

        # Need to reset client since it may have been initialized
        plugin_mod._client = None

        result = json.loads(handle_send_tool({
            "to": "x@example.com",
            "subject": "S",
            "text": "T",
        }))

        assert "error" in result


class TestDelegateFallback:
    @respx.mock
    def test_delegate_drops_message(self):
        respx.post(f"{BASE}/v1/email/send").mock(
            return_value=httpx.Response(200, json={
                "id": "out_delegate",
                "status": "sent",
                "sent_at": "2026-04-10T00:00:00Z",
            })
        )

        result = delegate_fallback(
            to="target-agent@agentlair.dev",
            subject="Handle this task",
            text="Please process the widget order",
        )

        assert result["status"] == "dropped"
        assert result["method"] == "agentlair_mail_drop"
        assert "out_delegate" == result["id"]

    @respx.mock
    def test_delegate_prefixes_subject(self):
        route = respx.post(f"{BASE}/v1/email/send").mock(
            return_value=httpx.Response(200, json={
                "id": "out_pfx",
                "status": "sent",
                "sent_at": "2026-04-10T00:00:00Z",
            })
        )

        delegate_fallback(
            to="target@agentlair.dev",
            subject="Handle this",
            text="Body",
        )

        request = route.calls[0].request
        body = json.loads(request.content)
        assert body["subject"] == "[delegate] Handle this"

    @respx.mock
    def test_delegate_no_double_prefix(self):
        route = respx.post(f"{BASE}/v1/email/send").mock(
            return_value=httpx.Response(200, json={
                "id": "out_nopfx",
                "status": "sent",
                "sent_at": "2026-04-10T00:00:00Z",
            })
        )

        delegate_fallback(
            to="target@agentlair.dev",
            subject="[delegate] Already prefixed",
            text="Body",
        )

        request = route.calls[0].request
        body = json.loads(request.content)
        assert body["subject"] == "[delegate] Already prefixed"

    def test_delegate_not_configured(self, monkeypatch):
        monkeypatch.delenv("AGENTLAIR_API_KEY")
        plugin_mod._client = None

        result = delegate_fallback(
            to="target@agentlair.dev",
            subject="Test",
            text="Body",
        )

        assert result["status"] == "error"

    def test_register_with_delegate_support(self):
        ctx = MagicMock()
        ctx.register_delegate_fallback = MagicMock()
        register(ctx)

        ctx.register_delegate_fallback.assert_called_once()
        fallback_call = ctx.register_delegate_fallback.call_args
        assert fallback_call[1]["name"] == "agentlair_mail_drop"

    def test_register_without_delegate_support(self):
        """Gracefully handles contexts that don't support delegate_fallback."""
        ctx = MagicMock(spec=["register_hook", "register_tool"])
        # Should not raise even though register_delegate_fallback doesn't exist
        register(ctx)
