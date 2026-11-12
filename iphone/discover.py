"""Find an iPhone over USB and show its connection details."""

import argparse
import asyncio
from contextlib import redirect_stderr, redirect_stdout
import os
import logging
import sys


# Allow running this file directly or importing it as a module.
if __package__:
    from .connection import (DiscoveryError, connect_phone, error_message,
                             redacted, safe_text, usb_devices)
else:
    from connection import (DiscoveryError, connect_phone, error_message,
                            redacted, safe_text, usb_devices)


async def inspect_phone(device):
    # Use the same pairing and identity checks as the location commands.
    async with asyncio.timeout(45):
        async with await connect_phone(device.serial) as phone:
            values = phone.all_values
            product = values.get('ProductType', '')
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
                    lines.append('[WARN] Developer Mode: off')
                else:
                    lines.append('[WARN] Developer Mode: unknown')
            return lines


def main(argv=None):
    # --list stops before selection or pairing.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--list', action='store_true', help='List USB devices without connecting')
    args = parser.parse_args(argv)
    # Keep library output private; print only the fields collected below.
    logging.disable(logging.CRITICAL)
    try:
        with open(os.devnull, 'w') as quiet, redirect_stdout(quiet), redirect_stderr(quiet):
            from pymobiledevice3.usbmux import list_devices
            devices = asyncio.run(usb_devices(list_devices))
        if not devices:
            print('[WAIT] No USB device found. Connect the iPhone, unlock it and check Finder.')
            return 1
        # The list can include other Apple devices; connection checks verify iPhones.
        print('Connected USB devices:')
        for number, device in enumerate(devices, 1):
            print(f'  {number}. UDID: {redacted(device.serial)}')
        if args.list:
            return 0
        # Require a person to choose instead of silently using the first device.
        if not sys.stdin.isatty():
            raise DiscoveryError('Run in Terminal to choose an iPhone.')
        # Choose a listed device or leave the phone untouched by cancelling.
        choice = input('Choose a device (Enter to cancel): ').strip()
        if not choice:
            print('Cancelled.')
            return 0
        if not choice.isdecimal() or not 1 <= int(choice) <= len(devices):
            raise DiscoveryError('Invalid selection. Run again and choose a number from the list.')
        device = devices[int(choice) - 1]
        print('Connecting... Unlock the iPhone and tap Trust if asked.')
        # Hide library output while connecting, then print the device details.
        with open(os.devnull, 'w') as quiet, redirect_stdout(quiet), redirect_stderr(quiet):
            lines = asyncio.run(inspect_phone(device))
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
