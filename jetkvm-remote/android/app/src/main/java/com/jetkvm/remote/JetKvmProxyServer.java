package com.jetkvm.remote;

import android.content.Context;
import android.content.res.AssetManager;
import android.util.Base64;
import android.util.Log;
import fi.iki.elonen.NanoHTTPD;
import fi.iki.elonen.NanoWSD;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.TimeUnit;
import okhttp3.Call;
import okhttp3.Callback;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.WebSocketListener;
import okio.ByteString;
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
public class JetKvmProxyServer extends NanoWSD {

    public static final int PORT = 47623;
    private static JetKvmProxyServer instance;

    private final Context appContext;
    private volatile String proxyTarget; // e.g. "https://remote-desktop.taileb686e.ts.net"
    // Outbound leg for proxied WebSocket connections (the settings page's own
    // /webrtc/signaling/client, opened for its live preview). Kept open-ended
    // -- these sockets are meant to live as long as the settings page does.
    private final OkHttpClient wsClient =
        new OkHttpClient.Builder().readTimeout(0, TimeUnit.MILLISECONDS).build();

    private JetKvmProxyServer(Context context) {
        super("127.0.0.1", PORT);
        this.appContext = context.getApplicationContext();
    }

    public static synchronized void ensureStarted(Context context) {
        if (instance == null) {
            instance = new JetKvmProxyServer(context);
            try {
                // NanoHTTPD.SOCKET_READ_TIMEOUT (5000ms) is meant for short-lived
                // HTTP requests; applied here it also caps every accepted socket,
                // including the long-lived signaling WebSocket between the
                // WebView and this proxy. Our side sends its offer once and then
                // goes idle waiting for the device to trickle ICE candidates back
                // -- nothing more is sent browser->proxy on that socket -- so 5s
                // after the offer, the read on that client-facing socket timed
                // out and NanoHTTPD force-closed it (confirmed via app log:
                // "signaling ws closed (code 1006)" ~5s after the offer, well
                // before the device had trickled anything past its first host
                // candidate). Java's Socket.setSoTimeout(0) means "no timeout",
                // which is what a session that can sit idle for minutes (no
                // mouse movement) actually needs.
                instance.start(0, false);
            } catch (IOException e) {
                Log.e("JetKvmProxyServer", "failed to start local proxy", e);
            }
        }
    }

    public static void setProxyTarget(String target) {
        if (instance != null) {
            instance.proxyTarget = target;
            instance.prewarmConnection(target);
        }
    }

    // The signaling WS relay (DeviceRelayWebSocket, below) was confirmed via
    // device-side logs to take 4-8s just to finish its own TCP+TLS+WS
    // handshake on a real LTE connection, well after login/session calls to
    // the same host (via CapacitorHttp, a separate client) were already
    // completing in under half a second -- pointing at cold-connection
    // setup specifically for this OkHttpClient instance, not general
    // network slowness. setProxyTarget() is called as soon as a device is
    // selected, well before any connection attempt starts, so firing a
    // throwaway request through the SAME client (and therefore the same
    // connection pool) here gives OkHttp a chance to have a warm TCP+TLS
    // connection to the device already sitting in the pool by the time the
    // real WS upgrade needs one. Best-effort: any failure here is silently
    // discarded, since this is purely a latency optimization, not a
    // required step.
    private void prewarmConnection(String target) {
        Request req = new Request.Builder().url(target + "/").head().build();
        wsClient.newCall(req).enqueue(new Callback() {
            @Override
            public void onResponse(Call call, okhttp3.Response response) {
                response.close();
            }

            @Override
            public void onFailure(Call call, IOException e) {
                /* best-effort -- the real connection attempt still tries fresh */
            }
        });
    }

    // NanoWSD's own serve() already does the WS-vs-not dispatch (validates
    // Sec-WebSocket-Version/-Key, builds the 101 response, calls
    // openWebSocket() for upgrades) and calls this for everything else --
    // no need to reimplement that check ourselves.
    @Override
    protected Response serveHttp(IHTTPSession session) {
        String path = session.getUri();
        boolean isOwnAsset =
            "/".equals(path) || "/index.html".equals(path) || path.startsWith("/assets/");
        if (isOwnAsset) return serveOwnAsset(path);
        return proxyToDevice(session, path);
    }

    // The settings page opens its own WebSocket (wss://<device>/webrtc/
    // signaling/client) for trickled ICE candidates on its live preview
    // connection -- entirely separate from our own client.ts video
    // connection, which never goes through this proxy at all. Before this,
    // proxyToDevice()'s HttpURLConnection had no way to handle an Upgrade
    // request, so every such attempt failed with a 502 (confirmed via
    // remote-debugging the settings iframe: "Unexpected response code:
    // 502"), leaving the settings page's live status/preview panel
    // permanently disconnected. NanoWSD does the client-side handshake and
    // framing for us; we just need to relay frames to/from a real
    // WebSocket to the device, via OkHttp.
    @Override
    protected WebSocket openWebSocket(IHTTPSession handshake) {
        return new DeviceRelayWebSocket(handshake);
    }

    private final class DeviceRelayWebSocket extends NanoWSD.WebSocket {
        private volatile okhttp3.WebSocket upstream;

        DeviceRelayWebSocket(IHTTPSession handshakeRequest) {
            super(handshakeRequest);
        }

        @Override
        protected void onOpen() {
            String target = proxyTarget;
            if (target == null) {
                try {
                    close(NanoWSD.WebSocketFrame.CloseCode.PolicyViolation, "no device set", false);
                } catch (IOException ignored) {
                    /* closing an already-broken socket */
                }
                return;
            }
            String query = getHandshakeRequest().getQueryParameterString();
            String wsUrl =
                target.replaceFirst("^http", "ws")
                    + "/webrtc/signaling/client"
                    + (query != null && !query.isEmpty() ? "?" + query : "");
            Request.Builder reqBuilder = new Request.Builder().url(wsUrl);
            // proxyToDevice() forwards the browser's Cookie header on plain
            // HTTP requests (that's how the settings iframe stays logged in
            // at all); this WS upgrade built a brand new OkHttp request from
            // scratch with no headers, so the device correctly rejected it
            // -- confirmed via remote-debugging: "Expected HTTP 101 response
            // but was '401 Unauthorized'". Forward the same cookie here.
            String cookie = getHandshakeRequest().getHeaders().get("cookie");
            if (cookie != null) reqBuilder.addHeader("Cookie", cookie);
            Request req = reqBuilder.build();
            upstream = wsClient.newWebSocket(req, new WebSocketListener() {
                @Override
                public void onMessage(okhttp3.WebSocket ws, String text) {
                    try {
                        DeviceRelayWebSocket.this.send(text);
                    } catch (IOException ignored) {
                        /* our client-side socket already gone */
                    }
                }

                @Override
                public void onMessage(okhttp3.WebSocket ws, ByteString bytes) {
                    try {
                        DeviceRelayWebSocket.this.send(bytes.toByteArray());
                    } catch (IOException ignored) {
                        /* our client-side socket already gone */
                    }
                }

                @Override
                public void onClosed(okhttp3.WebSocket ws, int code, String reason) {
                    try {
                        DeviceRelayWebSocket.this.close(
                            NanoWSD.WebSocketFrame.CloseCode.NormalClosure, reason, false);
                    } catch (IOException ignored) {
                        /* already closing */
                    }
                }

                @Override
                public void onFailure(okhttp3.WebSocket ws, Throwable t, okhttp3.Response response) {
                    Log.e("JetKvmProxyServer", "upstream device WS failed", t);
                    try {
                        // AbnormalClosure (1006) is reserved -- RFC 6455 forbids ever
                        // putting it on the wire in an actual Close frame (browsers use
                        // it internally to mean "no close frame was received at all").
                        // Sending it made every relayed failure look like protocol
                        // corruption to the browser ("broken close frame containing a
                        // reserved status code") instead of a clean close. 1011 is the
                        // correct code for "something went wrong on the server side".
                        DeviceRelayWebSocket.this.close(
                            NanoWSD.WebSocketFrame.CloseCode.InternalServerError, String.valueOf(t), false);
                    } catch (IOException ignored) {
                        /* already closing */
                    }
                }
            });
        }

        @Override
        protected void onClose(
            NanoWSD.WebSocketFrame.CloseCode code, String reason, boolean initiatedByRemote) {
            if (upstream != null) upstream.close(1000, reason);
        }

        @Override
        protected void onMessage(NanoWSD.WebSocketFrame message) {
            okhttp3.WebSocket up = upstream;
            if (up == null) return;
            if (message.getOpCode() == NanoWSD.WebSocketFrame.OpCode.Text) {
                up.send(message.getTextPayload());
            } else {
                up.send(ByteString.of(message.getBinaryPayload()));
            }
        }

        @Override
        protected void onPong(NanoWSD.WebSocketFrame pong) {
            /* no-op -- OkHttp answers pings on the upstream leg itself */
        }

        @Override
        protected void onException(IOException exception) {
            Log.e("JetKvmProxyServer", "client-side WS error", exception);
        }
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

    // The device's own settings page negotiates its OWN separate WebRTC
    // connection (its own RTCPeerConnection, for its own live status/
    // settings data) -- entirely outside our app's JS, so nothing on the
    // client.ts side can help it. But its HTML/JS passes through this same
    // proxy, so we can still reach it: inject a tiny script ahead of the
    // page's own bundle that wraps window.RTCPeerConnection to merge our
    // TURN servers into whatever config the device's own code passes,
    // before handing off to the real constructor. JetKVM does have its own
    // free TURN via Cloudflare, but only when reached through their own
    // Cloud/OIDC-login remote access -- going in through our own local
    // proxy (Funnel etc. instead of JetKVM Cloud), the settings page has
    // no way to get those credentials and configures no TURN at all, which
    // is the actual reason it fails to connect from outside the LAN.
    // Kept in sync with src/jetkvm/client.ts's DEFAULT_ICE (same free
    // public TURN service, Metered's OpenRelay).
    private static final String ICE_INJECTION_SCRIPT =
        "<script>(function(){"
            + "var extraIce=[{urls:'stun:stun.l.google.com:19302'},"
            + "{urls:'stun:openrelay.metered.ca:80'},"
            + "{urls:'turn:openrelay.metered.ca:80',username:'openrelayproject',credential:'openrelayproject'},"
            + "{urls:'turn:openrelay.metered.ca:443',username:'openrelayproject',credential:'openrelayproject'},"
            + "{urls:'turn:openrelay.metered.ca:443?transport=tcp',username:'openrelayproject',credential:'openrelayproject'},"
            + "{urls:'stun:stun.relay.metered.ca:80'},"
            + "{urls:'turn:global.relay.metered.ca:80',username:'openrelayproject',credential:'openrelayproject'},"
            + "{urls:'turn:global.relay.metered.ca:443',username:'openrelayproject',credential:'openrelayproject'},"
            + "{urls:'turn:global.relay.metered.ca:443?transport=tcp',username:'openrelayproject',credential:'openrelayproject'},"
            + "{urls:'turns:global.relay.metered.ca:443?transport=tcp',username:'openrelayproject',credential:'openrelayproject'},"
            + "{urls:'turns:openrelay.metered.ca:443?transport=tcp',username:'openrelayproject',credential:'openrelayproject'}];"
            + "var Native=window.RTCPeerConnection;"
            + "if(!Native)return;"
            + "function Patched(config,constraints){"
            + "config=config||{};"
            + "var existing=Array.isArray(config.iceServers)?config.iceServers:[];"
            + "var merged={};"
            + "for(var k in config)merged[k]=config[k];"
            + "merged.iceServers=existing.concat(extraIce);"
            + "return new Native(merged,constraints);"
            + "}"
            + "Patched.prototype=Native.prototype;"
            + "window.RTCPeerConnection=Patched;"
            + "})();</script>";

    private static String injectIceScript(String html) {
        java.util.regex.Matcher m = java.util.regex.Pattern.compile("<head[^>]*>", java.util.regex.Pattern.CASE_INSENSITIVE).matcher(html);
        if (!m.find()) return html;
        int idx = m.end();
        return html.substring(0, idx) + ICE_INJECTION_SCRIPT + html.substring(idx);
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
                // We string-rewrite HTML bodies below (ICE-injection script)
                // without decompressing first -- force identity encoding so
                // that never runs on a gzip'd body it can't read.
                if (key.equalsIgnoreCase("accept-encoding")) continue;
                try {
                    conn.setRequestProperty(key, h.getValue());
                } catch (Exception ignored) {
                    /* a handful of headers are restricted; skip them */
                }
            }
            conn.setRequestProperty("Accept-Encoding", "identity");

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
            if (contentType != null && contentType.contains("text/html")) {
                String html = injectIceScript(new String(respBody, "UTF-8"));
                respBody = html.getBytes("UTF-8");
            }
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
