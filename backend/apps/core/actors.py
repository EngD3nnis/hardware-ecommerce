"""Who is performing an action.

Every service that changes business data takes an `actor` argument. The actor
is written to the audit log, so "who did this?" always has an answer, whether
it was a staff member, a scheduled job, an external integration or an AI agent.
"""

from dataclasses import dataclass

from django.db import models


class ActorType(models.TextChoices):
    USER = "USER", "User"
    ADMIN = "ADMIN", "Admin / staff"
    SYSTEM = "SYSTEM", "System"
    AGENT = "AGENT", "AI agent"
    INTEGRATION = "INTEGRATION", "Integration"


@dataclass(frozen=True)
class Actor:
    type: ActorType
    label: str
    user_id: str | None = None
    # For agents: the AgentRun id, linking the action to the full run log.
    agent_run_id: str | None = None

    @classmethod
    def for_user(cls, user) -> "Actor":
        actor_type = ActorType.ADMIN if user.is_staff else ActorType.USER
        return cls(type=actor_type, label=user.get_username(), user_id=str(user.pk))

    @classmethod
    def system(cls, label: str = "system") -> "Actor":
        """Scheduled jobs, management commands and other internal processes."""
        return cls(type=ActorType.SYSTEM, label=label)

    @classmethod
    def integration(cls, name: str) -> "Actor":
        """An external system calling in, e.g. "mpesa" or "whatsapp"."""
        return cls(type=ActorType.INTEGRATION, label=name)

    @classmethod
    def agent(cls, name: str, run_id: str | None = None) -> "Actor":
        return cls(type=ActorType.AGENT, label=name, agent_run_id=run_id)

    @property
    def is_agent(self) -> bool:
        return self.type == ActorType.AGENT
