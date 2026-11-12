# SpoofMe

A small project I'm working on to learn how iPhone location services work. I want to connect my own iPhone to my Mac over USB and eventually choose a location from a map.

For now, there's a Python script that finds USB-connected devices, lets me pick one and checks the connection. It prints the iPhone's name, model, iOS version and Developer Mode status. Location commands and the desktop app are next.

## Setup

I'm using an Apple Silicon Mac with Python 3.14 and pymobiledevice3 11.50.3.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r iphone/requirements.txt
```

`.venv` contains the installed Python packages. Most of its size comes from pymobiledevice3 and its dependencies. It's ignored by Git and can be recreated with these commands.

## Running it

Connect the iPhone with a USB data cable and unlock it. It should appear in Finder. Then run this in Terminal without sudo:

```bash
.venv/bin/python iphone/discover.py
```

Choose a device number and accept **Trust This Computer** on the phone if asked. Press Enter to cancel.

To just list connected devices:

```bash
.venv/bin/python iphone/discover.py --list
```

Example output:

```text
Connected USB devices:
  1. UDID: ********1234
Choose a device (Enter to cancel): 1
Connecting... Unlock the iPhone and tap Trust if asked.
[OK] USB connection
[OK] Paired with this Mac
[OK] iPhone detected
Name: My iPhone
Model: iPhone13,2
iOS: 27.0.1
UDID: ********1234
[OK] Developer Mode: on
```

The list can include other Apple devices, so the script checks that the selected device is an iPhone after connecting. Wi-Fi connections are ignored. `Model` is Apple's product identifier.

## Notes from getting it working

USB discovery and pairing have worked on my iPhone, and Developer Mode is now enabled. Setting and clearing a simulated location still need to be implemented and tried on the phone.

Developer Mode is under **Settings > Privacy & Security > Developer Mode**. The toggle was initially missing. This command made it appear:

```bash
.venv/bin/python -m pymobiledevice3 amfi reveal-developer-mode --usbmux /var/run/usbmuxd
```

That only reveals the switch. Turning it on still happens on the phone, including the restart and confirmation.

If discovery fails, I'll check the cable, unlock the phone and make sure Finder can see it. A trust prompt can take longer than the script's timeout; accepting it and running the script again should handle that.

## Keeping device details private

Only the last four characters of the UDID are printed. Library output is hidden because it can contain device identifiers or pairing details. I also remove personal device names before sharing output.

Pairing files are kept in `.local/pairing/`, which is ignored by Git. The folder uses `0700` permissions and new files use `0600`. Apple's USB service can also store pairing records. These files and any private keys should stay local.

## How it connects

The script uses pymobiledevice3's async `list_devices` and `create_using_usbmux` calls. It connects to the selected USB device, checks the paired session and verifies that the device ID matches. Developer Mode is read from the lockdown AMFI domain; a missing value stays unknown instead of being treated as off.

I'm keeping the iPhone connection code in Python. The Electron app will call it later.

## References and licensing

- [pymobiledevice3 docs](https://doronz88.github.io/pymobiledevice3/latest/api/connection/)
- [pymobiledevice3 source](https://github.com/doronz88/pymobiledevice3)
- [Ghost Location](https://github.com/Blueturboguy07/ghost-location), a reference for the later location work. No source has been copied from it.

pymobiledevice3 and Ghost use GPL licensing. Private use is allowed, but distributing a combined app can require GPL-compatible licensing, corresponding source and license notices. I'll need to review those requirements before sharing a packaged app. See the [pymobiledevice3 license](https://github.com/doronz88/pymobiledevice3/blob/master/LICENSE).
