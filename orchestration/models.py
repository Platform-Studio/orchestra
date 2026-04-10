"""Data models for the orchestration framework."""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
import uuid


def new_id() -> str:
    """Generate a new unique ID."""
    return str(uuid.uuid4())


def now_iso() -> str:
    """Get current UTC time as ISO format string."""
    return datetime.now(timezone.utc).isoformat()


DEFAULT_TASK_STATES = {
    "pending": ["in_progress"],
    "in_progress": ["completed", "failed"],
    "completed": [],
    "failed": ["pending"],
}


@dataclass
class RetryConfig:
    max_retries: int = 3
    backoff: str = "exponential"
    base_seconds: int = 60

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        if data is None:
            return None
        return cls(
            max_retries=data.get("max_retries", 3),
            backoff=data.get("backoff", "exponential"),
            base_seconds=data.get("base_seconds", 60),
        )


@dataclass
class AuditEntry:
    timestamp: str
    type: str
    description: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict):
        return cls(
            timestamp=data["timestamp"],
            type=data["type"],
            description=data["description"],
        )


@dataclass
class Trigger:
    id: str
    on_state: str
    action: str  # "run_agent" or "run_command"
    agent: str = None
    command: str = None

    def to_dict(self) -> dict:
        d = {"id": self.id, "on_state": self.on_state, "action": self.action}
        if self.agent is not None:
            d["agent"] = self.agent
        if self.command is not None:
            d["command"] = self.command
        return d

    @classmethod
    def from_dict(cls, data: dict):
        return cls(
            id=data.get("id", new_id()),
            on_state=data["on_state"],
            action=data["action"],
            agent=data.get("agent"),
            command=data.get("command"),
        )


@dataclass
class Workstream:
    id: str
    name: str
    description: str = None
    parent_id: str = None
    task_states: dict = field(default_factory=lambda: dict(DEFAULT_TASK_STATES))
    retry: RetryConfig = None
    triggers: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "name": self.name,
            "task_states": self.task_states,
            "triggers": [t.to_dict() for t in self.triggers],
        }
        if self.description is not None:
            d["description"] = self.description
        if self.parent_id is not None:
            d["parent_id"] = self.parent_id
        if self.retry is not None:
            d["retry"] = self.retry.to_dict()
        return d

    @classmethod
    def from_dict(cls, data: dict):
        retry = RetryConfig.from_dict(data.get("retry"))
        triggers = [Trigger.from_dict(t) for t in data.get("triggers", [])]
        return cls(
            id=data["id"],
            name=data["name"],
            description=data.get("description"),
            parent_id=data.get("parent_id"),
            task_states=data.get("task_states", dict(DEFAULT_TASK_STATES)),
            retry=retry,
            triggers=triggers,
        )

    def initial_status(self) -> str:
        """Return the first state from the task_states map."""
        return next(iter(self.task_states))

    def validate_transition(self, from_state: str, to_state: str) -> bool:
        """Check if a state transition is allowed."""
        allowed = self.task_states.get(from_state, [])
        return to_state in allowed


@dataclass
class Task:
    id: str
    workstream_id: str
    title: str
    description: str = None
    status: str = "pending"
    creator: str = None
    tags: list = field(default_factory=list)
    retry: RetryConfig = None
    comments: list = field(default_factory=list)
    audit: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "workstream_id": self.workstream_id,
            "title": self.title,
            "status": self.status,
            "tags": self.tags,
            "comments": self.comments,
            "audit": [a.to_dict() for a in self.audit],
        }
        if self.description is not None:
            d["description"] = self.description
        if self.creator is not None:
            d["creator"] = self.creator
        if self.retry is not None:
            d["retry"] = self.retry.to_dict()
        return d

    @classmethod
    def from_dict(cls, data: dict):
        retry = RetryConfig.from_dict(data.get("retry"))
        audit = [AuditEntry.from_dict(a) for a in data.get("audit", [])]
        return cls(
            id=data["id"],
            workstream_id=data["workstream_id"],
            title=data["title"],
            description=data.get("description"),
            status=data.get("status", "pending"),
            creator=data.get("creator"),
            tags=data.get("tags", []),
            retry=retry,
            comments=data.get("comments", []),
            audit=audit,
        )

    def add_audit(self, event_type: str, description: str):
        """Append an audit entry with current timestamp."""
        self.audit.append(AuditEntry(
            timestamp=now_iso(),
            type=event_type,
            description=description,
        ))


@dataclass
class Lock:
    agent_id: str
    acquired_at: str
    expires_at: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict):
        return cls(
            agent_id=data["agent_id"],
            acquired_at=data["acquired_at"],
            expires_at=data["expires_at"],
        )

    def is_expired(self) -> bool:
        expires = datetime.fromisoformat(self.expires_at)
        now = datetime.now(timezone.utc)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return now >= expires
