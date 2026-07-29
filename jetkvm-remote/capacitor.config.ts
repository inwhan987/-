import type { CapacitorConfig } from '@capacitor/cli';

// Capacitor wraps the built web app (dist/) into native Android + iOS projects.
// The system WebView on both platforms supports WebRTC, so the same React/
// WebRTC code that runs in a browser runs unmodified inside the app.
const config: CapacitorConfig = {
  appId: 'com.jetkvm.remote',
  appName: '원격KVM',
  webDir: 'dist',
  server: {
    // JetKVM devices behind Tailscale Funnel use valid Let's Encrypt certs, so
    // no cleartext exception is needed. If you connect to a raw device IP over
    // http/self-signed cert, you'll need androidScheme + a cleartext/cert
    // exception here.
    androidScheme: 'https',
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
