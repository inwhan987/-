// Electron desktop shell (Windows / macOS / Linux).
// Loads the same built web app (dist/) that mobile uses. Chromium's WebRTC
// stack gives full-speed video and data channels on the desktop.
const { app, BrowserWindow, ipcMain, shell, session, dialog } = require('electron');
const path = require('node:path');
const fs = require('node:fs');
const http = require('node:http');
const https = require('node:https');

// Only one copy should ever run: a second launch would also try to bind
// PROXY_PORT below and crash with EADDRINUSE, on top of just being
// confusing to have two windows. Bail out immediately, before anything
// else in this file runs, if another instance already holds the lock.
if (!app.requestSingleInstanceLock()) {
  app.quit();
  return;
}
app.on('second-instance', () => {
  const win = BrowserWindow.getAllWindows()[0];
  if (win) {
    if (win.isMinimized()) win.restore();
    win.focus();
  }
  void dialog.showMessageBox({
    type: 'info',
    title: '원격KVM',
    message: '원격KVM이 이미 실행 중입니다.',
  });
});

const PROXY_PORT = 47623;

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
    if (match) {
      cookieJar.set(origin, `authToken=${match[1]}`);
      // Also hand this SAME token to the renderer's real cookie store for
      // the settings iframe's origin (127.0.0.1:PROXY_PORT), so it appears
      // already logged in. Deliberately NOT a fresh /auth/login-local call
      // from the renderer: JetKVM's local auth keeps a single global token
      // and overwrites it on every successful login, invalidating whatever
      // session (including this WebRTC connection) was using the old one —
      // confirmed by logging in from a separate browser tab disconnecting
      // the app. Reusing the existing token avoids minting a new one, so
      // nothing gets invalidated.
      void session.defaultSession.cookies.set({
        url: `http://127.0.0.1:${PROXY_PORT}/`,
        name: 'authToken',
        value: match[1],
        httpOnly: true,
        path: '/',
      });
    }
  }

  return { status: res.status, body: await res.text() };
});

// Opens the device's own settings page in the system's real default browser
// — used as the mobile/fallback path. Kept alongside the local reverse
// proxy below (the primary desktop path) since it's the one option that
// works on every platform with zero extra infrastructure.
ipcMain.handle('jetkvm-open-external', (_event, url) => shell.openExternal(url));

// Windows' own on-screen touch keyboard (TabTip.exe) only pops up
// automatically on its own heuristics (touch input detected, tablet mode,
// no physical keyboard, etc.) which Electron apps often don't trigger even
// on a touchscreen PC. Launching it directly on demand -- the same trick
// many kiosk/POS web apps use -- is the reliable way to get it up when the
// 입력… button is tapped. No-op (and harmless) on macOS/Linux.
ipcMain.handle('jetkvm-show-touch-keyboard', () => {
  if (process.platform !== 'win32') return;
  const common = process.env['CommonProgramFiles'] || 'C:\\Program Files\\Common Files';
  const tabTipPath = path.join(common, 'microsoft shared', 'ink', 'TabTip.exe');
  require('node:child_process').execFile(tabTipPath, (err) => {
    if (err) console.error('failed to launch TabTip.exe', err);
  });
});

// ---------------------------------------------------------------------------
// Local reverse proxy so the settings iframe is same-origin as our own app.
//
// The device's session cookie (authToken) has no SameSite attribute, which
// browsers default to Lax — that blocks it from being sent in a genuinely
// cross-origin iframe (confirmed: the iframe pointed straight at the device
// showed nothing). The fix is the same one browsers themselves use for
// embedding third-party content that needs first-party cookies: make it
// NOT cross-origin. This tiny local HTTP server serves our own app AND
// transparently forwards everything else (the device's /settings page, its
// own login page, and all the API calls its JS makes) to the real device.
// From the browser's point of view our app and the proxied device page are
// both http://127.0.0.1:<PORT>, so the cookie set during the proxied login
// is completely ordinary first-party same-site behavior — no special
// cookie handling needed here at all, unlike the WebRTC login flow above.
// ---------------------------------------------------------------------------
const distDir = path.join(__dirname, '..', 'dist');
let proxyTarget = null; // e.g. "https://remote-desktop.taileb686e.ts.net"
// The device's public LAN IP, substituted into its own trickled (private)
// ICE candidates so the settings page's WebRTC connection can actually
// reach it from outside the LAN -- same fix as src/jetkvm/client.ts's
// withPublicIpCandidate(), applied via iceInjectionScript()/injectIceScript
// below since this is a RTCPeerConnection we don't create ourselves. Set
// per-device (see jetkvm-set-proxy-target), not hardcoded -- requires the
// router to DMZ/port-forward that address to the device; unset or a stale
// value here just means one candidate never pairs, not a regression from
// not having it at all.
let proxyPublicIp = null;

const MIME_TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'application/javascript',
  '.css': 'text/css',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.json': 'application/json',
};

function serveStatic(req, res) {
  const reqPath = req.url === '/' ? '/index.html' : req.url;
  const filePath = path.join(distDir, decodeURIComponent(reqPath.split('?')[0]));
  if (!filePath.startsWith(distDir)) {
    res.writeHead(403).end('Forbidden');
    return;
  }
  fs.readFile(filePath, (err, data) => {
    if (err) {
      res.writeHead(404).end('Not found');
      return;
    }
    const ext = path.extname(filePath);
    res.writeHead(200, { 'Content-Type': MIME_TYPES[ext] ?? 'application/octet-stream' });
    res.end(data);
  });
}

// The device may send framing-blocking headers (X-Frame-Options,
// frame-ancestors in CSP) on its settings/access pages — enforced by the
// browser regardless of same-origin, so making the iframe same-origin (the
// whole point of this proxy) doesn't get around it. This is our own
// trusted local relay serving only our own iframe, so stripping them here
// is safe and is the only way framing works at all.
// The device's own settings page negotiates its OWN separate WebRTC
// connection (its own RTCPeerConnection, for its own live status/settings
// data, apparently including the actual get/set RPC calls -- not just
// video) -- completely outside our client.ts, so nothing we do there can
// help it directly. But since that page's HTML/JS passes through this same
// proxy, we can still reach it: inject a tiny script ahead of the page's
// own bundle that wraps window.RTCPeerConnection.
//
// This used to merge in a list of free public TURN servers (Metered's
// OpenRelay). That's dead weight now: a live tcpdump on our own TURN server
// (see src/jetkvm/client.ts's DEFAULT_ICE comment) proved no relay can ever
// reach the device, because it only ever advertises its private LAN
// address and a relay server has no route to forward packets there. What
// actually works -- confirmed on the main video connection -- is
// synthesizing a second candidate with the router's public IP substituted
// for the private one (the router DMZs/port-forwards that address to the
// device), which needs no external service at all. This applies the exact
// same substitution here, via addIceCandidate, since that's the only hook
// available on a RTCPeerConnection we didn't create ourselves.
// publicIp is per-device now (set alongside proxyTarget, see
// jetkvm-set-proxy-target below) rather than the one hardcoded address this
// was originally written against -- built fresh per request instead of
// once at startup so it always reflects whichever device is currently
// selected. Returns null (skip injection) if no public IP is set for the
// current device; that just means one fewer candidate to try, not broken.
function iceInjectionScript(publicIp) {
  if (!publicIp) return null;
  return `<script>(function(){
var PUB=${JSON.stringify(publicIp)};
var Native=window.RTCPeerConnection;
if(!Native)return;
function isPriv(a){return /^(10\\.|172\\.(1[6-9]|2\\d|3[01])\\.|192\\.168\\.)/.test(a);}
function withPub(c){
  if(!c||!c.candidate)return[c];
  var p=c.candidate.split(' ');
  var addr=p[4];
  if(!addr||!isPriv(addr))return[c];
  var pp=p.slice();
  pp[4]=PUB;
  pp[0]=p[0]+'pub';
  var c2={};
  for(var k in c)c2[k]=c[k];
  c2.candidate=pp.join(' ');
  return[c,c2];
}
function Patched(config,constraints){
  var pc=new Native(config,constraints);
  var origAdd=pc.addIceCandidate.bind(pc);
  pc.addIceCandidate=function(candidate){
    var arr=withPub(candidate);
    var p;
    for(var i=0;i<arr.length;i++){p=origAdd(arr[i]);}
    return p;
  };
  return pc;
}
Patched.prototype=Native.prototype;
window.RTCPeerConnection=Patched;
})();</script>`;
}

function injectIceScript(html) {
  const script = iceInjectionScript(proxyPublicIp);
  if (!script) return html;
  const headMatch = /<head[^>]*>/i.exec(html);
  if (!headMatch) return html;
  const idx = headMatch.index + headMatch[0].length;
  return html.slice(0, idx) + script + html.slice(idx);
}

function forwardResponseHeaders(res, proxyRes) {
  const headers = { ...proxyRes.headers };
  delete headers['x-frame-options'];
  if (headers['content-security-policy']) {
    headers['content-security-policy'] = headers['content-security-policy'].replace(
      /frame-ancestors[^;]*;?\s*/i,
      '',
    );
  }

  const contentType = String(headers['content-type'] || '');
  if (!contentType.includes('text/html')) {
    res.writeHead(proxyRes.statusCode, headers);
    proxyRes.pipe(res);
    return;
  }

  // HTML responses get buffered (they're small -- an SPA shell) instead of
  // streamed, so the injected script can be spliced in before forwarding.
  const chunks = [];
  proxyRes.on('data', (c) => chunks.push(c));
  proxyRes.on('end', () => {
    const html = injectIceScript(Buffer.concat(chunks).toString('utf8'));
    const body = Buffer.from(html, 'utf8');
    headers['content-length'] = body.length;
    res.writeHead(proxyRes.statusCode, headers);
    res.end(body);
  });
}

// We tried blocking /webrtc/* outright (thinking it only carried the
// settings page's video preview) and separately just CSS-hiding the
// <video>/<canvas> elements — but the real UI reuses that SAME connection's
// data channel for its settings get/set JSON-RPC calls too (exactly like
// our own client.ts), and CSS-hiding still lets the video track actually
// negotiate and stream, wasting the device's one hardware encoder on
// something invisible. The real fix: edit the SDP offer as it passes
// through, rejecting the video AND audio media sections (RFC 3264 §6: set
// each m-line's port to 0) before forwarding it — neither track ever gets
// negotiated, while the data channels (which live in a separate
// m=application section) are untouched and negotiate completely normally.
function rejectVideoInOffer(bodyText) {
  try {
    const payload = JSON.parse(bodyText);
    if (typeof payload.sd !== 'string') return null;
    const desc = JSON.parse(Buffer.from(payload.sd, 'base64').toString('utf8'));
    if (typeof desc.sdp !== 'string') return null;
    const rewrittenSdp = desc.sdp
      .replace(/^m=video \d+/m, 'm=video 0')
      .replace(/^m=audio \d+/m, 'm=audio 0');
    if (rewrittenSdp === desc.sdp) return null; // no video/audio m-line present
    const newSd = Buffer.from(
      JSON.stringify({ ...desc, sdp: rewrittenSdp }),
      'utf8',
    ).toString('base64');
    return Buffer.from(JSON.stringify({ ...payload, sd: newSd }), 'utf8');
  } catch {
    return null; // not the shape we expect — forward untouched
  }
}

function proxyToDevice(req, res) {
  if (!proxyTarget) {
    res.writeHead(502).end('No device set for this session yet.');
    return;
  }
  const target = new URL(req.url, proxyTarget);
  const mod = target.protocol === 'https:' ? https : http;

  const isWebrtcOffer = req.method === 'POST' && target.pathname === '/webrtc/session';
  if (!isWebrtcOffer) {
    // No compressed responses -- forwardResponseHeaders buffers and string-
    // rewrites HTML bodies to inject the ICE-servers script below, which
    // would corrupt/garble a gzip'd body it doesn't know to decompress
    // first. Stripping accept-encoding is the simplest way to guarantee
    // the device just sends identity/uncompressed instead.
    const outHeaders = { ...req.headers, host: target.host };
    delete outHeaders['accept-encoding'];
    const proxyReq = mod.request(
      target,
      { method: req.method, headers: outHeaders },
      (proxyRes) => forwardResponseHeaders(res, proxyRes),
    );
    req.pipe(proxyReq);
    proxyReq.on('error', (err) => res.writeHead(502).end(String(err)));
    return;
  }

  // Buffer the (small — an SDP offer) body instead of streaming it, so we
  // can rewrite it before forwarding.
  const chunks = [];
  req.on('data', (c) => chunks.push(c));
  req.on('end', () => {
    const original = Buffer.concat(chunks);
    const rewritten = rejectVideoInOffer(original.toString('utf8')) ?? original;
    const proxyReq = mod.request(
      target,
      {
        method: 'POST',
        headers: { ...req.headers, host: target.host, 'content-length': rewritten.length },
      },
      (proxyRes) => forwardResponseHeaders(res, proxyRes),
    );
    proxyReq.on('error', (err) => res.writeHead(502).end(String(err)));
    proxyReq.end(rewritten);
  });
}

const proxyServer = http.createServer((req, res) => {
  const isOwnAsset =
    req.url === '/' || req.url === '/index.html' || req.url.startsWith('/assets/');
  if (isOwnAsset) serveStatic(req, res);
  else proxyToDevice(req, res);
});

// Best-effort WebSocket passthrough, in case the device's settings page (or
// its own JS) opens one for live data — raw socket piping, no framing logic
// needed since we're just relaying bytes between two already-negotiated ends.
proxyServer.on('upgrade', (req, clientSocket, head) => {
  if (!proxyTarget) {
    clientSocket.destroy();
    return;
  }
  const target = new URL(req.url, proxyTarget);
  const mod = target.protocol === 'https:' ? https : http;
  const proxyReq = mod.request({
    hostname: target.hostname,
    port: target.port || (target.protocol === 'https:' ? 443 : 80),
    path: target.pathname + target.search,
    method: req.method,
    headers: { ...req.headers, host: target.host },
  });
  proxyReq.on('upgrade', (proxyRes, proxySocket, proxyHead) => {
    clientSocket.write(
      `HTTP/1.1 101 Switching Protocols\r\n` +
        Object.entries(proxyRes.headers)
          .map(([k, v]) => `${k}: ${v}`)
          .join('\r\n') +
        '\r\n\r\n',
    );
    proxySocket.write(proxyHead);
    proxySocket.pipe(clientSocket);
    clientSocket.pipe(proxySocket);
  });
  proxyReq.on('error', () => clientSocket.destroy());
  proxyReq.end(head);
});

proxyServer.listen(PROXY_PORT, '127.0.0.1');

ipcMain.handle('jetkvm-set-proxy-target', (_event, base, publicIp) => {
  proxyTarget = base;
  proxyPublicIp = publicIp || null;
});

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
  win.loadURL(`http://127.0.0.1:${PROXY_PORT}/`);
}

// ---------------------------------------------------------------------------
// Auto-update. CI publishes every build to the "latest" GitHub Release tag
// (see .github/workflows/jetkvm-remote-build.yml) marked prerelease:true --
// electron-updater's GitHub provider ignores prereleases unless told
// otherwise, so allowPrerelease is required for it to ever find our
// releases at all.
// ---------------------------------------------------------------------------
const { autoUpdater } = require('electron-updater');
autoUpdater.allowPrerelease = true;

autoUpdater.on('update-downloaded', (info) => {
  void dialog
    .showMessageBox({
      type: 'info',
      title: '원격KVM 업데이트',
      message: `새 버전(${info.version})이 있습니다. 지금 재시작해서 설치할까요?`,
      buttons: ['지금 재시작', '나중에'],
    })
    .then((result) => {
      if (result.response === 0) autoUpdater.quitAndInstall();
    });
});
autoUpdater.on('error', (err) => {
  console.error('auto-update error', err);
});

app.whenReady().then(() => {
  createWindow();
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
  autoUpdater.checkForUpdates().catch((err) => console.error('update check failed', err));
  setInterval(
    () => autoUpdater.checkForUpdates().catch((err) => console.error('update check failed', err)),
    60 * 60 * 1000,
  );
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});
