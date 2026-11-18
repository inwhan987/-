package com.jetkvm.remote;

import android.text.TextUtils;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import java.io.IOException;
import java.util.HashMap;
import java.util.Iterator;
import java.util.List;
import java.util.Map;
import okhttp3.Call;
import okhttp3.Callback;
import okhttp3.Response;

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

    @PluginMethod
    public void request(PluginCall call) {
        String url = call.getString("url");
        String method = call.getString("method", "GET");
        String data = call.getString("data");
        Map<String, String> headers = new HashMap<>();
        JSObject h = call.getObject("headers", new JSObject());
        Iterator<String> keys = h.keys();
        while (keys.hasNext()) {
            String k = keys.next();
            headers.put(k, h.getString(k));
        }
        JetKvmProxyServer.deviceRequest(url, method, headers, data, new Callback() {
            @Override
            public void onResponse(Call c, Response response) {
                try {
                    JSObject outHeaders = new JSObject();
                    List<String> cookies = response.headers("Set-Cookie");
                    if (!cookies.isEmpty()) outHeaders.put("set-cookie", TextUtils.join("\n", cookies));
                    JSObject ret = new JSObject();
                    ret.put("status", response.code());
                    ret.put("headers", outHeaders);
                    ret.put("data", response.body() != null ? response.body().string() : "");
                    call.resolve(ret);
                } catch (IOException e) {
                    call.reject(String.valueOf(e));
                } finally {
                    response.close();
                }
            }

            @Override
            public void onFailure(Call c, IOException e) {
                call.reject(String.valueOf(e));
            }
        });
    }
}
