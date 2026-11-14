from __future__ import annotations

from visa_agent.domain.enums import ErrorCode


class DomainError(Exception):
    """Typed domain failure. Prefer this over bare Exception at boundaries."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        details: tuple[str, ...] = (),
    ) -> None:
        self.code = code
        self.message = message
        self.details = details
        super().__init__(str(self))

    def __str__(self) -> str:
        if self.details:
            joined = "; ".join(self.details)
            return f"{self.code}: {self.message} ({joined})"
        return f"{self.code}: {self.message}"
