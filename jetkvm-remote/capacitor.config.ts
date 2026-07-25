import type { CapacitorConfig } from '@capacitor/cli';

// Capacitor wraps the built web app (dist/) into native Android + iOS projects.
// The system WebView on both platforms supports WebRTC, so the same React/
// WebRTC code that runs in a browser runs unmodified inside the app.
const config: CapacitorConfig = {
  appId: 'com.jetkvm.remote',
  appName: 'JetKVM Remote',
  webDir: 'dist',
  server: {
    // JetKVM devices behind Tailscale Funnel use valid Let's Encrypt certs, so
    // no cleartext exception is needed. If you connect to a raw device IP over
    // http/self-signed cert, you'll need androidScheme + a cleartext/cert
    // exception here.
    androidScheme: 'https',
  },
};

export default config;
