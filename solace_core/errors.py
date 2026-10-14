"""Stable errors shared by human and machine clients."""
from solace_engine.errors import CheckpointNotFound, CorruptObject, InvalidProjectPath, RepositoryError
import sqlite3


class CoreError(Exception):
    def __init__(self, code, message, next_step, retryable=False):
        super().__init__(message)
        self.code, self.message, self.next_step, self.retryable = code, message, next_step, retryable

    def as_dict(self):
        return dict(code=self.code, message=self.message, next_step=self.next_step, retryable=self.retryable)


def invalid(message='The request is invalid.'):
    return CoreError('INVALID_REQUEST', message, 'Check the command arguments and protocol version.')


def classify(error):
    if isinstance(error, CoreError):
        return error
    if isinstance(error, CorruptObject):
        return CoreError('CORRUPT_OBJECT', 'Stored checkpoint content failed verification.', 'Inspect the store with doctor; preserve it for recovery.')
    if isinstance(error, CheckpointNotFound):
        return CoreError('CHECKPOINT_NOT_FOUND', 'The checkpoint does not exist.', 'List this project’s checkpoints.')
    if isinstance(error, InvalidProjectPath):
        return CoreError('PROJECT_UNAVAILABLE', 'The source or checkpoint store is unavailable.', 'Reconnect its drive or explicitly relink the project.')
    if isinstance(error, RepositoryError):
        return CoreError('CAPTURE_CHANGED', 'The project changed during capture.', 'Retry when project writes have stopped.', True)
    if isinstance(error, sqlite3.DatabaseError):
        return CoreError('DATABASE_CORRUPT', 'The database cannot be read or written safely.', 'Run doctor and preserve the database for explicit recovery.')
    if isinstance(error, FileExistsError):
        return CoreError('DESTINATION_EXISTS', 'The destination already exists.', 'Select a new destination; existing files are never overwritten.')
    if isinstance(error, PermissionError):
        return CoreError('PERMISSION_DENIED', 'The requested location is not accessible.', 'Check ownership and permissions.')
    if isinstance(error, OSError):
        return CoreError('STORE_UNAVAILABLE', 'Storage is unavailable.', 'Check the drive, free space, and permissions.')
    return CoreError('INTERNAL_ERROR', 'The operation could not finish.', 'Inspect service diagnostics using the operation ID.')
