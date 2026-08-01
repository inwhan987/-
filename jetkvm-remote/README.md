# JetKVM Remote

A cross-platform, AnyDesk-style remote client for [JetKVM](https://jetkvm.com).
One codebase → **Android, iOS, and desktop (Windows / macOS / Linux)**.

Open the app, pick a saved device, and you're on the machine — video of the
remote screen plus keyboard/mouse control, over a direct WebRTC connection to
the JetKVM device.

> **Status: verified against real hardware**, including remote access from
> mobile data. The wire format lives in one isolated place
> (`src/jetkvm/client.ts`) so it's easy to re-check against your firmware.
>
> **Setting up a device? → [SETUP-ko.md](SETUP-ko.md)** (한국어) walks through
> the whole thing in order: device install, Tailscale Funnel, router port
> forwarding, and what to type into the app.

## How it works

It speaks the same protocol JetKVM's own web UI uses:

1. `POST /auth/login-local` — logs in with the device's local password
   (skipped when password protection is disabled) and gets a session cookie.
2. `/webrtc/signaling/client` (**WebSocket**) — sends a base64-encoded SDP
   **offer**, receives the **answer**, then trickles ICE candidates both ways.
   `POST /webrtc/session` is kept as a fallback, but going that route leaves
   the device unable to trickle candidates back at all (its `config.ws` stays
   nil, so `OnICECandidate` never fires) — see `exchangeSdpOverWs`.
3. A WebRTC **media track** delivers the remote screen (H.264 video).
4. The **`rpc`** data channel carries JSON-RPC keyboard/mouse reports:
   `keyboardReport`, `absMouseReport`, `relMouseReport`, `wheelReport`.
   (The binary `hidrpc` channels are created because the firmware's own
   frontend creates them, but this client doesn't use them — the JSON-RPC
   path on `rpc` is the documented compat route and needs no guessing at an
   undocumented byte layout.)

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

| Setup | Host to enter | Public IP field |
|-------|---------------|-----------------|
| Same LAN | `192.168.x.x` | *(leave empty)* |
| **Tailscale Funnel + router port forward** (verified from mobile data) | `your-device.your-tailnet.ts.net` | router's public IP |
| JetKVM Cloud | *(not supported — Cloud uses a different broker/auth (Google OIDC) than the local API this app talks to)* | — |

**Remote access needs both halves, and they carry different traffic:**

- **Funnel** carries login + signaling (HTTPS/WSS). Without it the app has no
  address to dial at all and fails during *authenticating*.
- **A UDP port-range forward on the router** carries the actual media. Without
  it, signaling succeeds but the connection hangs in *connecting*.

Why the split: JetKVM has no kernel TUN, so Tailscale runs in
`--tun=userspace-networking`, which doesn't accept inbound connections on its
`100.x` address — only traffic proxied through Funnel reaches the device. And
Funnel only relays HTTPS, so WebRTC's UDP can't ride it. Hence the second path.

The device also only ever advertises its own private LAN address as an ICE
candidate (no STUN/TURN in its firmware), which is useless to any outside
client and unreachable by any TURN relay — the `publicIp` setting exists to
synthesize a reachable candidate from it. See `withPublicIpCandidate` in
`client.ts`.

## Project layout

```
src/
  jetkvm/
    client.ts               ← WebRTC + signaling + HID (the whole protocol lives here)
    transport.ts            ← cookie-aware HTTP bridge (see Cross-origin cookies)
    hid.ts                  ← USB HID keycode maps + keyboard state
    updateCheck.ts          ← "new version available" check + in-app APK install
    settingsTranslations.ts ← EN→KO dictionary for the device's own settings page
  storage/
    devices.ts              ← saved-device list (localStorage)
  components/
    DeviceList.tsx          ← AnyDesk-style device manager
    Viewer.tsx              ← video surface, touch/mouse input, on-screen keyboard
  App.tsx
electron/
  main.cjs                  ← desktop shell + same-origin reverse proxy
  preload.cjs               ← contextBridge to the two privileged IPC calls
android/app/src/main/java/com/jetkvm/remote/
  JetKvmProxyServer.java    ← Android's equivalent of the Electron reverse proxy
  SettingsProxyPlugin.java  ← JS handle for pointing that proxy at a device
  UpdaterPlugin.java        ← downloads the APK and opens the system installer
capacitor.config.ts         ← mobile wrapper config
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

## Re-checking against a different firmware

All of the below are confirmed working against real hardware. If a firmware
revision ever breaks something, these are the places it would show up — all in
`src/jetkvm/client.ts`:

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
