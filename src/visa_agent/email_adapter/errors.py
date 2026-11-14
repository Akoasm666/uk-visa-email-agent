class SendStatusUnknownError(Exception):
    """Provider may have accepted the message, but local confirmation is incomplete."""


class SendPartialAcceptedError(Exception):
    """Some recipients were accepted and others refused — must not wholesale retry."""

    def __init__(
        self,
        *,
        accepted: list[str],
        refused: dict[str, object],
        provider_response: str,
    ) -> None:
        self.accepted = list(accepted)
        self.refused = dict(refused)
        self.provider_response = provider_response
        super().__init__(provider_response)
