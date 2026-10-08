// The page's only door to the outside: trader's API, read-only broker views, one event.
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('trader', {
  get: (route) => ipcRenderer.invoke('api', 'GET', route),
  post: (route, body) => ipcRenderer.invoke('api', 'POST', route, body),
  put: (route, body) => ipcRenderer.invoke('api', 'PUT', route, body),
  del: (route) => ipcRenderer.invoke('api', 'DELETE', route),
  broker: (view) => ipcRenderer.invoke('broker', view),
  backendStatus: () => ipcRenderer.invoke('backend-status'),
  onBackendStatus: (fn) => ipcRenderer.on('backend-status', (_e, s) => fn(s)),
});
