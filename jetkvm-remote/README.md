# JetKVM Remote

A cross-platform, AnyDesk-style remote client for [JetKVM](https://jetkvm.com).
One codebase → **Android, iOS, and desktop (Windows / macOS / Linux)**.

Open the app, pick a saved device, and you're on the machine — video of the
remote screen plus keyboard/mouse control, over a direct WebRTC connection to
the JetKVM device.

> **Status: pre-hardware scaffold.** This was built before the device was in
> hand, against JetKVM's open-source firmware protocol. The wire format lives in
> one isolated place (`src/jetkvm/client.ts`) so the handful of `VERIFY` points
> can be confirmed the moment a real device is connected. See
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

### Connection paths

Works with whatever address reaches the device:

| Setup | Host to enter |
|-------|---------------|
| Same LAN | `192.168.x.x` |
| Tailscale client | MagicDNS name or `100.x.x.x` |
| **Tailscale Funnel** | `jetkvm.your-tailnet.ts.net` |
| JetKVM Cloud | *(not supported — Cloud uses a different broker; this app connects directly)* |

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
- **Auth cookie** — `login-local` is assumed to set a session cookie that
  `fetch(..., { credentials: 'include' })` carries to the signaling call. If
  the firmware uses a bearer token instead, add the header in `authenticate()`.
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
```
