// Electron desktop shell (Windows / macOS / Linux).
// Loads the same built web app (dist/) that mobile uses. Chromium's WebRTC
// stack gives full-speed video and data channels on the desktop.
const { app, BrowserWindow, ipcMain, shell } = require('electron');
const path = require('node:path');

// Manual per-device cookie jar for the login/signaling HTTP calls. Handled
// here (plain Node, via IPC) instead of renderer fetch() because the Fetch
// spec hides Set-Cookie from JS and forbids scripts from setting a Cookie
// header at all — restrictions that exist independent of CORS, so no
// same-origin/CORS workaround in the renderer can get around them. Node's
// own fetch has neither restriction, so we do the request here and only
// hand the renderer the parsed result. See src/jetkvm/transport.ts for the
// renderer side of this bridge and the Android/iOS equivalent.
const cookieJar = new Map(); // origin -> "authToken=<value>"

ipcMain.handle('jetkvm-request', async (_event, { url, method, headers, body }) => {
  const origin = new URL(url).origin;
  const reqHeaders = { ...headers };
  const stored = cookieJar.get(origin);
  if (stored) reqHeaders['Cookie'] = stored;

  const res = await fetch(url, { method, headers: reqHeaders, body });

  const setCookies =
    typeof res.headers.getSetCookie === 'function'
      ? res.headers.getSetCookie()
      : res.headers.get('set-cookie')
        ? [res.headers.get('set-cookie')]
        : [];
  for (const sc of setCookies) {
    const match = /authToken=([^;]+)/.exec(sc);
    if (match) cookieJar.set(origin, `authToken=${match[1]}`);
  }

  return { status: res.status, body: await res.text() };
});

// Opens the device's own settings page (see src/components/Viewer.tsx,
// openDeviceSettings) in the system's real default browser rather than us
// re-implementing every settings screen — a real browser tab logs in via
// the device's own login page exactly like normal, no cookie/CORS bridging
// needed at all.
ipcMain.handle('jetkvm-open-external', (_event, url) => shell.openExternal(url));

function createWindow() {
  const win = new BrowserWindow({
    width: 1280,
    height: 800,
    backgroundColor: '#0f1115',
    title: '원격KVM',
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, 'preload.cjs'),
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
