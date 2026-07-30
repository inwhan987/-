package com.jetkvm.remote;

import android.os.Bundle;
import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        // Reverted: forcing IPv4 here didn't actually change what address
        // family CapacitorHttp's /auth/login-local and /webrtc/session
        // calls used (still IPv6 per device-side logs after this was
        // added), and a genuine Tailscale-routed test still failed with
        // the exact same signature -- ruling out IPv6 as the cause. Worse,
        // our own OkHttp-based signaling WS (the one component this
        // property *does* affect, being plain java.net-based) got
        // noticeably slower after adding this (WS handshake time went from
        // ~4-5s to ~8s on the very next LTE attempt) -- plausibly because
        // it was forced off a fast IPv6 path onto a slower IPv4 one while
        // everything else stayed on IPv6 regardless. The actual fix for
        // slow/dropped candidate delivery is the session-establishment
        // gating in client.ts, not this.
        registerPlugin(SettingsProxyPlugin.class);
        // Must be listening before super.onCreate() loads the WebView --
        // capacitor.config.ts points the whole app at this local server
        // (see JetKvmProxyServer's class comment for why).
        JetKvmProxyServer.ensureStarted(getApplicationContext());
        super.onCreate(savedInstanceState);
    }
}
