// Keep the page limited to the preload API. It never accesses Node, files or Python directly.
// Cache the controls once; state updates only change their text, values and enabled state.
const ui = Object.fromEntries([
  'refresh', 'devices', 'connect', 'device-details', 'device-name', 'device-version',
  'latitude', 'longitude', 'coordinate-hint', 'set', 'restore', 'confirm',
  'connection-badge', 'connection-label', 'location-state', 'last-sent',
  'recovery', 'message', 'map-notice',
].map((id) => [id, document.getElementById(id)]));
let snapshot;
let pending = false;
// Melbourne is just the initial map view; these coordinates are never sent automatically.
let preview = { latitude: -37.8136, longitude: 144.9631 };
let shownLastSent = '';
let lastSentMarker;
let initialized = false;
let previousLocationState;
let tileFailed = false;

// Use local Leaflet code for Voyager and let Electron fetch tiles with the private key.
const map = L.map('map', { zoomControl: false, maxBoundsViscosity: 1,
  minZoom: 1, maxZoom: 20,
  // Web Mercator cannot display the poles, although manually entered latitudes may reach ±90.
  maxBounds: [[-85.051129, -180], [85.051129, 180]] }).setView([-37.8136, 144.9631], 14);
L.control.zoom({ position: 'bottomright' }).addTo(map);
L.control.scale({ position: 'bottomleft', imperial: false }).addTo(map);
// {r} requests sharper tiles on Retina displays; the main process adds the private API key.
const tiles = L.tileLayer('spoofme://app/tiles/{z}/{x}/{y}{r}.png', {
  maxNativeZoom: 20, maxZoom: 20, noWrap: true,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap contributors</a>, ' +
    '&copy; <a href="https://carto.com/attribution/" target="_blank" rel="noopener">CARTO</a>',
});
tiles.on('tileerror', () => { tileFailed = true; renderMapNotice(); });
tiles.on('tileload', () => { tileFailed = false; renderMapNotice(); });

function renderMapNotice() {
  // Keep missing credentials separate from a failed download, without blocking coordinates.
  const configured = snapshot?.mapConfigured;
  ui['map-notice'].hidden = configured !== false && !tileFailed;
  ui['map-notice'].textContent = configured === false
    ? 'Voyager needs a CARTO key. Add your key to the local configuration and restart. Manual coordinates still work.'
    : 'Voyager tiles could not load. Check your CARTO key and internet connection. Manual coordinates still work.';
}

// Treat this draggable pin as a draft point, not a reading from the phone's GPS.
const marker = L.marker([preview.latitude, preview.longitude], {
  draggable: true,
  icon: L.divIcon({ className: 'preview-pin', html: '<span></span>', iconSize: [30, 38], iconAnchor: [15, 36] }),
}).addTo(map);

function validPoint(latitude, longitude) {
  // Check input here for quick feedback; Python validates the actual device request too.
  return Number.isFinite(latitude) && Math.abs(latitude) <= 90 &&
    Number.isFinite(longitude) && Math.abs(longitude) <= 180;
}

function updatePoint(latitude, longitude, updateFields = true) {
  // Keep the full coordinate values internally; six decimal places are just for display.
  preview = { latitude, longitude };
  marker.setLatLng([latitude, longitude]);
  if (updateFields) {
    ui.latitude.value = latitude.toFixed(6);
    ui.longitude.value = longitude.toFixed(6);
  }
  ui['coordinate-hint'].textContent = Math.abs(latitude) > 85.051129
    ? 'This point is beyond the map display range. The entered coordinates are preserved.'
    : 'The pin is a preview, not a reading from your phone.';
  ui['coordinate-hint'].classList.remove('invalid');
  render();
}

// Only update the preview on clicks and drags. Set is the only action that sends coordinates.
map.on('click', (event) => {
  const point = event.latlng.wrap();
  updatePoint(point.lat, point.lng);
});
marker.on('dragend', () => {
  const point = marker.getLatLng().wrap();
  updatePoint(point.lat, point.lng);
});
for (const field of [ui.latitude, ui.longitude]) field.addEventListener('input', () => {
  // Treat an empty field as invalid: Number('') would otherwise silently become zero.
  const latitude = ui.latitude.value.trim() ? Number(ui.latitude.value) : NaN;
  const longitude = ui.longitude.value.trim() ? Number(ui.longitude.value) : NaN;
  if (!validPoint(latitude, longitude)) {
    preview = null;
    ui['coordinate-hint'].textContent = 'Latitude: −90 to 90. Longitude: −180 to 180.';
    ui['coordinate-hint'].classList.add('invalid');
    render();
    return;
  }
  updatePoint(latitude, longitude, false);
  map.panTo([latitude, longitude]);
});

function render() {
  if (!snapshot) return;
  // Leave the tile layer unloaded until a key is configured, avoiding unauthenticated requests.
  if (snapshot.mapConfigured && !map.hasLayer(tiles)) tiles.addTo(map);
  renderMapNotice();
  const status = snapshot.status;
  const busy = pending || snapshot.busy || snapshot.closing;
  const ready = status.prepared;
  const confirmedPending = status.locationState === 'clear-sent';

  // Keep a selection only while its handle is still listed; a new device always needs an explicit selection.
  const previous = ui.devices.value;
  const list = snapshot.devices;
  // Rebuild options only when discovery changes, preserving focus during regular status polls.
  const signature = JSON.stringify(list);
  if (ui.devices.dataset.signature !== signature) {
    ui.devices.replaceChildren(new Option(list.length ? 'Choose a device' : 'No USB devices found', ''));
    for (const device of list) ui.devices.add(new Option(`${device.name} · ${device.identifier}`, device.id));
    ui.devices.dataset.signature = signature;
    if (list.some((device) => device.id === previous)) ui.devices.value = previous;
  }

  // Disable phone actions while busy or when the selected device is not the prepared one.
  const selectedReady = ready && ui.devices.value === snapshot.deviceHandle;
  ui.refresh.disabled = busy;
  ui.devices.disabled = busy;
  ui.connect.disabled = busy || !ui.devices.value;
  ui.set.disabled = busy || !selectedReady || !preview ||
    (status.recoveryRequired && status.locationState !== 'simulated');
  ui.restore.disabled = busy || !selectedReady;
  ui.confirm.hidden = !confirmedPending;
  ui.confirm.disabled = busy || !selectedReady;
  ui.connect.textContent = busy ? 'Working…' : 'Connect iPhone';
  ui['connection-label'].textContent = snapshot.closing ? 'Closing safely…' : busy ? 'Working…' : ready ? 'iPhone connected' : 'Not connected';
  ui['connection-badge'].classList.toggle('connected', ready && !snapshot.closing);

  // Insert phone names as plain text, so device-provided strings cannot become HTML.
  ui['device-details'].hidden = !status.device;
  ui['device-name'].textContent = status.device?.name ?? '';
  ui['device-version'].textContent = status.device ? `${status.device.model} · iOS ${status.device.ios}` : '';
  const labels = { idle: 'No location sent', simulated: 'Simulated location sent',
    'clear-sent': 'Clear sent · check Apple Maps', 'real-confirmed': 'Real location confirmed', unknown: 'Location state unknown' };
  ui['location-state'].textContent = labels[status.locationState] ?? 'Location state unknown';

  // Draw the last successful Set separately, so moving the preview does not suggest the phone
  // moved.
  const last = snapshot.lastSent;
  ui['last-sent'].textContent = last
    ? `Last sent: ${last.latitude.toFixed(6)}, ${last.longitude.toFixed(6)}`
    : 'Your selected point stays on this Mac until you press Set.';
  const lastSignature = JSON.stringify(last);
  if (lastSignature !== shownLastSent) {
    if (lastSentMarker) map.removeLayer(lastSentMarker);
    lastSentMarker = last ? L.circleMarker([last.latitude, last.longitude], {
      radius: 6, color: '#173d36', fillColor: '#ffffff', fillOpacity: 1, weight: 2,
    }).addTo(map).bindTooltip('Last point sent to the iPhone') : null;
    shownLastSent = lastSignature;
  }
  // A healthy simulation has a recovery record too; only unresolved sessions need this prompt.
  ui.recovery.hidden = !status.recoveryRequired || status.locationState === 'simulated';
  ui.recovery.textContent = confirmedPending
    ? 'Clear was sent. Check the current-location dot in Apple Maps before confirming below.'
    : 'An earlier session needs checking. Connect the original iPhone and press Restore Real Location. No location has been reapplied.';
  if (snapshot.error) showMessage(snapshot.error, true);
  else if (snapshot.closing) showMessage('Attempting cleanup before closing. Unconfirmed recovery information will be kept.');
  else if (!busy && !initialized) {
    initialized = true;
    showMessage(ready ? 'Connected. Choose a preview point.' : 'Choose a USB device and connect.');
  }
  // Bring a new recovery prompt into view on small windows, then return to the top after
  // confirmation.
  if (status.locationState !== previousLocationState) {
    if (confirmedPending) ui.confirm.scrollIntoView({ block: 'nearest' });
    else if (status.locationState === 'unknown' && status.recoveryRequired) ui.recovery.scrollIntoView({ block: 'nearest' });
    else if (status.locationState === 'real-confirmed') document.querySelector('.sidebar').scrollTo({ top: 0 });
    previousLocationState = status.locationState;
  }
}

function showMessage(message, error = false) {
  // Replace the footer text rather than accumulating a console-style log in the UI.
  ui.message.textContent = message;
  ui.message.classList.toggle('error', error);
}

async function action(operation, message, success) {
  // Capture one operation and wait for its result before accepting another button press.
  if (pending) return;
  pending = true;
  showMessage(message);
  render();
  try {
    snapshot = await operation();
    showMessage(success);
  } catch (error) {
    showMessage(error.message, true);
  } finally {
    pending = false;
    render();
  }
}

// Wire each button to exactly one operation. The map handlers above never send Set.
ui.devices.addEventListener('change', render);
ui.refresh.addEventListener('click', () => action(() => window.phone.refresh(),
  'Looking for USB devices…', 'Choose a device and connect.'));
ui.connect.addEventListener('click', () => action(() => window.phone.prepare(ui.devices.value),
  'Preparing the iPhone. Keep it unlocked and accept Trust if asked…', 'Connected. Choose a point, then press Set.'));
ui.set.addEventListener('click', () => {
  if (!preview) return;
  // Capture this click's coordinates even if the draft pin moves while Python is responding.
  const point = { ...preview };
  action(() => window.phone.setLocation(point.latitude, point.longitude),
    'Sending the selected point…', 'Location sent. Check the blue dot in Apple Maps.');
});
ui.restore.addEventListener('click', () => action(() => window.phone.clearLocation(),
  'Sending Clear…', 'Clear sent. Check Apple Maps, then confirm the real location below.'));
ui.confirm.addEventListener('click', () => action(() => window.phone.confirmRestored(),
  'Checking the connection and recording your confirmation…', 'Real location confirmed. Recovery record cleared.'));

// Subscribe before reading initial state so a startup update cannot be missed.
const unsubscribe = window.phone.onState((state) => { snapshot = state; render(); });
window.addEventListener('unload', unsubscribe);
window.phone.snapshot().then((state) => {
  snapshot = state;
  render();
  if (!state.busy) showMessage(state.status.prepared ? 'Connected. Choose a preview point.' : 'Choose a USB device and connect.');
}).catch((error) => showMessage(error.message, true));
