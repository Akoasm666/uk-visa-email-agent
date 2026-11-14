class SendStatusUnknownError(Exception):
    """Provider may have accepted the message, but local confirmation is incomplete."""
