package com.jetkvm.remote;

import android.content.Context;
import android.content.res.AssetManager;
import android.util.Base64;
import android.util.Log;
import fi.iki.elonen.NanoHTTPD;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.HashMap;
import java.util.Map;
import org.json.JSONException;
import org.json.JSONObject;

/**
 * Local same-origin reverse proxy, the Android counterpart of the desktop
 * Electron build's electron/main.cjs. The whole app (see capacitor.config.ts
 * server.android.url) is served from here instead of Capacitor's default
 * https://localhost virtual scheme, so that the device's own /settings page
 * -- proxied through this same server -- is same-origin with our app. That
 * matters because a nested iframe's cookies are only sent when EVERY frame
 * in the ancestor chain (including the top-level page) is same-site with the
 * request; anything less and the device's session cookie gets silently
 * dropped, which is exactly what happened the first time this was tried by
 * pointing an iframe straight at the device.
 *
 * Two jobs, same split as the Electron version:
 *  1. Serve our own bundled web app (Capacitor's normal "public" assets)
 *     for paths that are obviously ours ("/", "/index.html", "/assets/*").
 *  2. Forward everything else to whatever device is currently active,
 *     stripping framing-blocking headers (X-Frame-Options / CSP
 *     frame-ancestors) so the settings iframe isn't blocked, and rejecting
 *     the video+audio m-lines in outgoing WebRTC SDP offers (RFC 3264 ss6:
 *     port 0) so the device's one hardware encoder isn't fought over by our
 *     own connection and the settings page's live preview at the same time.
 */
public class JetKvmProxyServer extends NanoHTTPD {

    public static final int PORT = 47623;
    private static JetKvmProxyServer instance;

    private final Context appContext;
    private volatile String proxyTarget; // e.g. "https://remote-desktop.taileb686e.ts.net"

    private JetKvmProxyServer(Context context) {
        super("127.0.0.1", PORT);
        this.appContext = context.getApplicationContext();
    }

    public static synchronized void ensureStarted(Context context) {
        if (instance == null) {
            instance = new JetKvmProxyServer(context);
            try {
                instance.start(NanoHTTPD.SOCKET_READ_TIMEOUT, false);
            } catch (IOException e) {
                Log.e("JetKvmProxyServer", "failed to start local proxy", e);
            }
        }
    }

    public static void setProxyTarget(String target) {
        if (instance != null) instance.proxyTarget = target;
    }

    @Override
    public Response serve(IHTTPSession session) {
        String path = session.getUri();
        boolean isOwnAsset =
            "/".equals(path) || "/index.html".equals(path) || path.startsWith("/assets/");
        if (isOwnAsset) return serveOwnAsset(path);
        return proxyToDevice(session, path);
    }

    private Response serveOwnAsset(String path) {
        String assetPath = "public" + ("/".equals(path) ? "/index.html" : path);
        try {
            AssetManager assets = appContext.getAssets();
            android.content.res.AssetFileDescriptor fd = assets.openFd(assetPath);
            InputStream in = fd.createInputStream();
            return newFixedLengthResponse(customStatus(200), mimeTypeFor(assetPath), in, fd.getLength());
        } catch (IOException e) {
            try {
                InputStream in = appContext.getAssets().open(assetPath);
                byte[] bytes = readAll(in);
                return newFixedLengthResponse(
                    customStatus(200),
                    mimeTypeFor(assetPath),
                    new java.io.ByteArrayInputStream(bytes),
                    bytes.length);
            } catch (IOException e2) {
                return textResponse(404, "Not found");
            }
        }
    }

    private static String mimeTypeFor(String path) {
        if (path.endsWith(".html")) return "text/html; charset=utf-8";
        if (path.endsWith(".js")) return "application/javascript";
        if (path.endsWith(".css")) return "text/css";
        if (path.endsWith(".svg")) return "image/svg+xml";
        if (path.endsWith(".png")) return "image/png";
        if (path.endsWith(".json")) return "application/json";
        return "application/octet-stream";
    }

    private Response proxyToDevice(IHTTPSession session, String path) {
        String target = proxyTarget;
        if (target == null) {
            return textResponse(502, "No device set for this session yet.");
        }

        HttpURLConnection conn = null;
        try {
            String query = session.getQueryParameterString();
            String url = target + path + (query != null && !query.isEmpty() ? "?" + query : "");
            conn = (HttpURLConnection) new URL(url).openConnection();
            conn.setInstanceFollowRedirects(false);
            conn.setConnectTimeout(10_000);
            conn.setReadTimeout(20_000);

            Method method = session.getMethod();
            conn.setRequestMethod(method.name());

            for (Map.Entry<String, String> h : session.getHeaders().entrySet()) {
                String key = h.getKey();
                // Let HttpURLConnection manage these itself.
                if (key.equalsIgnoreCase("host") || key.equalsIgnoreCase("content-length")) continue;
                try {
                    conn.setRequestProperty(key, h.getValue());
                } catch (Exception ignored) {
                    /* a handful of headers are restricted; skip them */
                }
            }

            byte[] body = null;
            if (method == Method.POST || method == Method.PUT) {
                Map<String, String> files = new HashMap<>();
                session.parseBody(files); // NanoHTTPD quirk: reads the body as a side effect
                String raw = files.get("postData");
                body = raw != null ? raw.getBytes("UTF-8") : new byte[0];
                if ("/webrtc/session".equals(path)) {
                    byte[] rewritten = rejectVideoInOffer(body);
                    if (rewritten != null) body = rewritten;
                }
                conn.setDoOutput(true);
                conn.setRequestProperty("Content-Length", String.valueOf(body.length));
                OutputStream os = conn.getOutputStream();
                os.write(body);
                os.close();
            }

            int status = conn.getResponseCode();
            InputStream respStream = status >= 400 ? conn.getErrorStream() : conn.getInputStream();
            byte[] respBody = readAll(respStream);

            String contentType = conn.getContentType();
            Response response = newFixedLengthResponse(
                customStatus(status),
                contentType != null ? contentType : "application/octet-stream",
                new java.io.ByteArrayInputStream(respBody),
                respBody.length);

            for (Map.Entry<String, java.util.List<String>> h : conn.getHeaderFields().entrySet()) {
                String key = h.getKey();
                if (key == null) continue; // status line shows up with a null key
                String lower = key.toLowerCase();
                // Framing-blocking headers, stripped for the same reason the
                // desktop proxy strips them: this is our own trusted local
                // relay serving only our own iframe, so it's safe here and
                // it's the only way framing works at all.
                if (lower.equals("x-frame-options")) continue;
                if (lower.equals("content-length") || lower.equals("transfer-encoding")) continue;
                for (String value : h.getValue()) {
                    String v = value;
                    if (lower.equals("content-security-policy")) {
                        v = v.replaceAll("(?i)frame-ancestors[^;]*;?\\s*", "");
                    }
                    response.addHeader(key, v);
                }
            }
            return response;
        } catch (Exception e) {
            Log.e("JetKvmProxyServer", "proxy error for " + path, e);
            return textResponse(502, String.valueOf(e));
        } finally {
            if (conn != null) conn.disconnect();
        }
    }

    // Mirrors electron/main.cjs's rejectVideoInOffer: parses the {sd: base64}
    // POST body, decodes the SDP, rejects the video and audio m-lines (RFC
    // 3264 ss6: port 0) so neither track is ever negotiated, while leaving
    // the data channels (a separate m=application section) untouched.
    private static byte[] rejectVideoInOffer(byte[] bodyBytes) {
        try {
            String bodyText = new String(bodyBytes, "UTF-8");
            JSONObject payload = new JSONObject(bodyText);
            String sd = payload.optString("sd", null);
            if (sd == null) return null;
            String descText = new String(Base64.decode(sd, Base64.DEFAULT), "UTF-8");
            JSONObject desc = new JSONObject(descText);
            String sdp = desc.optString("sdp", null);
            if (sdp == null) return null;

            String rewritten = sdp
                .replaceAll("(?m)^m=video \\d+", "m=video 0")
                .replaceAll("(?m)^m=audio \\d+", "m=audio 0");
            if (rewritten.equals(sdp)) return null;

            desc.put("sdp", rewritten);
            String newSd = Base64.encodeToString(desc.toString().getBytes("UTF-8"), Base64.NO_WRAP);
            payload.put("sd", newSd);
            return payload.toString().getBytes("UTF-8");
        } catch (JSONException | java.io.UnsupportedEncodingException e) {
            return null; // not the shape we expect -- forward untouched
        }
    }

    private static byte[] readAll(InputStream in) throws IOException {
        if (in == null) return new byte[0];
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int n;
        while ((n = in.read(buf)) != -1) out.write(buf, 0, n);
        return out.toByteArray();
    }

    // Builds an IStatus for an arbitrary numeric code directly, instead of
    // relying on which named constants happen to exist on Response.Status.
    private static Response.IStatus customStatus(final int code) {
        return new Response.IStatus() {
            public int getRequestStatus() {
                return code;
            }

            public String getDescription() {
                return String.valueOf(code);
            }
        };
    }

    private static Response textResponse(int code, String text) {
        byte[] bytes;
        try {
            bytes = text.getBytes("UTF-8");
        } catch (java.io.UnsupportedEncodingException e) {
            bytes = text.getBytes();
        }
        return newFixedLengthResponse(
            customStatus(code), "text/plain", new java.io.ByteArrayInputStream(bytes), bytes.length);
    }
}
