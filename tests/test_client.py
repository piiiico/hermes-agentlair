"""Tests for AgentLairClient."""

import json
import pytest
import httpx
import respx

from hermes_agentlair.client import AgentLairClient, InboxMessage


BASE = "https://agentlair.dev"
TEST_KEY = "al_live_test_key_000000000000000"
TEST_ADDR = "test-agent@agentlair.dev"


@pytest.fixture
def client():
    c = AgentLairClient(api_key=TEST_KEY, address=TEST_ADDR)
    yield c
    c.close()


class TestInboxMessage:
    def test_clean_id_strips_brackets(self):
        msg = InboxMessage(
            message_id="<abc123@mail.agentlair.dev>",
            from_addr="sender@example.com",
            subject="Test",
        )
        assert msg.clean_id == "abc123@mail.agentlair.dev"

    def test_clean_id_no_brackets(self):
        msg = InboxMessage(
            message_id="abc123@mail.agentlair.dev",
            from_addr="sender@example.com",
            subject="Test",
        )
        assert msg.clean_id == "abc123@mail.agentlair.dev"

    def test_encoded_id(self):
        msg = InboxMessage(
            message_id="<abc@mail.agentlair.dev>",
            from_addr="sender@example.com",
            subject="Test",
        )
        assert msg.encoded_id == "abc%40mail.agentlair.dev"


class TestPeekInbox:
    @respx.mock
    def test_peek_returns_unread_only(self, client):
        respx.get(f"{BASE}/v1/email/inbox").mock(
            return_value=httpx.Response(200, json={
                "messages": [
                    {
                        "message_id": "<msg1@test>",
                        "from": "alice@example.com",
                        "subject": "Hello",
                        "read": False,
                        "received_at": "2026-04-10T00:00:00Z",
                    },
                    {
                        "message_id": "<msg2@test>",
                        "from": "bob@example.com",
                        "subject": "Already read",
                        "read": True,
                    },
                ],
                "count": 2,
            })
        )

        messages = client.peek_inbox()
        assert len(messages) == 1
        assert messages[0].from_addr == "alice@example.com"
        assert messages[0].subject == "Hello"

    @respx.mock
    def test_peek_empty_inbox(self, client):
        respx.get(f"{BASE}/v1/email/inbox").mock(
            return_value=httpx.Response(200, json={"messages": [], "count": 0})
        )

        messages = client.peek_inbox()
        assert messages == []


class TestReadMessage:
    @respx.mock
    def test_read_populates_body(self, client):
        msg = InboxMessage(
            message_id="<msg1@test>",
            from_addr="alice@example.com",
            subject="Hello",
        )

        respx.get(f"{BASE}/v1/email/messages/{msg.encoded_id}").mock(
            return_value=httpx.Response(200, json={
                "from": "alice@example.com",
                "subject": "Hello",
                "text": "This is the body text",
                "received_at": "2026-04-10T00:00:00Z",
            })
        )

        full = client.read_message(msg)
        assert full.body == "This is the body text"
        assert full.message_id == msg.message_id


class TestAck:
    @respx.mock
    def test_ack_marks_read(self, client):
        msg = InboxMessage(
            message_id="<msg1@test>",
            from_addr="alice@example.com",
            subject="Hello",
        )

        respx.patch(f"{BASE}/v1/email/messages/{msg.encoded_id}").mock(
            return_value=httpx.Response(200, json={
                "updated": True,
                "message_id": msg.message_id,
                "read": True,
            })
        )

        assert client.ack(msg) is True


class TestSendMessage:
    @respx.mock
    def test_send_basic(self, client):
        respx.post(f"{BASE}/v1/email/send").mock(
            return_value=httpx.Response(200, json={
                "id": "out_abc123",
                "status": "sent",
                "sent_at": "2026-04-10T00:00:00Z",
            })
        )

        result = client.send_message(
            to="recipient@example.com",
            subject="Test",
            text="Hello from Hermes!",
        )
        assert result["status"] == "sent"
        assert result["id"] == "out_abc123"

    @respx.mock
    def test_send_with_reply(self, client):
        respx.post(f"{BASE}/v1/email/send").mock(
            return_value=httpx.Response(200, json={
                "id": "out_def456",
                "status": "sent",
                "sent_at": "2026-04-10T00:00:00Z",
            })
        )

        result = client.send_message(
            to=["a@example.com", "b@example.com"],
            subject="Re: Thread",
            text="Reply body",
            in_reply_to="<original@test>",
        )
        assert result["status"] == "sent"

    @respx.mock
    def test_send_uses_text_not_body(self, client):
        """Verify we send 'text' field, not 'body' (API requirement)."""
        route = respx.post(f"{BASE}/v1/email/send").mock(
            return_value=httpx.Response(200, json={
                "id": "out_xxx",
                "status": "sent",
                "sent_at": "2026-04-10T00:00:00Z",
            })
        )

        client.send_message(to="x@example.com", subject="S", text="T")

        request = route.calls[0].request
        body = json.loads(request.content)
        assert "text" in body
        assert "body" not in body


class TestDrainInbox:
    @respx.mock
    def test_drain_fetches_bodies(self, client):
        respx.get(f"{BASE}/v1/email/inbox").mock(
            return_value=httpx.Response(200, json={
                "messages": [
                    {
                        "message_id": "<msg1@test>",
                        "from": "alice@example.com",
                        "subject": "Hello",
                        "read": False,
                    },
                ],
                "count": 1,
            })
        )
        respx.get(f"{BASE}/v1/email/messages/msg1%40test").mock(
            return_value=httpx.Response(200, json={
                "from": "alice@example.com",
                "subject": "Hello",
                "text": "Full body here",
            })
        )

        messages = client.drain_inbox()
        assert len(messages) == 1
        assert messages[0].body == "Full body here"
