"""Keep enough information to recognise a phone after an interrupted session."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile

# Allow the CLI to run directly or through the iphone package.
if __package__:
    from .connection import DiscoveryError, private_cache
else:
    from connection import DiscoveryError, private_cache


# Keep recovery data beside the ignored pairing cache.
RECORD = Path(__file__).resolve().parent.parent / '.local' / 'location.json'


def device_key(serial):
    # Store a fingerprint, not the phone's full identifier or coordinates.
    return hashlib.sha256(serial.encode()).hexdigest()


def read_record():
    # No saved file means there is no unresolved session from this tool.
    if not RECORD.exists():
        return None
    # Reject damaged records instead of losing the original device association.
    try:
        record = json.loads(RECORD.read_text())
        key = record['device']
        if (record['version'] != 1 or not isinstance(key, str) or len(key) != 64
                or any(c not in '0123456789abcdef' for c in key)
                or record['state'] not in {'unknown', 'active', 'clear-sent'}):
            raise ValueError()
        return record
    except (OSError, ValueError, KeyError, TypeError):
        raise DiscoveryError('Could not read the saved session. Keep .local/location.json for recovery.') from None


def check_target(record, serial):
    # Recovery commands must refer to the phone that started the session.
    if record and record['device'] != device_key(serial):
        raise DiscoveryError('The saved session belongs to another iPhone. Reconnect the original phone.')


def save_record(serial, state):
    private_cache()
    # Replace atomically so an interrupted write won't leave half a JSON file.
    fd, name = tempfile.mkstemp(prefix='location-', dir=RECORD.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump({'version': 1, 'device': device_key(serial), 'state': state}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        # Flush the file before replacing the previous record.
        os.replace(name, RECORD)
        sync_directory()
    finally:
        # Remove a temporary file if the write failed before replacement.
        if os.path.exists(name):
            os.unlink(name)


def sync_directory():
    # Flush the directory entry after replacing or deleting a record.
    fd = os.open(RECORD.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def forget_record():
    # Called only after the real location has been confirmed in Maps.
    RECORD.unlink()
    sync_directory()


@contextmanager
def session_lock():
    private_cache()
    # One location command at a time, including across separate terminals.
    with open(RECORD.parent / 'location.lock', 'a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DiscoveryError('Another location command is running. Stop it before starting this one.') from None
        # The OS releases the lock on exit, even if the command fails.
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
