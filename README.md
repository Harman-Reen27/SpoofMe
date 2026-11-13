# SpoofMe

I built this to learn how iPhone location services work. It lets me plug my own iPhone into my Mac over USB, choose a point on a map and ask iOS to use that simulated location.

The window is Electron, the map is Leaflet with CARTO Voyager tiles, and the phone connection is Python using pymobiledevice3. I've checked the USB Set/Clear flow in Apple Maps on my phone. I still treat Maps as the final check rather than trusting a successful desktop message alone.

This is a personal macOS project launched with `npm start`.

## Getting it running

My setup is Apple Silicon, Python 3.14 and Node 22.12 or later. The Python dependency is pinned in `iphone/requirements.txt`; npm versions are pinned in `package-lock.json`.

From the project folder:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r iphone/requirements.txt
npm ci
cp -n .env.example .env
open -e .env
```

Voyager needs a [CARTO Basemaps key](https://carto.com/basemaps/apikey/). I put it after `CARTO_API_KEY=` in `.env`, save the file, then start the app:

```bash
npm start
```

The key stays local and `.env` is ignored by Git. A `CARTO_API_KEY` environment variable overrides the file. Without a key, the app shows a map setup notice; manual coordinates and phone controls still work. Changing the key needs an app restart.

I leave `.venv` and `node_modules` out of Git too. They contain installed dependencies and can be recreated with the commands above.

## Connecting my iPhone

I use a USB **data** cable, unlock the phone and check that Finder can see it. If asked, I tap **Trust This Computer** and enter the passcode on the phone.

Location simulation also needs Developer Mode under **Settings → Privacy & Security → Developer Mode**. If the switch is missing, this reveals it without enabling it:

```bash
.venv/bin/python -m pymobiledevice3 amfi reveal-developer-mode --usbmux /var/run/usbmuxd
```

I run that with only the intended phone connected, then turn the setting on myself and follow the restart and confirmation prompts. There is no jailbreak or passcode bypass involved.

In the app, I select the USB device and press **Connect iPhone**. Discovery can list other Apple USB devices, but the connection checks reject anything that isn't the selected iPhone. Nothing automatically picks a phone or applies a saved location.

## Choosing and restoring a location

1. I click the map, drag the pin or enter latitude and longitude. This only changes the preview.
2. I press **Set Location** to send that point to the connected phone.
3. I open Apple Maps and tap its current-location arrow to check the blue dot.
4. When finished, I press **Restore Real Location**.
5. I check Maps again and press **I checked Maps — real location is back** only when it is.

The coordinate fields show six decimal places, but map selections keep their full numeric precision when sent. A separate dot and label show the last point successfully sent, so moving the preview doesn't suggest the phone has already moved.

Clear has a limitation: the developer service doesn't send an acknowledgement for its stop command. I check that USB and the developer connection still work afterward, but that can't prove the real location is back. That's why confirmation in Maps is separate.

## If the connection or power goes away

I save a small recovery record before sending Set. If USB disconnects, the phone restarts or a request fails, the app treats the location as **unknown**. Unplugging the cable isn't treated as restoration.

Closing the window or pressing Cmd+Q attempts Clear before Python exits. A forced kill, crash or flat laptop battery can prevent that cleanup. After reconnecting, I select the **same iPhone**, press Restore and check Maps again.

The recovery file is `.local/location.json`. It contains a device fingerprint and session state, with no coordinates, phone name or full UDID. An unresolved record blocks a new simulation, and its fingerprint keeps recovery tied to the original phone. Nothing replays a location on startup. I leave the record alone until the real location is confirmed in Maps.

Only one app/bridge process controls the phone at a time. An OS lock enforces that, including across separate terminals.

## How the code fits together

```text
Map and buttons
  → restricted preload API
  → Electron main process
  → Node adapter
  → JSON lines over stdin/stdout
  → Python bridge and live phone session
  → iPhone over USB
```

I keep the page separate from phone access. The renderer can't start Python, read files or choose arbitrary IPC channels. Electron validates the sender and action fields, and the Node adapter matches request IDs and handles deadlines. A timeout never automatically retries Set because it might already have reached the phone.

Python keeps one trusted USB connection open, builds a userspace RSD tunnel through CoreDeviceProxy, then opens DVT and its location simulation channel. This implementation uses the USB path available on iOS 17.4 and later. It doesn't fall back to Wi-Fi or download developer images for this DVT path.

### Why it is set up this way

Electron gives me a desktop window while letting me use Leaflet for the map. Python handles the phone because pymobiledevice3 already implements Apple's pairing and developer protocols. The Node adapter connects the two with JSON lines over a child-process pipe, so there is no local web server or port to manage.

The phone connection stays open between button presses. Reopening the tunnel for every Set would add setup time and make it harder to tell whether a command belongs to the same session. USB connection IDs and device identity checks catch a replug or a different phone; a disconnect marks the session unknown rather than quietly reconnecting and resending a point.

The recovery file is written before Set because a timeout can happen after the phone receives the command. Atomic replacement and disk flushes reduce the chance of leaving a partial record after a crash. Clear and confirmation are separate because a working connection cannot report what the Maps blue dot is showing.

`connection.py` is still part of the final app. It holds USB discovery, pairing, identity checks and safe errors shared by the bridge, location session and diagnostics. Keeping those helpers together avoids maintaining different connection behaviour for Doctor and the window. `session.py` handles the on-disk recovery record and lock; `location.py` handles the live phone session.

Doctor shares the app's setup code instead of keeping a second implementation. It opens the developer services and checks a reply from the selected phone, then closes those resources. It sends no Set or Clear command and leaves existing recovery information alone. The old terminal location controller is removed; the desktop app handles Set and Restore.

| File | What I keep there |
| --- | --- |
| `electron/main.mjs` | The window, private map key, validated IPC, polling and quit handling. |
| `electron/preload.cjs` | The small API the page can call. |
| `src/index.html` | Device selection, coordinate fields, map container and recovery controls. |
| `src/main.js` | Map preview, button actions and display state. |
| `src/style.css` | The window layout and map styling. |
| `backend/iphone.mjs` | Python startup, JSON framing, request IDs, timeouts and shutdown. |
| `iphone/bridge.py` | Allowed JSON commands and responses, with private library output suppressed. |
| `iphone/location.py` | The live USB/tunnel session, Set, Clear and disconnect monitoring. |
| `iphone/connection.py` | Shared USB discovery, trust checks and safe display/error text. |
| `iphone/session.py` | Private recovery records and the process lock. |
| `iphone/discover.py` | Standalone USB discovery and the full Doctor connection check. |

## Checking a connection with Doctor

With the desktop app closed, I run:

```bash
npm run doctor
```

I choose the phone from the numbered USB list. Doctor checks pairing, Developer Mode, the RSD tunnel, DVT, the location channel and a reply through the developer connection. A failure reports the step that could not finish. Opening a location channel does not start a simulation, and passing Doctor doesn't confirm the phone's GPS location. The same process lock prevents Doctor and the app from controlling the connection at once.

For a lighter USB and phone-details check, I run:

```bash
.venv/bin/python iphone/discover.py
```

The default check prints the phone's model, iOS version and Developer Mode status. Adding `--list` only lists USB candidates, without selecting or pairing.

## What stays local

Pairing files live in `.local/pairing/`, and Apple's USB service can also store pairing records. My local folder uses `0700` permissions; newly created credential and recovery files use `0600`. `.local/`, keys, logs and environment files are excluded from Git. I redact complete device IDs from output and remove personal device names before sharing discovery results.

Voyager tiles come from CARTO using OpenStreetMap data, with both credited on the map. Electron fetches tiles through a fixed local route, so the key doesn't appear in renderer URLs or status messages. Cache headers are preserved. CARTO still sees the key, viewed map area and network connection; it doesn't receive phone identifiers or Set/Clear commands. There is no address search or analytics.

## References

- [pymobiledevice3](https://github.com/doronz88/pymobiledevice3) and its [DVT documentation](https://doronz88.github.io/pymobiledevice3/latest/api/dvt/).
- [Electron security](https://www.electronjs.org/docs/latest/tutorial/security) and [Leaflet](https://leafletjs.com/reference-1.9.4.html).
- [CARTO Basemaps](https://www.carto.com/basemaps/) for map access, attribution and usage terms.
- [Ghost Location](https://github.com/Blueturboguy07/ghost-location), which I used as an architectural reference. No source was copied from it.

pymobiledevice3 and Ghost use GPL licensing. If I distribute a bundled app, I need to account for the relevant licensing, notices and corresponding-source requirements. Writing my own wrapper doesn't automatically remove those obligations. See the [pymobiledevice3 license](https://github.com/doronz88/pymobiledevice3/blob/master/LICENSE).
