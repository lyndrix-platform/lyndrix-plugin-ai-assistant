"""SQLAlchemy models for the AI Assistant plugin (shared by every core instance).

Sessions and their transcript live in the database so the chat history survives
a restart of the runner container and is visible from every instance; the live
turn itself (streaming events, pending approvals) is held by the bridge in the
runner container and proxied by the instance that hosts it (``pinned:runner``).
"""
from __future__ import annotations

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase


class _Base(DeclarativeBase):
    pass


class AiSession(_Base):
    __tablename__ = "ai_sessions"

    id = Column(String(36), primary_key=True)                 # bridge session id (uuid)
    owner = Column(String(100), nullable=False, index=True)   # Lyndrix username
    title = Column(String(200), nullable=False, default="Neue Unterhaltung")
    provider = Column(String(32), nullable=False, default="claude-code")
    model = Column(String(100), nullable=True)
    permission_mode = Column(String(32), nullable=False, default="default")
    claude_session_id = Column(String(64), nullable=True)     # for --resume after a runner restart
    status = Column(String(24), nullable=False, default="idle")   # idle | running | waiting_approval | closed
    turns = Column(Integer, nullable=False, default=0)
    total_cost_usd = Column(Float, nullable=False, default=0.0)
    archived = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False)
    last_activity = Column(DateTime, nullable=False)


class AiMessage(_Base):
    """One transcript entry: the user's prompt, the assistant's final text, a tool
    call summary or an approval decision. Streaming deltas are not stored."""
    __tablename__ = "ai_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(36), nullable=False, index=True)
    role = Column(String(16), nullable=False)                 # user | assistant | tool | system
    kind = Column(String(24), nullable=False, default="text") # text | tool_use | tool_result | approval | result | error
    content = Column(Text, nullable=True)
    meta = Column(JSON, nullable=True)                        # tool name/input summary, cost, decision …
    created_at = Column(DateTime, nullable=False)
