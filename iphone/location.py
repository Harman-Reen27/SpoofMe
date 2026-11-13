"""One live USB location session, including setup, commands and cleanup.

The trusted connection, developer tunnel and location channel share a lifetime.
The bridge supplies requests; this module has no terminal prompts or UI code.
Failed commands leave recovery pending until restoration is checked in Maps."""

import asyncio
from contextlib import AsyncExitStack
import math

# Support both direct script execution and package imports.
if __package__:
    from .connection import DiscoveryError, connect_phone, error_message, safe_text
    from .connection import usb_devices
    from .session import check_target, forget_record, read_record, save_record
else:
    from connection import DiscoveryError, connect_phone, error_message, safe_text
    from connection import usb_devices
    from session import check_target, forget_record, read_record, save_record


async def require_usb(device):
    devices = await usb_devices()
    # Compare both IDs so a reboot or quick replug does not silently reuse an old connection.
    if not any(d.serial == device.serial and d.devid == device.devid for d in devices):
        raise DiscoveryError('The USB connection changed. Location state is unknown. Reconnect and run clear.')


async def watch_usb(device):
    # Check USB once a second while the session is open, even between button presses.
    while True:
        await require_usb(device)
        await asyncio.sleep(1)


async def open_rsd(phone, stack):
    from pymobiledevice3.remote import tunnel_service
    from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
    from pymobiledevice3.remote.userspace_tunnel import UserspaceDialPlane

    # Build on the explicitly selected USB connection. The high-level helper reconnects by serial
    # and can fall back to another transport, which could leave the USB-only path.
    previous = tunnel_service.USE_USERSPACE_TUNNEL
    # A userspace tunnel avoids creating a privileged system network interface.
    tunnel_service.USE_USERSPACE_TUNNEL = True
    stack.callback(setattr, tunnel_service, 'USE_USERSPACE_TUNNEL', previous)
    # Register cleanup as each resource opens, so closing unwinds the tunnel in reverse order.
    proxy = await tunnel_service.CoreDeviceTunnelProxy.create(phone)
    stack.push_async_callback(proxy.close)
    tunnel = await stack.enter_async_context(proxy.start_tcp_tunnel())
    tunnel.client.tun.set_peer(tunnel.address)
    dialer = await stack.enter_async_context(UserspaceDialPlane(tunnel.client.tun, tunnel.address))
    # Ask the phone which services it offers and double-check its identity through the tunnel.
    rsd = RemoteServiceDiscoveryService(
        (tunnel.address, tunnel.port), open_connection=dialer.dial,
        auxiliary_metadata=tunnel.auxiliary_metadata)
    stack.push_async_callback(rsd.close)
    await rsd.connect()
    if rsd.udid != phone.udid:
        raise DiscoveryError('The tunnel reached a different device. Nothing was sent.')
    return rsd


async def open_location(phone, stack):
    from packaging.version import Version
    from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
    from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation

    # Use the USB developer connection available from iOS 17.4 onward.
    if Version(phone.product_version) < Version('17.4'):
        raise DiscoveryError('Location simulation currently needs iOS 17.4 or later.')
    status = await run_step(phone.get_value(
        domain='com.apple.security.mac.amfi', key='DeveloperModeStatus'),
        'Could not read Developer Mode', 10)
    if status is not True:
        raise DiscoveryError('Developer Mode is off or unreadable. Check it on the iPhone.')
    rsd = await run_step(open_rsd(phone, stack), 'Could not open the developer tunnel', 60)
    # Stop here if the phone is not offering the DVT service used for location simulation.
    if DvtProvider.RSD_SERVICE_NAME not in rsd.peer_info.get('Services', {}):
        raise DiscoveryError('The iPhone is not advertising its DVT developer service. Keep it unlocked and retry.')
    # Open DVT first, then the location channel. Both stay alive for the whole session.
    dvt = DvtProvider(rsd)
    stack.push_async_callback(dvt.close)
    await run_step(dvt.connect(), 'Could not open developer services', 30)
    location = LocationSimulation(dvt)
    stack.push_async_callback(location.close)
    await run_step(location.connect(), 'Could not open the location channel', 30)
    return location, dvt


async def round_trip(dvt):
    # Use a small remote-lockdown read to check the tunnel, avoiding optional DVT methods.
    provider = dvt.lockdown
    probe = asyncio.create_task(provider.get_value(key='UniqueDeviceID'))
    disconnected = asyncio.create_task(dvt.dtx.wait_disconnected())
    # This reply only proves the connection works. Restoration still needs a check in Maps.
    try:
        done, _ = await asyncio.wait((probe, disconnected), return_when=asyncio.FIRST_COMPLETED)
        if disconnected in done:
            raise DiscoveryError('The developer connection closed. Location state is unknown.')
        if await probe != provider.udid:
            raise DiscoveryError('Could not verify the phone through the tunnel. Location state is unknown.')
    finally:
        # Cancel the leftover wait so background tasks do not build up after each check.
        for task in (probe, disconnected):
            task.cancel()
        await asyncio.gather(probe, disconnected, return_exceptions=True)


async def run_step(operation, message, timeout):
    # Add the failed step to a safe error message and put a deadline on the device request.
    try:
        return await asyncio.wait_for(operation, timeout)
    except DiscoveryError as exc:
        raise DiscoveryError(f'{message}: {exc}') from None
    except Exception as exc:
        raise DiscoveryError(f'{message}: {error_message(exc)}') from None


def validate_coordinates(latitude, longitude):
    # Reject booleans as well as invalid ranges; Python otherwise treats True as the number 1.
    for value, limit, name in ((latitude, 90, 'Latitude'), (longitude, 180, 'Longitude')):
        if (type(value) not in (int, float) or not -limit <= value <= limit
                or not math.isfinite(value)):
            raise DiscoveryError(f'{name} must be a number between {-limit} and {limit}.', 'INVALID_REQUEST')


class LocationSession:
    """Own the selected phone, its connections and the recovery state."""

    def __init__(self):
        # Start idle, or unknown if recovery is pending. Never replay saved coordinates.
        self.device = None
        self.phone = self.location = self.dvt = None
        self.stack = None
        self.monitor = None
        self.ready = False
        # dirty tracks whether this process may need to send Clear when it closes.
        self.dirty = False
        self.state = 'unknown' if read_record() else 'idle'

    def status(self):
        # Return just what the window needs. The full device ID never leaves Python.
        phone = self.phone
        target = None if phone is None else {
            'name': safe_text(phone.all_values.get('DeviceName'), self.device.serial),
            'model': safe_text(phone.all_values.get('ProductType'), self.device.serial),
            'ios': safe_text(phone.product_version, self.device.serial),
        }
        return {'prepared': self.ready, 'locationState': self.state,
                'recoveryRequired': read_record() is not None, 'device': target}

    async def prepare(self, device):
        # Check the saved target before changing phones, so recovery cannot affect another device.
        check_target(read_record(), device.serial)
        if self.ready and self.device == device:
            await require_usb(device)
            return self.status()
        await self.close()

        # Open the phone once and keep its tunnel and location channel until cleanup.
        self.device = device
        self.dirty = False
        self.state = 'unknown' if read_record() else 'idle'
        self.stack = AsyncExitStack()
        try:
            await run_step(require_usb(device), 'USB connection failed', 12)
            self.phone = await run_step(connect_phone(device.serial), 'Pairing failed', 45)
            self.stack.push_async_callback(self.phone.close)
            self.location, self.dvt = await open_location(self.phone, self.stack)
            await require_usb(device)
            self.ready = True
            self.monitor = asyncio.create_task(self._watch_connection())
            return self.status()
        except BaseException:
            # If shutdown interrupts setup, release every resource opened so far.
            await self._release()
            raise

    async def _require_ready(self):
        # Require an explicitly selected, live USB session before any location operation.
        if not self.ready:
            raise DiscoveryError('Select and prepare the iPhone first.', 'NOT_PREPARED')
        check_target(read_record(), self.device.serial)
        await require_usb(self.device)
        if not self.ready:
            raise DiscoveryError('The developer connection closed. Reconnect and run Clear.', 'DEVICE_DISCONNECTED')

    def _unknown(self):
        # Mark failures as unknown and try to keep the original target recorded for recovery.
        self.state = 'unknown'
        if self.dirty or read_record():
            try:
                save_record(self.device.serial, 'unknown')
            except OSError:
                pass

    async def _watch_connection(self):
        # Watch USB and DVT together so a lost cable or developer connection changes the state.
        usb = asyncio.create_task(watch_usb(self.device))
        dvt = asyncio.create_task(self.dvt.dtx.wait_disconnected())
        try:
            await asyncio.wait((usb, dvt), return_when=asyncio.FIRST_COMPLETED)
            self.ready = False
            self._unknown()
        finally:
            # Collect both tasks, including their errors, rather than leaving an unhandled
            # traceback.
            for task in (usb, dvt):
                task.cancel()
            await asyncio.gather(usb, dvt, return_exceptions=True)

    async def set(self, latitude, longitude):
        validate_coordinates(latitude, longitude)
        await self._require_ready()

        # Allow another point during a healthy simulation, but not over an unresolved session.
        if read_record() and not (self.dirty and self.state == 'simulated'):
            raise DiscoveryError('Run Clear and confirm the real location before setting another point.', 'RECOVERY_REQUIRED')
        # Persist uncertainty before Set: the command might arrive even if its request times out.
        save_record(self.device.serial, 'unknown')
        self.dirty = True
        self.state = 'unknown'
        try:
            await run_step(self.location.set(latitude, longitude), 'Could not set location', 20)
            await self._require_ready()
            save_record(self.device.serial, 'active')
            self.state = 'simulated'
            return self.status()
        except BaseException:
            # A timeout does not mean Set missed the phone, so keep recovery pending.
            self._unknown()
            raise

    async def clear(self):
        # Let Clear run again on the original phone, even if the last Set did not finish.
        await self._require_ready()
        self.dirty = True
        self.state = 'unknown'
        try:
            save_record(self.device.serial, 'unknown')
        except OSError:
            # A disk failure should not prevent a best-effort restoration request to the phone.
            pass
        try:
            await run_step(self.location.clear(), 'Could not send Clear', 15)
            await run_step(round_trip(self.dvt), 'Clear sent, but the connection check failed', 10)
            await self._require_ready()
            save_record(self.device.serial, 'clear-sent')
            self.state = 'clear-sent'
            # Clear was sent, but the recovery record stays until the Maps confirmation.
            self.dirty = False
            return self.status()
        except BaseException:
            self._unknown()
            raise

    async def confirm_restored(self):
        # Call this only after explicitly checking the real-location dot in Apple Maps.
        await self._require_ready()
        if self.state != 'clear-sent' or self.dirty:
            raise DiscoveryError('Send Clear before confirming the real location.', 'INVALID_STATE')
        await run_step(round_trip(self.dvt), 'Connection check during confirmation failed', 10)
        await require_usb(self.device)
        forget_record()
        self.state = 'real-confirmed'
        return self.status()

    async def _release(self):
        # Stop the monitor first, then close the resources it was watching.
        self.ready = False
        if self.monitor is not None:
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
            self.monitor = None
        stack, self.stack = self.stack, None
        try:
            if stack is not None:
                await asyncio.wait_for(stack.aclose(), 20)
        finally:
            self.phone = self.location = self.dvt = None

    async def close(self):
        # Attempt Clear before closing the channel; failed cleanup stays unresolved.
        cleanup_error = None
        try:
            if self.dirty and self.location is not None:
                await self.clear()
        except Exception as exc:
            self._unknown()
            cleanup_error = error_message(exc)
        finally:
            try:
                await self._release()
            except Exception as exc:
                cleanup_error = error_message(exc)
        result = self.status()
        if cleanup_error:
            result['cleanupError'] = cleanup_error
        return result
