"""Private recovery records and a process lock for location sessions.

A device fingerprint ties recovery to the original phone. Atomic writes avoid
partial records; no coordinates are saved or replayed after a restart."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile

# Share the private cache and safe error messages with the connection layer.
if __package__:
    from .connection import DiscoveryError, private_cache
else:
    from connection import DiscoveryError, private_cache


# Keep the recovery record beside the private pairing cache, outside Git.
RECORD = Path(__file__).resolve().parent.parent / '.local' / 'location.json'


def device_key(serial):
    # Save a comparison fingerprint, not the full UDID or the selected coordinates.
    return hashlib.sha256(serial.encode()).hexdigest()


def read_record():
    # No record means this tool has no saved session waiting for confirmation.
    if not RECORD.exists():
        return None
    # Stop on a damaged record rather than guessing which phone needs recovery.
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
    # Compare the fingerprint before recovery so another iPhone cannot receive the command.
    if record and record['device'] != device_key(serial):
        raise DiscoveryError('The saved session belongs to another iPhone. Reconnect the original phone.')


def save_record(serial, state):
    private_cache()
    # Write a temporary file first so an interrupted save does not leave half a record.
    fd, name = tempfile.mkstemp(prefix='location-', dir=RECORD.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump({'version': 1, 'device': device_key(serial), 'state': state}, stream)
            # Flush Python's buffer, then ask the OS to persist the temporary file before replacing.
            stream.flush()
            os.fsync(stream.fileno())
        # After flushing the contents, replace the old record and flush its directory entry.
        os.replace(name, RECORD)
        sync_directory()
    finally:
        # Remove any leftover temporary file if the save failed before replacement.
        if os.path.exists(name):
            os.unlink(name)


def sync_directory():
    # Flush the directory after a replace or delete to help the change survive a crash.
    fd = os.open(RECORD.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def forget_record():
    # Remove this only after the real location has been confirmed in Maps.
    RECORD.unlink()
    sync_directory()


@contextmanager
def session_lock():
    private_cache()
    # Take an OS lock so two app or bridge processes cannot control the phone together.
    with open(RECORD.parent / 'location.lock', 'a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DiscoveryError('Another location command is running. Stop it before starting this one.') from None
        # The OS releases this lock when the process exits, including after a crash.
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
