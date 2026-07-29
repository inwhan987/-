// Electron desktop shell (Windows / macOS / Linux).
// Loads the same built web app (dist/) that mobile uses. Chromium's WebRTC
// stack gives full-speed video and data channels on the desktop.
const { app, BrowserWindow } = require('electron');
const path = require('node:path');

function createWindow() {
  const win = new BrowserWindow({
    width: 1280,
    height: 800,
    backgroundColor: '#0f1115',
    title: '원격KVM',
    webPreferences: {
      // Renderer only loads local bundle + talks WebRTC to the device.
      contextIsolation: true,
      nodeIntegration: false,
      // JetKVM's local API sets no CORS headers, so Chromium's same-origin
      // policy blocks our renderer (a different origin than the device) from
      // using the authToken cookie /auth/login-local sets. This app only
      // ever loads our own bundled UI (never third-party remote pages), and
      // its whole purpose is cross-origin requests to a user-specified
      // device, so disabling the renderer's CORS enforcement here is the
      // desktop-equivalent of CapacitorHttp on mobile, not a general
      // weakening of an app that browses the open web.
      webSecurity: false,
    },
  });

  win.removeMenu();
  win.loadFile(path.join(__dirname, '..', 'dist', 'index.html'));
}

app.whenReady().then(() => {
  createWindow();
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});
