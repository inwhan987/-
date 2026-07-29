// Exposes a minimal, cookie-aware HTTP bridge to the renderer. The renderer
// (a Chromium browser context) can't read Set-Cookie or set Cookie headers
// itself (Fetch spec restrictions, independent of CORS) — this hands those
// two requests to the main process (plain Node, no such restrictions).
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('jetkvmIpc', {
  request: (options) => ipcRenderer.invoke('jetkvm-request', options),
  openExternal: (url) => ipcRenderer.invoke('jetkvm-open-external', url),
});
