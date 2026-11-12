"""Find an iPhone over USB and show its connection details."""

import argparse
import asyncio
from contextlib import redirect_stderr, redirect_stdout
import io
import logging
import os
from pathlib import Path
import sys


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
    return ('Could not connect. Check the USB cable, unlock the iPhone '
            'and make sure it appears in Finder.')


async def usb_devices(list_devices):
    # Give Apple's USB service up to ten seconds to return connected devices.
    devices = await asyncio.wait_for(list_devices(usbmux_address=USBMUX), 10)
    # usbmux lists Wi-Fi connections too; this CLI only uses USB.
    return [d for d in devices if d.connection_type == 'USB']


async def inspect_phone(device, connect, cache):
    # Stop waiting after 45 seconds, including the trust prompt.
    async with asyncio.timeout(45):
        # Connect to the selected device and pair if needed.
        phone = await connect(serial=device.serial, connection_type='USB',
                              usbmux_address=USBMUX, autopair=True, pair_timeout=25,
                              pairing_records_cache_folder=cache, label='SpoofMe')
        async with phone:  # Close the connection on success or failure.
            if not phone.paired or not phone.session_id:
                raise DiscoveryError('Could not pair. Unlock the iPhone, tap Trust and try again.')
            values = phone.all_values
            product = values.get('ProductType', '')
            if not product.startswith('iPhone'):
                raise DiscoveryError('The selected device is not an iPhone.')
            # Make sure the connection belongs to the device selected earlier.
            if phone.udid != device.serial:
                raise DiscoveryError('Connected device does not match the selection. Reconnect and try again.')
            # Only show the basic device details.
            lines = ['[OK] USB connection', '[OK] Paired with this Mac', '[OK] iPhone detected',
                     'Name: ' + safe_text(values.get('DeviceName'), device.serial),
                     'Model: ' + safe_text(product, device.serial),
                     'iOS: ' + safe_text(values.get('ProductVersion'), device.serial),
                     'UDID: ' + redacted(device.serial)]
            # Older iOS versions don't have a Developer Mode setting.
            if not phone.has_developer_mode:
                lines.append('[INFO] This iOS version does not have Developer Mode.')
            else:
                # The bool helper treats a missing value as false. Keep it unknown here.
                try:
                    status = await phone.get_value(domain='com.apple.security.mac.amfi',
                                                   key='DeveloperModeStatus')
                except Exception:
                    status = None
                if status is True:
                    lines.append('[OK] Developer Mode: on')
                elif status is False:
                    lines.extend(['[WARN] Developer Mode: off',
                                  'Turn it on in Settings > Privacy & Security > Developer Mode.'])
                else:
                    lines.append('[WARN] Could not read Developer Mode status. Check it in Settings > Privacy & Security.')
            return lines


def private_cache():
    # Keep pairing files readable only by the current user.
    os.umask(0o077)
    CACHE.mkdir(parents=True, exist_ok=True, mode=0o700)
    CACHE.parent.chmod(0o700)
    CACHE.chmod(0o700)
    return CACHE


def main(argv=None):
    # --list stops before selection or pairing.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--list', action='store_true', help='List USB devices without connecting')
    args = parser.parse_args(argv)
    # Keep library output private; print only the fields collected below.
    logging.disable(logging.CRITICAL)
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            from pymobiledevice3.usbmux import list_devices
            from pymobiledevice3.lockdown import create_using_usbmux
            devices = asyncio.run(usb_devices(list_devices))
        if not devices:
            print('[WAIT] No USB device found. Connect the iPhone, unlock it and check Finder.')
            return 1
        print('Connected USB devices:')
        for number, device in enumerate(devices, 1):
            print(f'  {number}. UDID: {redacted(device.serial)}')
        if args.list:
            return 0
        # Require a person to choose instead of silently using the first device.
        if not sys.stdin.isatty():
            raise DiscoveryError('Run in Terminal to choose an iPhone.')
        choice = input('Choose a device (Enter to cancel): ').strip()
        if not choice:
            print('Cancelled.')
            return 0
        if not choice.isdecimal() or not 1 <= int(choice) <= len(devices):
            raise DiscoveryError('Invalid selection. Run again and choose a number from the list.')
        device = devices[int(choice) - 1]
        print('Connecting... Unlock the iPhone and tap Trust if asked.')
        # Hide library output while connecting, then print the device details.
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            lines = asyncio.run(inspect_phone(device, create_using_usbmux, private_cache()))
        print('\n'.join(lines))
        return 0
    # Turn failures into short messages and exit codes for the terminal.
    except ModuleNotFoundError:
        print('[ERROR] Install dependencies first: .venv/bin/python -m pip install -r iphone/requirements.txt')
        return 2
    except DiscoveryError as exc:
        print('[ERROR] ' + str(exc))
        return 1
    except (KeyboardInterrupt, EOFError):
        print('\nCancelled.')
        return 130
    except Exception as exc:
        print('[ERROR] ' + error_message(exc))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
