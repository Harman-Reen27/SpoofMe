"""USB discovery, pairing and safe display text shared by the app and diagnostics.

Pairing uses Apple's normal trust flow. Full device identifiers stay in Python,
and library exceptions are translated before reaching the window or terminal."""

import asyncio
import os
from pathlib import Path


# Keep pairing files outside Git and talk to the local Apple USB service.
CACHE = Path(__file__).resolve().parent.parent / '.local' / 'pairing'
USBMUX = '/var/run/usbmuxd'  # Use the local Apple USB service.


class DiscoveryError(Exception):
    """An error message without private device details."""

    def __init__(self, message, code='DEVICE_ERROR'):
        # Attach a stable code so Node can handle errors without parsing their text.
        super().__init__(message)
        self.code = code


def redacted(serial):
    # Show just the final four characters so phones can be distinguished without exposing full IDs.
    return '********' + str(serial)[-4:] if len(str(serial)) > 4 else '********'


def safe_text(value, serial):
    # Clean names from the phone before displaying them: no terminal controls or full ID.
    text = str(value or 'Unavailable').replace(str(serial), redacted(serial))
    return ''.join(c for c in text if c.isprintable())[:120]


def error_message(exc):
    # Replace library error text because it can include private device or pairing details.
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


async def usb_devices():
    from pymobiledevice3.usbmux import list_devices

    # Give the USB service ten seconds, then return an error instead of waiting forever.
    devices = await asyncio.wait_for(list_devices(usbmux_address=USBMUX), 10)
    # Filter out Wi-Fi entries here so every caller gets USB connections only.
    return [d for d in devices if d.connection_type == 'USB']


def check_phone(phone, serial):
    # Require a trusted session before opening any developer services.
    if not phone.paired or not phone.session_id:
        raise DiscoveryError('Could not pair. Unlock the iPhone, tap Trust and try again.')
    # Reject iPads and make sure the connection really belongs to the selected iPhone.
    if not phone.all_values.get('ProductType', '').startswith('iPhone'):
        raise DiscoveryError('The selected device is not an iPhone.')
    if phone.udid != serial:
        raise DiscoveryError('Connected device does not match the selection. Reconnect and try again.')


async def connect_phone(serial):
    from pymobiledevice3.lockdown import create_using_usbmux

    # Reuse an existing pairing, or wait briefly for Trust This Computer on the phone.
    async with asyncio.timeout(45):
        phone = await create_using_usbmux(
            serial=serial, connection_type='USB', usbmux_address=USBMUX,
            autopair=True, pair_timeout=25, pairing_records_cache_folder=private_cache(),
            label='SpoofMe')
        # Close a connection that fails identity checks so failed setup leaves no open session.
        try:
            check_phone(phone, serial)
        except BaseException:
            await phone.close()
            raise
        return phone


def private_cache():
    # Make the pairing folder private and create new credential files with owner-only access.
    os.umask(0o077)
    CACHE.mkdir(parents=True, exist_ok=True, mode=0o700)
    CACHE.parent.chmod(0o700)
    CACHE.chmod(0o700)
    return CACHE
