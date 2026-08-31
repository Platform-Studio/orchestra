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
    "in_progress": ["completed", "failed", "pending"],
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
    timezone: str = None       # IANA timezone for schedule-based trigger
    on_email: dict = None      # email-based trigger config: {recipient, event}
    event_context: dict = None # transient event payload injected at execution time
    task_selection: str = None # for state triggers: first_unlocked | all_unlocked
    filter: dict = None        # filter for schedule triggers (state/status AND tag/tags AND older_than_days)
    agent: str = None
    command: str = None
    prompt: str = None         # custom prompt injected into agent when trigger fires
    timeout: int = None        # override agent timeout (seconds); None = use agent default
    paused: bool = False

    def to_dict(self) -> dict:
        d = {"id": self.id, "action": self.action}
        if self.on_state is not None:
            d["on_state"] = self.on_state
        if self.on_schedule is not None:
            d["on_schedule"] = self.on_schedule
        if self.timezone is not None:
            d["timezone"] = self.timezone
        if self.on_email is not None:
            d["on_email"] = self.on_email
        if self.task_selection is not None:
            d["task_selection"] = self.task_selection
        if self.filter is not None:
            d["filter"] = self.filter
        if self.agent is not None:
            d["agent"] = self.agent
        if self.command is not None:
            d["command"] = self.command
        if self.prompt is not None:
            d["prompt"] = self.prompt
        if self.timeout is not None:
            d["timeout"] = self.timeout
        if self.paused:
            d["paused"] = True
        return d

    @classmethod
    def from_dict(cls, data: dict):
        return cls(
            id=data.get("id", new_id()),
            action=data["action"],
            on_state=data.get("on_state"),
            on_schedule=data.get("on_schedule"),
            timezone=data.get("timezone"),
            on_email=data.get("on_email"),
            task_selection=data.get("task_selection"),
            filter=data.get("filter"),
            agent=data.get("agent"),
            command=data.get("command"),
            prompt=data.get("prompt"),
            timeout=data.get("timeout"),
            paused=data.get("paused", False),
        )


@dataclass
class Workstream:
    id: str
    name: str
    description: str = None
    context: str = None
    parent_id: str = None
    working_directory: str = None
    artifact_root: str = None
    child_workstream_root: str = None
    inline_attachments: bool = False
    agent_concurrency: dict = field(default_factory=dict)
    task_states: dict = field(default_factory=lambda: dict(DEFAULT_TASK_STATES))
    tag_definitions: list = field(default_factory=list)
    retry: RetryConfig = None
    triggers: list = field(default_factory=list)
    paused: bool = False
    paused_states: list = field(default_factory=list)

    def to_dict(self, *, include_transient: bool = True) -> dict:
        d = {
            "id": self.id,
            "name": self.name,
            "task_states": self.task_states,
            "triggers": [t.to_dict() for t in self.triggers],
        }
        if self.description is not None:
            d["description"] = self.description
        if self.context is not None:
            d["context"] = self.context
        if self.parent_id is not None:
            d["parent_id"] = self.parent_id
        if self.working_directory is not None:
            d["working_directory"] = self.working_directory
        if self.artifact_root is not None:
            d["artifact_root"] = self.artifact_root
        if self.child_workstream_root is not None:
            d["child_workstream_root"] = self.child_workstream_root
        if self.inline_attachments:
            d["inline_attachments"] = True
        if self.agent_concurrency:
            d["agent_concurrency"] = self.agent_concurrency
        if self.tag_definitions:
            d["tag_definitions"] = self.tag_definitions
        if self.retry is not None:
            d["retry"] = self.retry.to_dict()
        if self.paused:
            d["paused"] = True
        if self.paused_states:
            d["paused_states"] = list(self.paused_states)
        if include_transient:
            if hasattr(self, "_mount_available"):
                d["mount_available"] = bool(getattr(self, "_mount_available"))
            if hasattr(self, "_resolved_workspace_path"):
                d["resolved_workspace_path"] = getattr(self, "_resolved_workspace_path")
            if hasattr(self, "_resolved_artifact_root"):
                d["resolved_artifact_root"] = getattr(self, "_resolved_artifact_root")
            if hasattr(self, "_resolved_child_workstream_root"):
                d["resolved_child_workstream_root"] = getattr(self, "_resolved_child_workstream_root")
        return d

    @classmethod
    def from_dict(cls, data: dict):
        retry = RetryConfig.from_dict(data.get("retry"))
        triggers = [Trigger.from_dict(t) for t in data.get("triggers", [])]
        return cls(
            id=data["id"],
            name=data["name"],
            description=data.get("description"),
            context=data.get("context"),
            parent_id=data.get("parent_id"),
            working_directory=data.get("working_directory"),
            artifact_root=data.get("artifact_root"),
            child_workstream_root=data.get("child_workstream_root"),
            inline_attachments=data.get("inline_attachments", False),
            agent_concurrency=data.get("agent_concurrency", {}),
            task_states=data.get("task_states", dict(DEFAULT_TASK_STATES)),
            tag_definitions=data.get("tag_definitions", []),
            retry=retry,
            triggers=triggers,
            paused=data.get("paused", False),
            paused_states=list(data.get("paused_states", data.get("paused_columns", [])) or []),
        )

    def initial_status(self) -> str:
        """Return the first state from the task_states map."""
        return next(iter(self.task_states))

    def validate_transition(self, from_state: str, to_state: str) -> bool:
        """Check if a state transition is allowed."""
        allowed = self.task_states.get(from_state, [])
        return to_state in allowed


def normalize_agent_concurrency_policy(policy) -> dict:
    """Validate and normalize workstream agent concurrency policy."""
    if policy in (None, ""):
        return {}
    if not isinstance(policy, dict):
        raise ValueError("agent_concurrency must be a JSON object")

    normalized = {}

    def _normalize_limit(value, *, field_name: str) -> int:
        try:
            limit = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{field_name} must be an integer") from None
        if limit < 1:
            raise ValueError(f"{field_name} must be >= 1")
        return limit

    def _normalize_overrides(raw_overrides, *, field_name: str) -> dict:
        if raw_overrides in (None, {}):
            return {}
        if not isinstance(raw_overrides, dict):
            raise ValueError(f"{field_name} must be an object")

        cleaned = {}
        for key, value in raw_overrides.items():
            name = str(key or "").strip()
            if not name:
                raise ValueError(f"{field_name} cannot contain an empty agent key")
            cleaned[name] = _normalize_limit(value, field_name=f"{field_name}.{name}")
        return cleaned

    if "default" in policy:
        normalized["default"] = _normalize_limit(
            policy.get("default"),
            field_name="agent_concurrency.default",
        )

    overrides = _normalize_overrides(
        policy.get("overrides"),
        field_name="agent_concurrency.overrides",
    )
    if overrides:
        normalized["overrides"] = overrides

    raw_state_overrides = policy.get("state_overrides")
    if raw_state_overrides not in (None, {}):
        if not isinstance(raw_state_overrides, dict):
            raise ValueError("agent_concurrency.state_overrides must be an object")

        state_overrides = {}
        for state_name, state_policy in raw_state_overrides.items():
            state_key = str(state_name or "").strip()
            if not state_key:
                raise ValueError("agent_concurrency.state_overrides cannot contain an empty state key")
            if not isinstance(state_policy, dict):
                raise ValueError(
                    f"agent_concurrency.state_overrides.{state_key} must be an object"
                )

            normalized_state_policy = {}
            if "default" in state_policy:
                normalized_state_policy["default"] = _normalize_limit(
                    state_policy.get("default"),
                    field_name=f"agent_concurrency.state_overrides.{state_key}.default",
                )
            state_agent_overrides = _normalize_overrides(
                state_policy.get("overrides"),
                field_name=f"agent_concurrency.state_overrides.{state_key}.overrides",
            )
            if state_agent_overrides:
                normalized_state_policy["overrides"] = state_agent_overrides

            if normalized_state_policy:
                state_overrides[state_key] = normalized_state_policy

        if state_overrides:
            normalized["state_overrides"] = state_overrides

    unknown_keys = set(policy.keys()) - {"default", "overrides", "state_overrides"}
    if unknown_keys:
        unknown = ", ".join(sorted(str(k) for k in unknown_keys))
        raise ValueError(f"Unsupported agent_concurrency keys: {unknown}")

    return normalized


@dataclass
class Task:
    id: str
    workstream_id: str
    title: str
    description: str = None
    status: str = "pending"
    rank: str = None
    creator: str = None
    tags: list = field(default_factory=list)
    retry: RetryConfig = None
    comments: list = field(default_factory=list)
    audit: list = field(default_factory=list)
    scheduled_at: str = None       # ISO datetime for one-shot scheduled action
    scheduled_action: dict = None  # {"type": "run_agent", "agent": "..."} or {"type": "run_command", "command": "..."}
    attachments: list = field(default_factory=list)  # artifact paths
    retry_count: int = 0
    last_failure_at: str = None
    paused: bool = False
    token_usage: dict = None
    task_errors: list = field(default_factory=list)

    def to_dict(self, *, include_audit: bool = True) -> dict:
        d = {
            "id": self.id,
            "workstream_id": self.workstream_id,
            "title": self.title,
            "status": self.status,
            "tags": self.tags,
            "comments": self.comments,
            "attachments": self.attachments,
        }
        if include_audit:
            d["audit"] = [a.to_dict() for a in self.audit]
        if self.paused:
            d["paused"] = True
        if self.description is not None:
            d["description"] = self.description
        if self.rank is not None:
            d["rank"] = self.rank
        if self.creator is not None:
            d["creator"] = self.creator
        if self.retry is not None:
            d["retry"] = self.retry.to_dict()
        if self.scheduled_at is not None:
            d["scheduled_at"] = self.scheduled_at
        if self.scheduled_action is not None:
            d["scheduled_action"] = self.scheduled_action
        if self.retry_count > 0:
            d["retry_count"] = self.retry_count
        if self.last_failure_at is not None:
            d["last_failure_at"] = self.last_failure_at
        if self.token_usage is not None:
            d["token_usage"] = self.token_usage
        if self.task_errors:
            d["task_errors"] = self.task_errors
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
            rank=data.get("rank"),
            creator=data.get("creator"),
            tags=data.get("tags", []),
            retry=retry,
            comments=data.get("comments", []),
            audit=audit,
            scheduled_at=data.get("scheduled_at"),
            scheduled_action=data.get("scheduled_action"),
            attachments=data.get("attachments", []),
            retry_count=data.get("retry_count", 0),
            last_failure_at=data.get("last_failure_at"),
            paused=bool(data.get("paused", False)),
            token_usage=data.get("token_usage"),
            task_errors=data.get("task_errors", []) or [],
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
    pid: int = None
    subprocess_pid: int = None

    def to_dict(self) -> dict:
        d = {
            "agent_id": self.agent_id,
            "acquired_at": self.acquired_at,
            "expires_at": self.expires_at,
        }
        if self.pid is not None:
            d["pid"] = self.pid
        if self.subprocess_pid is not None:
            d["subprocess_pid"] = self.subprocess_pid
        return d

    @classmethod
    def from_dict(cls, data: dict):
        return cls(
            agent_id=data["agent_id"],
            acquired_at=data["acquired_at"],
            expires_at=data["expires_at"],
            pid=data.get("pid"),
            subprocess_pid=data.get("subprocess_pid"),
        )

    def is_expired(self) -> bool:
        expires = datetime.fromisoformat(self.expires_at)
        now = datetime.now(timezone.utc)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return now >= expires
