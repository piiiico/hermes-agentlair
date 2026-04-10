"""
hermes-agentlair: AgentLair integration for Hermes Agent.

Lifecycle-first design:
  - on_session_start: drain inbox (peek+ack via read→process→mark-read)
  - on_session_end: flush any queued outbound messages
  - send_agentlair_message tool: explicit messaging during session

Crash recovery: messages stay unread until successfully processed (ack).
If the agent crashes mid-session, unprocessed messages remain in inbox
for the next startup.
"""

from hermes_agentlair.plugin import register, delegate_fallback
from hermes_agentlair.client import AgentLairClient

__all__ = ["register", "delegate_fallback", "AgentLairClient"]
__version__ = "0.1.0"
