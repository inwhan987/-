package com.jetkvm.remote;

import android.os.Bundle;
import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        registerPlugin(SettingsProxyPlugin.class);
        // Must be listening before super.onCreate() loads the WebView --
        // capacitor.config.ts points the whole app at this local server
        // (see JetKvmProxyServer's class comment for why).
        JetKvmProxyServer.ensureStarted(getApplicationContext());
        super.onCreate(savedInstanceState);
    }
}
