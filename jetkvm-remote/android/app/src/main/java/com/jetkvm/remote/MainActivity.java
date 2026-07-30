package com.jetkvm.remote;

import android.os.Bundle;
import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        // Force IPv4 for all Java-level networking (CapacitorHttp's
        // /auth/login-local and /webrtc/session calls, and our own
        // JetKvmProxyServer's OkHttp WS relay). Confirmed via the device's
        // own logs (SSH) that on a real dual-stack LTE connection this app
        // was reaching the device over IPv6, while the only working
        // external case seen so far (WiFi, same phone) went out over IPv4
        // -- the JVM's default (Happy-Eyeballs-style) address selection
        // otherwise prefers IPv6 whenever it's available, which is exactly
        // what native LTE (464XLAT/NAT64) offers. Must be set before any
        // networking starts, so first thing here.
        System.setProperty("java.net.preferIPv4Stack", "true");
        registerPlugin(SettingsProxyPlugin.class);
        // Must be listening before super.onCreate() loads the WebView --
        // capacitor.config.ts points the whole app at this local server
        // (see JetKvmProxyServer's class comment for why).
        JetKvmProxyServer.ensureStarted(getApplicationContext());
        super.onCreate(savedInstanceState);
    }
}
