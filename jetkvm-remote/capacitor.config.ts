import type { CapacitorConfig } from '@capacitor/cli';

// Capacitor wraps the built web app (dist/) into native Android + iOS projects.
// The system WebView on both platforms supports WebRTC, so the same React/
// WebRTC code that runs in a browser runs unmodified inside the app.
const config: CapacitorConfig = {
  appId: 'com.jetkvm.remote',
  appName: '원격KVM',
  webDir: 'dist',
  server: {
    // The whole app loads from JetKvmProxyServer's local loopback server
    // (android/.../JetKvmProxyServer.java) instead of Capacitor's default
    // https://localhost virtual scheme, so that the device's own /settings
    // page -- proxied through that same server -- is same-origin with our
    // own app. Cookies set inside the settings iframe are otherwise
    // silently dropped: they're only sent when every frame in the ancestor
    // chain, including the top-level page, is same-site with the request.
    //
    // NOTE: this only has a real listener on Android right now (no iOS
    // build/native proxy exists yet -- if iOS is added later this needs its
    // own WKURLSchemeHandler-based equivalent, and this url/cleartext pair
    // would need to become Android-only).
    url: 'http://127.0.0.1:47623',
    cleartext: true,
  },
  // JetKVM's local API sets no CORS headers, so a WebView-context fetch() to
  // it (a different origin than our app) gets its cross-origin cookie/session
  // blocked by the browser engine. CapacitorHttp routes window.fetch through
  // native iOS/Android networking instead of the WebView engine, which isn't
  // subject to CORS at all, so the authToken cookie set by /auth/login-local
  // just works on subsequent requests exactly like a native Go/Python client.
  plugins: {
    CapacitorHttp: { enabled: true },
    CapacitorCookies: { enabled: true },
  },
};

export default config;
