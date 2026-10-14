package app.leotruyen.reader;

import android.content.Intent;
import android.os.Handler;
import android.os.Looper;
import android.speech.tts.TextToSpeech;
import android.speech.tts.Voice;
import com.getcapacitor.JSArray;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import org.json.JSONException;
import org.json.JSONObject;

@CapacitorPlugin(name = "NativeTts")
public class NativeTtsPlugin extends Plugin {
    @PluginMethod
    public void listVoices(PluginCall call) {
        final TextToSpeech[] probe = new TextToSpeech[1];
        probe[0] = new TextToSpeech(getContext(), result -> new Handler(Looper.getMainLooper()).post(() -> {
            if (result != TextToSpeech.SUCCESS) {
                probe[0].shutdown();
                call.reject("Không khởi tạo được Android TTS.");
                return;
            }
            List<Voice> voices = probe[0].getVoices() == null ? new ArrayList<>() : new ArrayList<>(probe[0].getVoices());
            voices.removeIf(voice -> !"vi".equals(voice.getLocale().getLanguage()));
            voices.sort(Comparator.comparing(Voice::isNetworkConnectionRequired).thenComparing(Voice::getName));
            JSArray rows = new JSArray();
            for (Voice voice : voices) {
                JSObject row = new JSObject();
                row.put("id", voice.getName());
                row.put("name", voice.getName());
                row.put("lang", voice.getLocale().toLanguageTag());
                row.put("offline", !voice.isNetworkConnectionRequired() && !voice.getFeatures().contains(TextToSpeech.Engine.KEY_FEATURE_NOT_INSTALLED));
                row.put("installed", !voice.getFeatures().contains(TextToSpeech.Engine.KEY_FEATURE_NOT_INSTALLED));
                rows.put(row);
            }
            probe[0].shutdown();
            JSObject response = new JSObject();
            response.put("voices", rows);
            call.resolve(response);
        }));
    }

    @PluginMethod
    public void speak(PluginCall call) {
        JSArray chunks = call.getArray("chunks");
        if (chunks == null || chunks.length() == 0 || chunks.length() > 1000) {
            call.reject("Hàng đợi TTS không hợp lệ.");
            return;
        }
        try {
            int total = 0;
            for (int i = 0; i < chunks.length(); i++) {
                String chunk = chunks.getString(i);
                if (chunk.isBlank() || chunk.length() > 3000) throw new IllegalArgumentException("Đoạn TTS quá dài hoặc rỗng.");
                total += chunk.length();
            }
            if (total > 1_000_000) throw new IllegalArgumentException("Chương vượt giới hạn TTS.");
            JSONObject queue = new JSONObject();
            queue.put("chapterId", call.getString("chapterId", ""));
            queue.put("title", call.getString("title", "Chương đang đọc"));
            queue.put("chunks", chunks);
            File file = File.createTempFile("native-tts-", ".json", getContext().getCacheDir());
            try (FileOutputStream output = new FileOutputStream(file)) {
                output.write(queue.toString().getBytes(StandardCharsets.UTF_8));
            }
            Intent intent = action(NativeTtsService.START);
            intent.putExtra("queuePath", file.getAbsolutePath());
            intent.putExtra("startIndex", Math.max(0, call.getInt("startIndex", 0)));
            intent.putExtra("voice", call.getString("voice", ""));
            intent.putExtra("rate", call.getDouble("rate", 1.0));
            intent.putExtra("sleepUntil", call.getLong("sleepUntil", 0L));
            getContext().startForegroundService(intent);
            call.resolve();
        } catch (Exception error) {
            call.reject("Không bắt đầu được Android TTS: " + error.getMessage());
        }
    }

    @PluginMethod public void pause(PluginCall call) { command(call, NativeTtsService.PAUSE); }
    @PluginMethod public void resume(PluginCall call) { command(call, NativeTtsService.RESUME); }
    @PluginMethod public void stop(PluginCall call) { command(call, NativeTtsService.STOP); }
    @PluginMethod public void previous(PluginCall call) { command(call, NativeTtsService.PREVIOUS); }
    @PluginMethod public void next(PluginCall call) { command(call, NativeTtsService.NEXT); }

    @PluginMethod public void seek(PluginCall call) {
        if (NativeTtsService.isRunning()) {
            Intent intent = action(NativeTtsService.SEEK);
            intent.putExtra("index", Math.max(0, call.getInt("index", 0)));
            getContext().startService(intent);
        }
        call.resolve();
    }

    @PluginMethod
    public void setVoice(PluginCall call) {
        String voice = call.getString("voice", "");
        getContext().getSharedPreferences("native-tts", 0).edit().putString("voice", voice).apply();
        Intent intent = action(NativeTtsService.SET_VOICE);
        intent.putExtra("voice", voice);
        if (NativeTtsService.isRunning()) getContext().startService(intent);
        call.resolve();
    }

    @PluginMethod
    public void setRate(PluginCall call) {
        double rate = call.getDouble("rate", 1.0);
        if (rate < 0.5 || rate > 2.0) { call.reject("Tốc độ phải từ 0.5 đến 2."); return; }
        getContext().getSharedPreferences("native-tts", 0).edit().putFloat("rate", (float) rate).apply();
        Intent intent = action(NativeTtsService.SET_RATE);
        intent.putExtra("rate", rate);
        if (NativeTtsService.isRunning()) getContext().startService(intent);
        call.resolve();
    }

    @PluginMethod
    public void setSleep(PluginCall call) {
        if (NativeTtsService.isRunning()) {
            Intent intent = action(NativeTtsService.SET_SLEEP);
            intent.putExtra("sleepUntil", call.getLong("until", 0L));
            getContext().startService(intent);
        }
        call.resolve();
    }

    @PluginMethod
    public void getState(PluginCall call) {
        NativeTtsService.State state = NativeTtsService.snapshot();
        JSObject result = new JSObject();
        result.put("status", state.status);
        result.put("chapterId", state.chapterId);
        result.put("index", state.index);
        result.put("total", state.total);
        result.put("voice", state.voice);
        result.put("rate", state.rate);
        result.put("error", state.error);
        call.resolve(result);
    }

    @PluginMethod
    public void installVoiceData(PluginCall call) {
        try {
            Intent intent = new Intent(TextToSpeech.Engine.ACTION_INSTALL_TTS_DATA);
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            getContext().startActivity(intent);
            call.resolve();
        } catch (Exception error) {
            call.reject("Không mở được màn hình cài giọng đọc.", error);
        }
    }

    private Intent action(String name) { return new Intent(getContext(), NativeTtsService.class).setAction(name); }
    private void command(PluginCall call, String name) {
        if (NativeTtsService.isRunning()) getContext().startService(action(name));
        call.resolve();
    }
}
