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
    action: str  # "run_agent" or "run_command"
    on_state: str = None       # state-based trigger
    on_schedule: str = None    # cron expression for schedule-based trigger
    filter: dict = None        # filter for schedule-based triggers (state, tags, older_than_days)
    agent: str = None
    command: str = None
    max_concurrent: int = 1    # max parallel executions within the workstream

    def to_dict(self) -> dict:
        d = {"id": self.id, "action": self.action}
        if self.on_state is not None:
            d["on_state"] = self.on_state
        if self.on_schedule is not None:
            d["on_schedule"] = self.on_schedule
        if self.filter is not None:
            d["filter"] = self.filter
        if self.agent is not None:
            d["agent"] = self.agent
        if self.command is not None:
            d["command"] = self.command
        if self.max_concurrent != 1:
            d["max_concurrent"] = self.max_concurrent
        return d

    @classmethod
    def from_dict(cls, data: dict):
        return cls(
            id=data.get("id", new_id()),
            action=data["action"],
            on_state=data.get("on_state"),
            on_schedule=data.get("on_schedule"),
            filter=data.get("filter"),
            agent=data.get("agent"),
            command=data.get("command"),
            max_concurrent=data.get("max_concurrent", 1),
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
    paused: bool = False

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
        if self.paused:
            d["paused"] = True
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
            paused=data.get("paused", False),
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
    scheduled_at: str = None       # ISO datetime for one-shot scheduled action
    scheduled_action: dict = None  # {"type": "run_agent", "agent": "..."} or {"type": "run_command", "command": "..."}

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
        if self.scheduled_at is not None:
            d["scheduled_at"] = self.scheduled_at
        if self.scheduled_action is not None:
            d["scheduled_action"] = self.scheduled_action
        if hasattr(self, '_parse_error'):
            d["_error"] = self._parse_error
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
            scheduled_at=data.get("scheduled_at"),
            scheduled_action=data.get("scheduled_action"),
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
