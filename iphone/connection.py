"""Shared USB connection and output helpers."""

import asyncio
import os
from pathlib import Path


# Store app pairing files locally and use Apple's USB socket.
CACHE = Path(__file__).resolve().parent.parent / '.local' / 'pairing'
USBMUX = '/var/run/usbmuxd'  # Use the local Apple USB service.


class DiscoveryError(Exception):
    """An error message without private device details."""


def redacted(serial):
    # Keep only the last four characters for recognising a device in the list.
    return '********' + str(serial)[-4:] if len(str(serial)) > 4 else '********'


def safe_text(value, serial):
    # Device names can contain terminal controls or the full identifier.
    text = str(value or 'Unavailable').replace(str(serial), redacted(serial))
    return ''.join(c for c in text if c.isprintable())[:120]


def error_message(exc):
    # Library exception messages can include UDIDs and pairing records.
    if isinstance(exc, DiscoveryError):
        return str(exc)
    name = type(exc).__name__
    if name in {'PairingDialogResponsePendingError', 'NotPairedError', 'FatalPairingError',
                'PairingError', 'InvalidHostIDError'}:
        return 'Unlock the iPhone and tap Trust This Computer, then try again.'
    if name == 'UserDeniedPairingError':
        return 'Trust was declined. Reconnect and tap Trust This Computer on the iPhone.'
    if name in {'PasswordRequiredError', 'PasscodeRequiredError', 'DeviceLockedError'}:
        return 'Unlock the iPhone with its passcode and try again.'
    if isinstance(exc, TimeoutError):
        return 'Connection timed out. Check the cable, unlock the iPhone and try again.'
    if isinstance(exc, PermissionError):
        return 'Access denied. Try running in Terminal without sudo and check folder permissions.'
    if name in {'InvalidServiceError', 'StartServiceError'}:
        return 'The developer service is unavailable. Check Developer Mode and try again.'
    if name == 'UnrecognizedSelectorError':
        return 'This developer request is not supported by the phone.'
    return 'The iPhone request failed. Unlock the phone and try again.'


async def usb_devices(list_devices):
    # Give Apple's USB service up to ten seconds to return connected devices.
    devices = await asyncio.wait_for(list_devices(usbmux_address=USBMUX), 10)
    # usbmux lists Wi-Fi connections too; this CLI only uses USB.
    return [d for d in devices if d.connection_type == 'USB']


def check_phone(phone, serial):
    # Trust and identity must both match before using developer services.
    if not phone.paired or not phone.session_id:
        raise DiscoveryError('Could not pair. Unlock the iPhone, tap Trust and try again.')
    # Reject other Apple devices and connections to an unexpected phone.
    if not phone.all_values.get('ProductType', '').startswith('iPhone'):
        raise DiscoveryError('The selected device is not an iPhone.')
    if phone.udid != serial:
        raise DiscoveryError('Connected device does not match the selection. Reconnect and try again.')


async def connect_phone(serial):
    from pymobiledevice3.lockdown import create_using_usbmux

    # Reuse trust where possible and allow time for an on-device pairing prompt.
    async with asyncio.timeout(45):
        phone = await create_using_usbmux(
            serial=serial, connection_type='USB', usbmux_address=USBMUX,
            autopair=True, pair_timeout=25, pairing_records_cache_folder=private_cache(),
            label='SpoofMe')
        # Close a rejected connection before returning control to the caller.
        try:
            check_phone(phone, serial)
        except BaseException:
            await phone.close()
            raise
        return phone


def private_cache():
    # Keep pairing files readable only by the current user.
    os.umask(0o077)
    CACHE.mkdir(parents=True, exist_ok=True, mode=0o700)
    CACHE.parent.chmod(0o700)
    CACHE.chmod(0o700)
    return CACHE
