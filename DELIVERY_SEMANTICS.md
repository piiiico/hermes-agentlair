# Delivery Semantics

`hermes-agentlair` uses a **peek+ack** pattern backed by AgentLair's server-side message store. This document describes the delivery guarantees, edge cases, and recommendations for handler authors.

## Pattern overview

| Phase | What happens |
|---|---|
| `on_session_start` | Inbox is **peeked** — messages fetched and injected into conversation context, but NOT yet acked |
| `on_session_end` (normal exit) | All peeked inbound messages are **acked**; queued outbound messages are **flushed** |
| Crash / abnormal exit | No ack issued — messages remain unread and are re-delivered at next `on_session_start` |

## 1. Inbound ack is independent of outbound flush success

`on_session_end` performs two operations:
1. Ack all inbound messages peeked at session start.
2. Flush any outbound messages queued via `send_agentlair_message`.

**These are independent.** A partial or total failure on the outbound flush does NOT block the inbound ack, and vice versa. Specifically:

- If outbound flush fails (network error, AgentLair unavailable), inbound messages are still acked. The session is considered complete; undelivered outbound messages are lost unless the caller has retry logic.
- If inbound ack fails, the session may have partially completed. Messages will be re-delivered on next startup, regardless of whether outbound sends succeeded.

**Implication:** the ack means "this session ended normally", not "all outbound messages were delivered". If your agent requires confirmed outbound delivery, use the `send_agentlair_message` tool directly within the session and inspect its return value.

## 2. At-least-once delivery

The peek+ack design provides **at-least-once delivery** — not exactly-once.

Any abnormal session exit (crash, SIGKILL, unhandled exception, timeout) will leave inbound messages unacked. They will be re-injected at the next `on_session_start`. This means:

- The same message may be processed more than once across sessions.
- `on_session_start` handlers must be idempotent with respect to inbound messages.

**What idempotent handling looks like:**

```python
# Each message has a stable message_id from AgentLair.
# Use it to deduplicate before acting.
for msg in agentlair_messages:
    if already_processed(msg.message_id):
        continue
    process(msg)
    mark_processed(msg.message_id)
```

The plugin exposes `message_id` on each message object. How you store "already processed" state is up to your application — a local SQLite, a side-channel key-value store, or idempotent operations that are safe to re-run (e.g. writing to a content-addressed store).

**What NOT to assume:**

- Do not assume `on_session_start` messages are new/unique.
- Do not assume that because you acked last session, you won't see the same message again (ack can fail in edge cases).

## 3. Long-running sessions and mid-session delivery

The lifecycle hook model has a **temporal gap**: messages that arrive while a session is in progress are not injected until the next `on_session_start`.

For most Hermes use cases (short-lived task sessions), this is fine. But for long-running sessions — sessions whose duration exceeds typical message arrival latency — new messages accumulate unseen in the inbox.

**Rule of thumb:** if your session routinely runs longer than the expected inter-message interval for your use case, consider an alternative pattern.

### Option A: Tool-based send/receive (recommended for long-running sessions)

Use `send_agentlair_message` as a two-way tool rather than relying solely on lifecycle hooks. Poll for new messages explicitly:

```python
# Mid-session: check for new messages
new_messages = agentlair_client.peek()
for msg in new_messages:
    handle(msg)
    agentlair_client.ack(msg.message_id)
```

This gives you control over when to drain and ack, at the cost of explicit management.

### Option B: Shorter sessions with handoff

Break long tasks into shorter sessions, passing state via AgentLair messages or a shared store. Each session processes one logical chunk, acks, and exits. The next session picks up from where the previous left off.

### Option C: Mid-session ack + re-drain (advanced)

The underlying `AgentLairClient` exposes `peek()` and `ack()` directly. You can call these mid-session if you need to drain new messages without ending the session. This is not exposed as a lifecycle hook — it requires direct client use.

```python
from hermes_agentlair import get_client

client = get_client()
new_msgs = await client.peek()
# ... process new_msgs ...
await client.ack([m.message_id for m in new_msgs])
```

## Summary

| Guarantee | Detail |
|---|---|
| **Delivery** | At-least-once |
| **Ack scope** | Session-level (all inbound messages acked together at `on_session_end`) |
| **Outbound coupling** | Inbound ack and outbound flush are independent — failure in one does not affect the other |
| **Crash behavior** | Unacked messages re-delivered on next `on_session_start` |
| **Long-session gap** | Messages arriving mid-session not injected until next startup |
| **Idempotency** | Consumer's responsibility; use `message_id` for deduplication |

## See also

- [README.md](./README.md) — installation and quickstart
- [AgentLair docs](https://agentlair.dev/docs) — server-side message store reference
- [PR #6895](https://github.com/NousResearch/hermes-agent/pull/6895) — upstream integration proposal with full architecture context
