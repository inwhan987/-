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
    title: 'JetKVM Remote',
    webPreferences: {
      // Renderer only loads local bundle + talks WebRTC to the device.
      contextIsolation: true,
      nodeIntegration: false,
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
