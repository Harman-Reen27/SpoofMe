"""Try a simulated iPhone location over USB, or send a clear command."""

import argparse
import asyncio
from contextlib import AsyncExitStack, redirect_stderr, redirect_stdout
import logging
import math
import os
import signal
import sys

# Support both direct script execution and package imports.
if __package__:
    from .connection import (DiscoveryError, connect_phone, error_message, redacted,
                             safe_text, usb_devices)
    from .session import (check_target, forget_record, read_record, save_record,
                          session_lock)
else:
    from connection import (DiscoveryError, connect_phone, error_message, redacted,
                            safe_text, usb_devices)
    from session import (check_target, forget_record, read_record, save_record,
                         session_lock)


# Keep our terminal output separate from hidden library messages.
CONSOLE = sys.stdout
LAYERS = ('USB connection', 'Pairing', 'Developer Mode', 'RSD tunnel',
          'Developer support', 'DVT provider', 'Location channel')
EXIT_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def say(message):
    # Write our own messages separately from the library output being hidden.
    print(message, file=CONSOLE, flush=True)


def coordinate(text, limit, name):
    # Reject invalid coordinates before opening a device connection.
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f'{name} must be a number.') from None
    if not math.isfinite(value) or not -limit <= value <= limit:
        raise argparse.ArgumentTypeError(f'{name} must be between {-limit} and {limit}.')
    return value


async def ask(message, stop=None):
    # Keep the USB tunnel running while waiting for terminal input.
    say(message)
    loop = asyncio.get_running_loop()
    answer = loop.create_future()
    fd = sys.stdin.fileno()

    # Read input only when the terminal has a line ready.
    def ready():
        try:
            data = os.read(fd, 4096)
            if not answer.done():
                answer.set_result(data.decode(errors='replace').strip())
        except Exception as exc:
            if not answer.done():
                answer.set_exception(exc)

    # Wait for input or a stop request while the tunnel keeps running.
    loop.add_reader(fd, ready)
    interrupted = asyncio.create_task(stop.wait()) if stop is not None else None
    try:
        if interrupted is not None:
            done, _ = await asyncio.wait((answer, interrupted), return_when=asyncio.FIRST_COMPLETED)
            if interrupted in done:
                return ''
        return await answer
    finally:
        # Remove the terminal callback and any unfinished wait.
        loop.remove_reader(fd)
        if interrupted is not None:
            interrupted.cancel()
            await asyncio.gather(interrupted, return_exceptions=True)


class ConnectionChecks:
    """Show connection details for doctor, keeping Set and Clear quieter."""

    def __init__(self, verbose=False):
        self.verbose = verbose
        self.passed = set()
        self.current = None

    async def run(self, name, operation, timeout=30):
        # Remember the current step so doctor can show where it stopped.
        self.current = name
        result = await run_step(operation, f'{name} failed', timeout)
        self.ok(name)
        return result

    def ok(self, name, detail=''):
        self.passed.add(name)
        self.current = None
        if self.verbose:
            say(f'[OK] {name}' + (f' ({detail})' if detail else ''))

    def failed(self):
        # Later steps were never reached if setup failed.
        if not self.verbose:
            return
        for name in LAYERS:
            if name not in self.passed:
                say(f'[FAIL] {name}' if name == self.current else f'[SKIP] {name}')


async def choose_device(checks, stop):
    from pymobiledevice3.usbmux import list_devices

    # Only show USB candidates and require an explicit selection.
    checks.current = 'USB connection'
    devices = await usb_devices(list_devices)
    if not devices:
        raise DiscoveryError('No USB device found. Connect the iPhone, unlock it and check Finder.')
    say('Connected USB devices:')
    for number, device in enumerate(devices, 1):
        say(f'  {number}. UDID: {redacted(device.serial)}')
    # An empty response cancels without opening a connection.
    choice = await ask('Choose a device (Enter to cancel):', stop)
    if not choice:
        return None
    if not choice.isdecimal() or not 1 <= int(choice) <= len(devices):
        raise DiscoveryError('Choose a number from the list.')
    checks.ok('USB connection')
    return devices[int(choice) - 1]


async def require_usb(device):
    from pymobiledevice3.usbmux import list_devices

    devices = await usb_devices(list_devices)
    # A new usbmux connection ID also catches a reboot or quick reconnection.
    if not any(d.serial == device.serial and d.devid == device.devid for d in devices):
        raise DiscoveryError('The USB connection changed. Location state is unknown. Reconnect and run clear.')


async def watch_usb(device):
    # Poll while a session is open so unplugging the cable wakes the CLI.
    while True:
        await require_usb(device)
        await asyncio.sleep(1)


async def open_rsd(phone, stack):
    from pymobiledevice3.remote import tunnel_service
    from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
    from pymobiledevice3.remote.userspace_tunnel import UserspaceDialPlane

    # Use the already selected USB connection. The high-level helper reconnects
    # by serial and can fall back to other transports, which this CLI avoids.
    previous = tunnel_service.USE_USERSPACE_TUNNEL
    tunnel_service.USE_USERSPACE_TUNNEL = True
    stack.callback(setattr, tunnel_service, 'USE_USERSPACE_TUNNEL', previous)
    # Build the USB tunnel and release its resources in reverse order on exit.
    proxy = await tunnel_service.CoreDeviceTunnelProxy.create(phone)
    stack.push_async_callback(proxy.close)
    tunnel = await stack.enter_async_context(proxy.start_tcp_tunnel())
    tunnel.client.tun.set_peer(tunnel.address)
    dialer = await stack.enter_async_context(UserspaceDialPlane(tunnel.client.tun, tunnel.address))
    # Discover services through this tunnel, then verify the phone identity.
    rsd = RemoteServiceDiscoveryService(
        (tunnel.address, tunnel.port), open_connection=dialer.dial,
        auxiliary_metadata=tunnel.auxiliary_metadata)
    stack.push_async_callback(rsd.close)
    await rsd.connect()
    if rsd.udid != phone.udid:
        raise DiscoveryError('The tunnel reached a different device. Nothing was sent.')
    return rsd


async def open_location(phone, stack, checks):
    from packaging.version import Version
    from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
    from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation

    # Location simulation needs Developer Mode enabled on the phone.
    checks.current = 'Developer Mode'
    if not phone.has_developer_mode:
        raise DiscoveryError('This location CLI currently needs iOS 17.4 or later.')
    status = await asyncio.wait_for(phone.get_value(
        domain='com.apple.security.mac.amfi', key='DeveloperModeStatus'), 10)
    if status is not True:
        raise DiscoveryError('Developer Mode is off or unreadable. Check it on the iPhone.')
    checks.ok('Developer Mode')
    # This implementation uses the USB tunnel available from iOS 17.4.
    checks.current = 'RSD tunnel'
    if Version(phone.product_version) < Version('17.4'):
        raise DiscoveryError('This location CLI currently needs iOS 17.4 or later. No Wi-Fi fallback is used.')
    rsd = await checks.run('RSD tunnel', open_rsd(phone, stack), 60)
    # Check that the phone advertises the developer service we need.
    checks.current = 'Developer support'
    if DvtProvider.RSD_SERVICE_NAME not in rsd.peer_info.get('Services', {}):
        raise DiscoveryError('The iPhone is not advertising its DVT developer service. Keep it unlocked and retry.')
    # Modern DVT uses Developer Mode and a tunnel; no disk image is needed here.
    checks.ok('Developer support', 'DVT available')
    # Open DVT and its location channel on the same service connection.
    dvt = DvtProvider(rsd)
    stack.push_async_callback(dvt.close)
    await checks.run('DVT provider', dvt.connect())
    location = LocationSimulation(dvt)
    stack.push_async_callback(location.close)
    await checks.run('Location channel', location.connect())
    return location, dvt


async def round_trip(dvt):
    # Verify the tunnel through remote lockdown without extra DVT methods.
    provider = dvt.lockdown
    probe = asyncio.create_task(provider.get_value(key='UniqueDeviceID'))
    disconnected = asyncio.create_task(dvt.dtx.wait_disconnected())
    # A reply verifies the connection only; Maps still confirms restoration.
    try:
        done, _ = await asyncio.wait((probe, disconnected), return_when=asyncio.FIRST_COMPLETED)
        if disconnected in done:
            raise DiscoveryError('The developer connection closed. Location state is unknown.')
        if await probe != provider.udid:
            raise DiscoveryError('Could not verify the phone through the tunnel. Location state is unknown.')
    finally:
        # Cancel whichever wait is still pending.
        for task in (probe, disconnected):
            task.cancel()
        await asyncio.gather(probe, disconnected, return_exceptions=True)


async def run_step(operation, message, timeout):
    # Add context to errors without printing the library's private details.
    try:
        return await asyncio.wait_for(operation, timeout)
    except DiscoveryError as exc:
        raise DiscoveryError(f'{message}: {exc}') from None
    except Exception as exc:
        raise DiscoveryError(f'{message}: {error_message(exc)}') from None


async def send_clear(device, location, dvt):
    # Record uncertainty before sending; a disk error must not block cleanup.
    try:
        save_record(device.serial, 'unknown')
    except OSError:
        say('Could not update the recovery file. Still trying Clear.')

    # Clear has no device reply, so check the connection before and after it.
    await run_step(require_usb(device), 'USB check before Clear failed', 12)
    await run_step(location.clear(), 'Could not send Clear', 15)
    await run_step(round_trip(dvt), 'Clear sent, but the connection check failed', 10)
    await run_step(require_usb(device), 'USB check after Clear failed', 12)

    # Keep the record until the real location is confirmed in Maps.
    try:
        save_record(device.serial, 'clear-sent')
    except OSError:
        raise DiscoveryError('Clear sent, but the recovery file could not be updated. Check Apple Maps.') from None
    say('Clear sent. Check your real location in Apple Maps.')


async def wait_with_connection(operation, device, dvt, stop):
    # Wake on input, Ctrl+C, USB loss or the developer connection closing.
    work = asyncio.create_task(operation)
    usb = asyncio.create_task(watch_usb(device))
    disconnected = asyncio.create_task(dvt.dtx.wait_disconnected())
    interrupted = asyncio.create_task(stop.wait())
    tasks = (work, usb, disconnected, interrupted)
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if usb in done:
            await usb
        if disconnected in done:
            raise DiscoveryError('The developer connection closed. Location state is unknown. Run clear after reconnecting.')
        if interrupted in done:
            return None
        return await work
    finally:
        # Release the other waits when one event has finished the session.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def run(args, checks, stop):
    # Resolve an earlier session before allowing a new simulated location.
    record = read_record()
    if record:
        say('A previous session still needs a location check.')
        if args.command == 'set':
            raise DiscoveryError('Run clear on the same iPhone before setting another location.')
    # Select a phone and keep recovery tied to its original device ID.
    device = await choose_device(checks, stop)
    if device is None:
        say('Cancelled.')
        return 0
    if args.command != 'doctor':
        check_target(record, device.serial)
    say('Connecting... Unlock the iPhone and tap Trust if asked.')
    # Keep all connections alive until the command and cleanup finish.
    stack = AsyncExitStack()
    try:
        phone = await checks.run('Pairing', connect_phone(device.serial), 45)
        stack.push_async_callback(phone.close)
        await require_usb(device)
        say('Model: ' + safe_text(phone.all_values.get('ProductType'), device.serial))
        say('iOS: ' + safe_text(phone.product_version, device.serial))
        location, dvt = await open_location(phone, stack, checks)
        if stop.is_set():
            say('Cancelled before sending a location command.')
            return 130
        # Doctor opens the services without sending a location command.
        if args.command == 'doctor':
            await require_usb(device)
            say('Connection checks passed. No location command was sent.')
            return 0
        # Clear needs a Maps confirmation before removing the recovery record.
        if args.command == 'clear':
            await send_clear(device, location, dvt)
            answer = await wait_with_connection(ask(
                'Tap the current-location arrow in Apple Maps. Is it showing your real location? '
                'Type yes to confirm (Enter skips):'), device, dvt, stop)
            # Recheck the connection before accepting the manual confirmation.
            if answer and answer.lower() == 'yes':
                await run_step(require_usb(device), 'USB check during confirmation failed', 12)
                await run_step(round_trip(dvt), 'Developer connection check during confirmation failed', 10)
                forget_record()
                say('Real location confirmed. Recovery record cleared.')
                return 0
            say('Location not confirmed. Recovery record kept; run clear to retry.')
            return 3

        # Record the target before sending anything, including a set that times out.
        save_record(device.serial, 'unknown')
        try:
            await require_usb(device)
            await run_step(location.set(args.latitude, args.longitude), 'Could not set location', 20)
            await require_usb(device)
            save_record(device.serial, 'active')
            say('Location sent. Check the blue dot in Apple Maps.')
            say('Keep this terminal open. Press Ctrl+C to send clear and exit.')
            await wait_with_connection(hold(), device, dvt, stop)
        finally:
            # This also runs after a failed Set: it may have reached the phone.
            await send_clear(device, location, dvt)
        say('Run clear to confirm the real location and finish the session.')
        return 3
    finally:
        # Clear runs above while the channel is still open, before closing resources.
        await asyncio.wait_for(stack.aclose(), 20)


async def hold():
    # Keep Set open until the signal or disconnect watcher ends the wait.
    await asyncio.Future()


async def start(args, checks):
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    # Signals request a normal exit so the live location channel can send clear.
    for sig in EXIT_SIGNALS:
        loop.add_signal_handler(sig, stop.set)
    try:
        return await run(args, checks, stop)
    finally:
        for sig in EXIT_SIGNALS:
            loop.remove_signal_handler(sig)


def main(argv=None):
    # Parse the command and validate coordinates before connecting.
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('doctor', help='Check the USB and developer connection without changing location')
    setter = commands.add_parser('set', help='Set a location and keep the connection open until Ctrl+C')
    setter.add_argument('latitude', type=lambda text: coordinate(text, 90, 'Latitude'))
    setter.add_argument('longitude', type=lambda text: coordinate(text, 180, 'Longitude'))
    commands.add_parser('clear', help='Send clear, then check the real location in Apple Maps')
    args = parser.parse_args(argv)
    # Device selection and Maps confirmation need an interactive terminal.
    if not sys.stdin.isatty():
        say('[ERROR] Run in Terminal so you can choose the iPhone.')
        return 1
    logging.disable(logging.CRITICAL)
    checks = ConnectionChecks(verbose=args.command == 'doctor')
    try:
        # Never print library exceptions, device objects, tunnel addresses or keys.
        with open(os.devnull, 'w') as quiet, redirect_stdout(quiet), redirect_stderr(quiet):
            from loguru import logger
            logger.remove()
            with session_lock():
                return asyncio.run(start(args, checks))
    # Print short errors instead of tracebacks containing device details.
    except ModuleNotFoundError:
        say('[ERROR] Install dependencies: .venv/bin/python -m pip install -r iphone/requirements.txt')
        return 2
    except (KeyboardInterrupt, EOFError):
        say('Interrupted. Run clear if the location still needs checking.')
        return 130
    except Exception as exc:
        checks.failed()
        say('[ERROR] ' + error_message(exc))
        if args.command != 'doctor':
            say('Run clear on the same iPhone if its location still needs checking.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
