"""Standalone USB discovery and Doctor checks without opening Electron.

Basic discovery reads the model, iOS version and Developer Mode status. Doctor
also opens the same developer connection used by the app, without sending a
location command. Neither check needs a map API key."""

import argparse
import asyncio
from contextlib import redirect_stderr, redirect_stdout
import os
import logging
import sys


# Allow the same diagnostics to run directly or through package imports.
if __package__:
    from .connection import (DiscoveryError, connect_phone, error_message,
                             redacted, safe_text, usb_devices)
    from .location import LocationSession, round_trip, run_step
    from .session import session_lock
else:
    from connection import (DiscoveryError, connect_phone, error_message,
                            redacted, safe_text, usb_devices)
    from location import LocationSession, round_trip, run_step
    from session import session_lock


async def doctor(device):
    """Check the app's complete connection path without changing location."""
    # Share the app's lock so a diagnostic cannot compete with an active simulation.
    with session_lock():
        session = LocationSession()
        try:
            # Preparing opens USB, pairing, the RSD tunnel, DVT and its location channel.
            # Reusing this path keeps Doctor aligned with what the app actually needs.
            await session.prepare(device)
            await run_step(round_trip(session.dvt), 'Developer connection check failed', 10)
            await session._require_ready()
            return ['[OK] USB connection', '[OK] Paired with this Mac',
                    '[OK] Developer Mode', '[OK] Developer tunnel (RSD)',
                    '[OK] Developer service (DVT)', '[OK] Location channel',
                    '[OK] Reply received from the selected iPhone',
                    '[INFO] No location command was sent. This checks the connection, not GPS.']
        finally:
            # A fresh diagnostic session never sets dirty, so close only releases resources.
            # Any existing recovery record stays untouched, even when a check fails.
            await session.close()


async def inspect_phone(device):
    # Reuse the app's trust and identity checks for this standalone USB check.
    async with asyncio.timeout(45):
        async with await connect_phone(device.serial) as phone:
            values = phone.all_values
            product = values.get('ProductType', '')
            # Print the basic phone details and keep the full identifier hidden.
            lines = ['[OK] USB connection', '[OK] Paired with this Mac', '[OK] iPhone detected',
                     'Name: ' + safe_text(values.get('DeviceName'), device.serial),
                     'Model: ' + safe_text(product, device.serial),
                     'iOS: ' + safe_text(values.get('ProductVersion'), device.serial),
                     'UDID: ' + redacted(device.serial)]
            # Skip the Developer Mode query on older versions that do not have the setting.
            if not phone.has_developer_mode:
                lines.append('[INFO] This iOS version does not have Developer Mode.')
            else:
                # Keep a missing value unknown, rather than treating it as Developer Mode being
                # off.
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
    # Use --list for a quick USB check without pairing or selecting anything.
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--list', action='store_true', help='List USB devices without connecting')
    mode.add_argument('--doctor', action='store_true', help='Check the full developer connection without changing location')
    args = parser.parse_args(argv)
    # Hide library output and print only the explicitly selected fields below.
    logging.disable(logging.CRITICAL)
    try:
        with open(os.devnull, 'w') as quiet, redirect_stdout(quiet), redirect_stderr(quiet):
            devices = asyncio.run(usb_devices())
        if not devices:
            print('[WAIT] No USB device found. Connect the iPhone, unlock it and check Finder.')
            return 1
        # The USB list can include other Apple devices; the trust helper verifies an iPhone after selection.
        print('Connected USB devices:')
        for number, device in enumerate(devices, 1):
            print(f'  {number}. UDID: {redacted(device.serial)}')
        if args.list:
            return 0
        # Require a selection even with one phone, rather than silently choosing a target.
        if not sys.stdin.isatty():
            raise DiscoveryError('Run in Terminal to choose an iPhone.')
        # An empty response cancels. Otherwise accept only a number from the displayed list.
        choice = input('Choose a device (Enter to cancel): ').strip()
        if not choice:
            print('Cancelled.')
            return 0
        if not choice.isdecimal() or not 1 <= int(choice) <= len(devices):
            raise DiscoveryError('Invalid selection. Run again and choose a number from the list.')
        device = devices[int(choice) - 1]
        print('Connecting... Unlock the iPhone and tap Trust if asked.')
        # Keep connection diagnostics private and print the cleaned device details afterward.
        with open(os.devnull, 'w') as quiet, redirect_stdout(quiet), redirect_stderr(quiet):
            lines = asyncio.run(doctor(device) if args.doctor else inspect_phone(device))
        print('\n'.join(lines))
        return 0
    # Show a short error and exit code instead of a raw device traceback.
    except ModuleNotFoundError:
        print('[ERROR] Install dependencies first: .venv/bin/python -m pip install -r iphone/requirements.txt')
        return 2
    except (KeyboardInterrupt, EOFError):
        print('\nCancelled.')
        return 130
    except Exception as exc:
        print('[ERROR] ' + error_message(exc))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
