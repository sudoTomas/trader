// The page's only door to the outside: trader's API, read-only broker views, one event.
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('trader', {
  get: (route) => ipcRenderer.invoke('api', 'GET', route),
  post: (route) => ipcRenderer.invoke('api', 'POST', route),
  broker: (view) => ipcRenderer.invoke('broker', view),
  backendStatus: () => ipcRenderer.invoke('backend-status'),
  onBackendStatus: (fn) => ipcRenderer.on('backend-status', (_e, s) => fn(s)),
});
