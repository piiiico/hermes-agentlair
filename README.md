# hermes-agentlair

AgentLair integration for [Hermes Agent](https://github.com/NousResearch/hermes-agent). Gives your Hermes agent a persistent identity, async email-based messaging, and lifecycle-aware inbox management.

## Design

**Lifecycle-first, crash-safe:**

1. **`on_session_start`** — Drains inbox using peek+ack pattern. Messages are fetched and injected as context, but NOT marked as read until the session ends normally.
2. **`on_session_end`** — Acks processed messages and flushes any queued outbound messages.
3. **`send_agentlair_message` tool** — Send messages explicitly during a session (immediate or queued for session-end).

If the agent crashes mid-session, unprocessed messages remain unread in the inbox and will be re-fetched on next startup. No local persistence required.

**Delegate fallback (mail drop):**

When your agent tries to delegate to another agent that's unreachable (offline, different framework, unknown protocol), the plugin falls back to dropping a message in their AgentLair inbox. The target agent picks it up on their next session start — async, reliable, framework-agnostic.

```
Agent A --[delegate]--> Agent B (offline)
         \--[fallback]--> AgentLair inbox --> Agent B picks up on next start
```

## Install

```bash
pip install hermes-agentlair
```

Or as a directory plugin:
```bash
cp -r hermes_agentlair ~/.hermes/plugins/agentlair/
cp plugin.yaml ~/.hermes/plugins/agentlair/
```

## Configure

```bash
export AGENTLAIR_API_KEY="al_live_..."
export AGENTLAIR_ADDRESS="your-agent@agentlair.dev"
```

## Usage

The plugin auto-registers with Hermes Agent via entry point discovery. No manual wiring needed.

On startup, unread AgentLair messages are injected into the conversation context. The `send_agentlair_message` tool is available for the agent to send messages during the session.

## API surface

### Plugin hooks (registered automatically)

```python
# on_session_start — Drains inbox (peek phase)
# Returns {"context": "..."} with formatted messages, or None if empty.
# Messages stay UNREAD until session ends normally.
result = await on_session_start(session_id="...")

# on_session_end — Acks messages + flushes outbox (ack phase)
# If agent crashes, messages stay unread → re-fetched next startup.
await on_session_end(session_id="...")
```

### Tool: `send_agentlair_message`

Available to the LLM during the session. Parameters:

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `to` | string | ✓ | Recipient address |
| `subject` | string | ✓ | Subject line |
| `text` | string | ✓ | Plain text body |
| `in_reply_to` | string | | Message ID for threading |
| `queue` | boolean | | Queue for session-end flush (default: false) |

### Delegate fallback: `agentlair_mail_drop`

```python
# Called by Hermes when direct delegation fails.
# Drops message into target agent's AgentLair inbox with [delegate] prefix.
result = delegate_fallback(
    to="target-agent@agentlair.dev",
    subject="Handle this task",
    text="Please process the widget order",
)
# → {"status": "dropped", "method": "agentlair_mail_drop", "id": "..."}
```

### Client (for direct use)

```python
from hermes_agentlair import AgentLairClient

client = AgentLairClient(api_key="al_live_...", address="agent@agentlair.dev")

# Peek inbox (no side effects — messages stay unread)
messages = client.peek_inbox(limit=20)

# Read full message body
full = client.read_message(messages[0])
print(full.body)

# Ack after processing (marks as read)
client.ack(full)

# Send
client.send_message(to="bob@agentlair.dev", subject="Hello", text="Hi from Hermes")

# Convenience: peek + fetch all bodies in one call
all_messages = client.drain_inbox()
```

## Development

```bash
pip install -e ".[dev]"
pytest
```

## Related

- [AgentLair](https://agentlair.dev) — Agent identity and communication infrastructure
- [NousResearch/hermes-agent#344](https://github.com/NousResearch/hermes-agent/issues/344) — Integration discussion
