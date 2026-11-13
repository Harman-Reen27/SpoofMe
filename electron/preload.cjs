// Use the preload as a small boundary between the page and Electron. These wrappers expose phone
// actions and state updates, not raw ipcRenderer, Node modules or the Electron event objects
// received by listeners.

const { contextBridge, ipcRenderer } = require('electron');

// Expose specific operations instead of handing the page a generic IPC or filesystem API.
async function invoke(action, fields = {}) {
  const response = await ipcRenderer.invoke('phone:command', { action, ...fields });
  // IPC sends plain envelopes because Error objects lose useful details across process boundaries.
  if (!response.ok) throw new Error(response.error);
  return response.result;
}

// The page gets named methods with fixed payloads, not arbitrary IPC channels.
contextBridge.exposeInMainWorld('phone', {
  snapshot: () => invoke('snapshot'),
  refresh: () => invoke('refresh'),
  prepare: (device) => invoke('prepare', { device }),
  setLocation: (latitude, longitude) => invoke('set', { latitude, longitude }),
  clearLocation: () => invoke('clear'),
  confirmRestored: () => invoke('confirm', { confirmed: true }),

  // Forward only display data, stripping the Electron event object before calling the page.
  onState: (callback) => {
    const listener = (_event, state) => callback(state);
    ipcRenderer.on('phone:state', listener);
    // Return an unsubscribe function so reloading the page does not leave old listeners behind.
    return () => ipcRenderer.removeListener('phone:state', listener);
  },
});
