"""Translate newline-delimited JSON requests into operations on one phone.

Increasing request IDs match replies to calls. Library output is suppressed so
private diagnostics cannot corrupt the protocol. Signals or parent-pipe closure
trigger cleanup while unresolved recovery information remains on disk."""

import asyncio
import json
import logging
import os
import secrets
import signal
import sys

# Support direct execution by Node as well as normal package imports.
if __package__:
    from .connection import DiscoveryError, error_message, redacted, usb_devices
    from .location import LocationSession
    from .session import session_lock
else:
    from connection import DiscoveryError, error_message, redacted, usb_devices
    from location import LocationSession
    from session import session_lock


# Accept only these commands and fields, keeping the JSON interface small.
FIELDS = {
    'discover': set(), 'status': set(), 'prepare': {'device'},
    'set': {'device', 'latitude', 'longitude'}, 'clear': {'device'},
    'confirm': {'device', 'confirmed'}, 'shutdown': set(),
}
MAX_LINE = 16384


class Bridge:
    def __init__(self):
        # Keep handles and the prepared session in memory for this process only.
        self.session = LocationSession()
        self.devices = {}
        self.selected = None
        self.last_id = 0
        self.stopping = False

    async def discover(self):
        # Give each USB connection a temporary handle so Node never needs the full UDID.
        devices = await usb_devices()
        # Include the USB connection ID: reconnecting produces a new handle for the same phone.
        old = {(d.serial, d.devid): handle for handle, d in self.devices.items()}
        self.devices = {old.get((d.serial, d.devid)) or secrets.token_hex(12): d for d in devices}
        return {'devices': [{'id': handle, 'name': 'USB device',
                             'identifier': redacted(device.serial)}
                            for handle, device in self.devices.items()]}

    def validate(self, message):
        # Accept increasing integer IDs and never echo arbitrary input as an identifier.
        if not isinstance(message, dict):
            raise DiscoveryError('Expected a JSON object.', 'INVALID_REQUEST')
        identifier = request_id(message)
        if identifier is None:
            raise DiscoveryError('Request ID must be a positive safe integer.', 'INVALID_REQUEST')
        if identifier <= self.last_id:
            raise DiscoveryError('Request IDs must increase; commands are not replayed.', 'INVALID_REQUEST')
        # Consume the ID even for a rejected command, so the same request cannot be replayed.
        self.last_id = identifier

        # Reject unknown operations and extra fields before they can reach a phone.
        command = message.get('command')
        if not isinstance(command, str) or command not in FIELDS:
            raise DiscoveryError('Unknown command.', 'INVALID_REQUEST')
        if set(message) != {'id', 'command'} | FIELDS[command]:
            raise DiscoveryError('The command fields do not match.', 'INVALID_REQUEST')
        # The location session checks coordinates; this layer checks the message shape.
        if command == 'confirm' and message['confirmed'] is not True:
            raise DiscoveryError('Confirm only after checking the real location in Maps.', 'INVALID_REQUEST')
        return command

    async def dispatch(self, message):
        command = self.validate(message)
        if command == 'discover':
            return await self.discover()
        if command == 'status':
            return self.session.status()
        if command == 'shutdown':
            self.stopping = True
            result = await self.session.close()
            if result.get('cleanupError'):
                raise DiscoveryError(result['cleanupError'], 'CLEANUP_FAILED')
            return result

        # Require a handle from this running bridge, not an ID supplied from elsewhere.
        handle = message['device']
        if not isinstance(handle, str) or handle not in self.devices:
            raise DiscoveryError('Discover devices and select the iPhone again.', 'INVALID_DEVICE')
        device = self.devices[handle]
        if command == 'prepare':
            self.selected = None
            result = await self.session.prepare(device)
            self.selected = handle
            return result
        if handle != self.selected or device != self.session.device:
            raise DiscoveryError('The request does not match the prepared iPhone.', 'INVALID_DEVICE')

        # Send each operation to the one prepared session instead of opening new connections.
        if command == 'set':
            return await self.session.set(message['latitude'], message['longitude'])
        if command == 'clear':
            return await self.session.clear()
        return await self.session.confirm_restored()


def failure(request_id, exc):
    # Return short application messages; private library exception text stays hidden.
    code = exc.code if isinstance(exc, DiscoveryError) else (
        'TIMEOUT' if isinstance(exc, TimeoutError) else 'DEVICE_ERROR')
    if isinstance(exc, ModuleNotFoundError):
        code = 'MISSING_DEPENDENCY'
    return {'id': request_id, 'ok': False,
            'error': {'code': code, 'message': error_message(exc)}}


def request_id(message):
    # Return null for an invalid ID, rather than copying potentially private input.
    value = message.get('id') if isinstance(message, dict) else None
    return value if type(value) is int and 1 <= value <= 9007199254740991 else None


async def until_stopped(operation, stop):
    # Let an exit signal interrupt the current wait so cleanup can start promptly.
    work = asyncio.create_task(operation)
    interrupted = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait((work, interrupted), return_when=asyncio.FIRST_COMPLETED)
        if interrupted in done:
            return None
        return await work
    finally:
        for task in (work, interrupted):
            task.cancel()
        await asyncio.gather(work, interrupted, return_exceptions=True)


async def serve(bridge, output):
    # Read newline-delimited JSON, allowing split writes but limiting message size.
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=MAX_LINE)
    transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        loop.add_signal_handler(sig, stop.set)

    # A closed parent pipe interrupts an active command so cleanup can start.
    eof = loop.create_task(watch_eof(reader, stop, bridge.session))
    try:
        while not stop.is_set():
            try:
                line = await until_stopped(reader.readline(), stop)
                if not line:
                    break
            except ValueError:
                reply(output, failure(None, DiscoveryError('Command is too long.', 'INVALID_REQUEST')))
                break

            # Finish one command before reading the next, and reply using its request ID.
            message = None
            try:
                message = json.loads(line, parse_constant=reject_constant)
                result = await until_stopped(asyncio.wait_for(bridge.dispatch(message), 180), stop)
                if stop.is_set():
                    break
                response = {'id': request_id(message), 'ok': True, 'result': result}
            except (ValueError, UnicodeError):
                response = failure(None, DiscoveryError('Invalid JSON command.', 'INVALID_REQUEST'))
            except Exception as exc:
                response = failure(request_id(message), exc)
            reply(output, response)
            # Deliver the shutdown reply before leaving the reader loop and closing the pipe.
            if bridge.stopping:
                break
    finally:
        # Run cleanup for normal shutdown, closed pipes, signals and invalid oversized input.
        eof.cancel()
        await asyncio.gather(eof, return_exceptions=True)
        try:
            await bridge.session.close()
        finally:
            transport.close()
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                loop.remove_signal_handler(sig)


async def watch_eof(reader, stop, session):
    # Watch the input pipe without stealing bytes from the command reader.
    while not (reader.at_eof() and session.dirty):
        await asyncio.sleep(0.2)
    stop.set()


def reject_constant(value):
    # Reject nonstandard JSON constants such as NaN before dispatching a command.
    raise ValueError()


def reply(output, message):
    # Flush one complete JSON line so Node can resolve the matching request promptly.
    output.write(json.dumps(message, allow_nan=False, separators=(',', ':')) + '\n')
    output.flush()


def main():
    # Keep a private copy of the response pipe, then silence ordinary library output. That also
    # catches native writes to stdout, which would otherwise break the JSON stream.
    output = os.fdopen(os.dup(sys.stdout.fileno()), 'w', buffering=1)
    with open(os.devnull, 'w') as quiet:
        # File-descriptor redirection also silences C extensions that bypass Python's print streams.
        os.dup2(quiet.fileno(), 1)
        os.dup2(quiet.fileno(), 2)
        logging.disable(logging.CRITICAL)
        try:
            from loguru import logger
            logger.remove()
            with session_lock():
                asyncio.run(serve(Bridge(), output))
            return 0
        except (BrokenPipeError, KeyboardInterrupt):
            return 1
        except Exception as exc:
            reply(output, failure(None, exc))
            return 1
        finally:
            output.close()


if __name__ == '__main__':
    raise SystemExit(main())
