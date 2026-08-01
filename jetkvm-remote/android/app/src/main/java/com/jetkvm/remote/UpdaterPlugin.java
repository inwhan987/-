package com.jetkvm.remote;

import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import androidx.core.content.FileProvider;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.util.concurrent.TimeUnit;
import okhttp3.Call;
import okhttp3.Callback;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;

/**
 * A sideloaded APK has no store to silently self-update through -- but "tap
 * the update banner once, the install screen pops up" beats "open a
 * browser, find the release page, download the file, open Downloads, tap
 * it there instead." This downloads the APK straight into the app's own
 * cache dir and launches Android's own package installer on it, via a
 * FileProvider content:// URI (installing a raw file:// path has been
 * blocked since Android 7 without one -- the same FileProvider already
 * declared in AndroidManifest.xml for other purposes covers this too,
 * since its file_paths.xml already maps the whole cache dir).
 */
@CapacitorPlugin(name = "Updater")
public class UpdaterPlugin extends Plugin {

    private static final OkHttpClient client =
        new OkHttpClient.Builder()
            .connectTimeout(15, TimeUnit.SECONDS)
            .readTimeout(60, TimeUnit.SECONDS)
            .build();

    @PluginMethod
    public void downloadAndInstall(PluginCall call) {
        String url = call.getString("url");
        if (url == null || url.isEmpty()) {
            call.reject("missing url");
            return;
        }
        Context context = getContext();
        Request req = new Request.Builder().url(url).build();
        client.newCall(req).enqueue(new Callback() {
            @Override
            public void onFailure(Call c, IOException e) {
                call.reject("download failed: " + e.getMessage());
            }

            @Override
            public void onResponse(Call c, Response response) {
                if (!response.isSuccessful() || response.body() == null) {
                    call.reject("download failed: HTTP " + response.code());
                    response.close();
                    return;
                }
                File file = new File(context.getCacheDir(), "update.apk");
                try (InputStream in = response.body().byteStream();
                     FileOutputStream out = new FileOutputStream(file)) {
                    byte[] buf = new byte[8192];
                    int n;
                    while ((n = in.read(buf)) != -1) {
                        out.write(buf, 0, n);
                    }
                } catch (IOException e) {
                    call.reject("save failed: " + e.getMessage());
                    return;
                } finally {
                    response.close();
                }

                Uri apkUri = FileProvider.getUriForFile(
                    context, context.getPackageName() + ".fileprovider", file);
                Intent install = new Intent(Intent.ACTION_VIEW);
                install.setDataAndType(apkUri, "application/vnd.android.package-archive");
                install.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_GRANT_READ_URI_PERMISSION);
                context.startActivity(install);
                call.resolve();
            }
        });
    }
}
