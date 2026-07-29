# JetKVM Remote

A cross-platform, AnyDesk-style remote client for [JetKVM](https://jetkvm.com).
One codebase → **Android, iOS, and desktop (Windows / macOS / Linux)**.

Open the app, pick a saved device, and you're on the machine — video of the
remote screen plus keyboard/mouse control, over a direct WebRTC connection to
the JetKVM device.

> **Status: verified against real hardware.** Tailscale Funnel + device auth
> flow have been confirmed working end-to-end in a browser against a real
> JetKVM (see [Cross-origin cookies](#cross-origin-cookies-cors) below for the
> one runtime-specific fix this client needs). The wire format lives in one
> isolated place (`src/jetkvm/client.ts`) so any remaining `VERIFY` points are
> easy to re-check against your firmware. See
> [Verifying against a real device](#verifying-against-a-real-device).

## How it works

It speaks the same protocol JetKVM's own web UI uses:

1. `POST /auth/login-local` — logs in with the device's local password
   (skipped when password protection is disabled) and gets a session cookie.
2. `POST /webrtc/session` — sends a base64-encoded SDP **offer** and receives a
   base64-encoded **answer** (the firmware's "legacy" HTTP signaling path — no
   websocket needed, simplest for a native client).
3. A WebRTC **media track** delivers the remote screen (H.264 video).
4. A **`hidrpc`** data channel carries JSON-RPC keyboard/mouse reports:
   `keyboardReport`, `absMouseReport`, `relMouseReport`, `wheelReport`.

Because it's plain WebRTC + `fetch`, the identical code runs in a browser, in
the Capacitor WebView on phones, and in Electron's Chromium on desktop.

### Cross-origin cookies (CORS *and* the Fetch spec)

JetKVM's local API authenticates with a plain cookie (`authToken`, set by
`/auth/login-local`) and sends **no CORS headers**. But the real blocker
turned out to be bigger than CORS: the Fetch spec unconditionally (1) hides
`Set-Cookie` from JS on every response, and (2) forbids scripts from setting
a `Cookie` request header at all. Both rules apply regardless of CORS
config — enabling CapacitorHttp or disabling `webSecurity` stops the browser
from *blocking* the cross-origin request, but `fetch()` still can't read the
cookie it just got, or attach it to the next request. That's what a
`Signaling failed (HTTP 401)` after an apparently-successful login means.

The fix (`src/jetkvm/transport.ts`): don't use `fetch()` for
`/auth/login-local` and `/webrtc/session` at all. Route them through a tiny
manual cookie jar over a platform bridge that isn't bound by the Fetch spec:

- **Android/iOS**: the `CapacitorHttp` *plugin API* (`@capacitor/core`, not
  the patched `window.fetch`) — a native bridge, so no forbidden-header rules
  apply; we read `Set-Cookie` from its response and set `Cookie` on the next
  request ourselves. Enabled in `capacitor.config.ts`.
- **Desktop**: proxied over IPC (`electron/preload.cjs`) to the Electron
  **main process** (`electron/main.cjs`), which uses Node's built-in
  `fetch` — plain Node, not a browser, so neither restriction exists there
  either. The main process keeps the per-device cookie jar.
- **Plain browser** (`npm run dev`): falls back to normal `fetch()` with
  `credentials: 'include'`. This won't actually persist a cross-origin
  cookie — fine, since it's only used for local UI iteration, never a
  packaged build.

If you ever see a 401 on `/webrtc/session` after a successful login, check
that `transport.ts` is actually being used (not a stray raw `fetch()`) before
suspecting the protocol itself.

### Connection paths

Works with whatever address reaches the device:

| Setup | Host to enter |
|-------|---------------|
| Same LAN | `192.168.x.x` |
| Tailscale client (userspace-networking devices can't be reached by raw `100.x` IP — see note below) | MagicDNS name |
| **Tailscale Funnel** (verified working) | `your-device.your-tailnet.ts.net` |
| JetKVM Cloud | *(not supported — Cloud uses a different broker/auth (Google OIDC) than the local API this app talks to)* |

> Devices without kernel TUN support (e.g. JetKVM's own armv7l Linux) run
> Tailscale in `--tun=userspace-networking` mode, which does not accept
> inbound connections on its `100.x` address directly — only traffic proxied
> through Funnel (or `tailscale serve`) reaches the device. If `100.x.x.x`
> doesn't connect, use the `ts.net` Funnel hostname instead.

## Project layout

```
src/
  jetkvm/
    client.ts      ← WebRTC + signaling + HID (the whole protocol lives here)
    hid.ts         ← USB HID keycode maps + keyboard state
  storage/
    devices.ts     ← saved-device list (localStorage)
  components/
    DeviceList.tsx ← AnyDesk-style device manager
    Viewer.tsx     ← video surface + touch/mouse/keyboard input
  App.tsx
electron/main.cjs  ← desktop shell
capacitor.config.ts← mobile wrapper config
```

## Develop

```bash
npm install
npm run dev          # browser at http://localhost:5173 — fastest way to iterate
```

You can test most of the UI in a browser. A live video/HID test needs a
reachable JetKVM (or the firmware running in the JetKVM dev simulator).

## Build

### Desktop (Windows / macOS / Linux)
```bash
npm run electron:dev      # build + launch locally
npm run electron:build    # produce installers in release/
```

### Android
```bash
npm install
npx cap add android       # one-time: creates the android/ project
npm run cap:android       # build web + sync + open Android Studio
```
Requires Android Studio + JDK. Build/sign the APK/AAB from Android Studio.

### iOS (macOS only)
```bash
npm install
npx cap add ios           # one-time: creates the ios/ project
npm run cap:ios           # build web + sync + open Xcode
```
Requires Xcode + an Apple developer account to run on a device.

## Verifying against a real device

Once you have a JetKVM, confirm these in `src/jetkvm/client.ts` (each is marked
`VERIFY` in comments). They're taken from the firmware source but a firmware
revision could differ:

- **`/webrtc/session` field name** — the code sends `{ "sd": "<base64>" }` and
  reads the answer from `sd` / `answer` / `result`. Check your firmware's
  session struct if signaling fails.
- **Auth cookie** — confirmed: `login-local` sets a session cookie
  (`authToken`) that `fetch(..., { credentials: 'include' })` carries to the
  signaling call, *provided* the native-networking settings in
  [Cross-origin cookies](#cross-origin-cookies-cors) are in place. Without
  them you'll see a 401 even with a correct password.
- **HID method names / params** — `keyboardReport`, `absMouseReport`,
  `relMouseReport`, `wheelReport` with the params in `hid.ts`. If input doesn't
  register, log what the JetKVM web UI sends on its `hidrpc` channel and match.

A quick way to capture ground truth: open the JetKVM web UI in Chrome →
DevTools → Network (for `/webrtc/session`) and the WebRTC internals
(`chrome://webrtc-internals`) to see the data-channel traffic.

## Security notes

- Device passwords are stored in `localStorage` (not encrypted). For a release
  build, move them to the platform secure store — Capacitor Preferences /
  iOS Keychain / Android Keystore, and Electron `safeStorage`. `storage/devices.ts`
  is deliberately small to make that swap easy.
- The connection itself is end-to-end WebRTC (DTLS-SRTP encrypted). Over
  Tailscale Funnel the HTTP signaling is TLS to the device.
- The renderer/WebView keeps standard browser security (`webSecurity` stays
  on, `nodeIntegration` stays off, `contextIsolation` stays on). Only the two
  auth-sensitive HTTP calls are proxied out to trusted native code (Electron
  main process via a narrow `contextBridge` API, or the CapacitorHttp plugin)
  — nothing in the page itself gets elevated privileges.
