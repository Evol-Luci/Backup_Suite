"""Domain errors raised by the local checkpoint engine."""


class RepositoryError(Exception):
    """Base class for checkpoint repository errors."""


class InvalidProjectPath(RepositoryError):
    """The requested project path is invalid or unavailable."""


class CheckpointNotFound(RepositoryError):
    """The requested checkpoint does not exist."""


class CorruptObject(RepositoryError):
    """A stored checkpoint object is corrupt or unreadable."""
