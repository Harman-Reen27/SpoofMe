# SpoofMe

A personal project to learn how iPhone location services work. I'm using Python and pymobiledevice3 to connect to my own iPhone over USB and simulate a location. Eventually I'd like to put a small macOS interface around it.

USB discovery, Set and Clear are working on my phone and have been checked in Apple Maps. The interface is still to come.

## Setup

I'm developing on an Apple Silicon Mac with Python 3.14. The scripts use pymobiledevice3 11.50.3 and the USB developer connection available on iOS 17.4 and later.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r iphone/requirements.txt
```

`.venv` holds the installed libraries and is ignored by Git. Most of its size comes from pymobiledevice3's dependencies. Node and Electron aren't needed yet.

## Connecting the phone

Connect the iPhone with a USB data cable, unlock it and make sure it appears in Finder. Run commands from Terminal without sudo, and accept **Trust This Computer** on the phone if asked.

```bash
.venv/bin/python iphone/discover.py
```

Choose a device number, or press Enter to cancel. Add `--list` to list devices without connecting. Only USB connections are listed; other Apple USB devices are rejected after selection if they aren't iPhones.

Location commands also need Developer Mode under **Settings > Privacy & Security > Developer Mode**. If the switch is missing, this command reveals it:

```bash
.venv/bin/python -m pymobiledevice3 amfi reveal-developer-mode --usbmux /var/run/usbmuxd
```

Then enable it on the phone and follow the restart and confirmation prompts.

## Location commands

Check the connection and developer services without changing location:

```bash
.venv/bin/python iphone/location_cli.py doctor
```

Doctor shows each connection step. Set and Clear keep the output shorter. If the connection fails, check the cable, unlock the phone and check Finder first.

Set a location, for example central Melbourne:

```bash
.venv/bin/python iphone/location_cli.py set -37.8136 144.9631
```

Choose the phone, then open Apple Maps and tap the current-location arrow to check the blue dot. Keep the terminal open while using the simulated location. **Ctrl+C** attempts to clear it before closing the connection.

To clear and confirm the real location:

```bash
.venv/bin/python iphone/location_cli.py clear
```

Check the blue dot in Maps and type `yes` only when it's back at the real location. Enter leaves it unconfirmed. Clear doesn't receive a device acknowledgement, so a successful send alone isn't proof that location reporting has returned to normal.

## Interrupted sessions

The scripts keep a private recovery record in `.local/location.json`. It contains a device fingerprint and session state, without coordinates or a full UDID. An unresolved session blocks another Set, and recovery stays tied to the original phone. Nothing automatically reapplies a location.

If the cable disconnects, the phone restarts or a command fails, reconnect the same phone, run Clear and check Maps. Unplugging the cable isn't treated as restoration. A forced kill or crash may prevent the normal Clear attempt from running.

Only one location command runs at a time. Stop Set before running Clear in another terminal. The recovery record is removed after the Maps confirmation, not just when Clear is sent.

Exit code `0` means the command finished or selection was cancelled. Code `3` means the location still needs confirmation, including after Set exits and sends Clear. Errors return `1`, missing dependencies or invalid arguments return `2`, and interruptions can return `130`.

## Code layout

- `iphone/discover.py` — list devices and show phone details.
- `iphone/connection.py` — shared USB, pairing and output helpers.
- `iphone/location_cli.py` — Doctor, Set, Clear and connection cleanup.
- `iphone/session.py` — recovery file and lock for concurrent commands.
- `iphone/requirements.txt` — the Python dependency version.

The location connection goes through USB lockdown → CoreDeviceProxy → a userspace RSD tunnel → DVT → LocationSimulation. It reuses the selected USB connection and doesn't fall back to Wi-Fi. This DVT path doesn't need a developer image mount.

Pairing records live in `.local/pairing/` and may also be stored by Apple's USB service. The local folder uses `0700` permissions and new credential and recovery files use `0600`. `.local/`, private keys and logs are excluded from Git. Full device IDs and library output are hidden; personal device names should still be removed before sharing discovery output.

## References and licensing

- [pymobiledevice3](https://github.com/doronz88/pymobiledevice3) and its [connection docs](https://doronz88.github.io/pymobiledevice3/latest/api/connection/).
- [DVT documentation](https://doronz88.github.io/pymobiledevice3/latest/api/dvt/).
- [Ghost Location](https://github.com/Blueturboguy07/ghost-location), an architectural reference. No source has been copied from it.

pymobiledevice3 and Ghost use GPL licensing. Private use is allowed, but distributing a combined app can require compatible licensing, corresponding source and license notices. I'll review those requirements before distributing a packaged app. See the [pymobiledevice3 license](https://github.com/doronz88/pymobiledevice3/blob/master/LICENSE).
