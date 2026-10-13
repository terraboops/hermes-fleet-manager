"""Pre-model intake: durable triage card per inbound message, plus a completion check."""
from .capture import Intake, IntakeEvent, intake_key, intake_enabled  # noqa: F401
from .completion import CompletionRejected, check_completion, guarded_complete  # noqa: F401
