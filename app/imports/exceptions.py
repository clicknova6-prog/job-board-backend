"""Exceptions shared by the import pipeline."""


class TransientImportError(Exception):
    """A retryable import failure, such as a network or database outage."""

    def __init__(
        self,
        message: str,
        *,
        import_run_id: int | None = None,
    ) -> None:
        super().__init__(message)
        self.import_run_id = import_run_id


class InvalidFeedArchiveError(Exception):
    """A downloaded archive cannot provide a trustworthy XML feed."""
