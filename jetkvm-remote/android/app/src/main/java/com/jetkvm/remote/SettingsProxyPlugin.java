package com.jetkvm.remote;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

/**
 * JS-facing handle for JetKvmProxyServer. The web/src side calls
 * setProxyTarget once it knows which device it's talking to -- same moment
 * Electron's window.jetkvmIpc.setProxyTarget is called on desktop.
 */
@CapacitorPlugin(name = "SettingsProxy")
public class SettingsProxyPlugin extends Plugin {

    @PluginMethod
    public void setProxyTarget(PluginCall call) {
        String base = call.getString("base");
        String publicIp = call.getString("publicIp");
        JetKvmProxyServer.setProxyTarget(base, publicIp);
        JSObject ret = new JSObject();
        ret.put("port", JetKvmProxyServer.PORT);
        call.resolve(ret);
    }
}
