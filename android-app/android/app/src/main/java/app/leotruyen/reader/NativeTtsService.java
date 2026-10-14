package app.leotruyen.reader;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.media.AudioAttributes;
import android.media.AudioFocusRequest;
import android.media.AudioManager;
import android.media.MediaMetadata;
import android.media.session.MediaSession;
import android.media.session.PlaybackState;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.speech.tts.TextToSpeech;
import android.speech.tts.UtteranceProgressListener;
import android.speech.tts.Voice;
import java.io.File;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.List;
import org.json.JSONArray;
import org.json.JSONObject;

public class NativeTtsService extends Service {
    public static final String START = "app.leotruyen.reader.tts.START";
    public static final String PAUSE = "app.leotruyen.reader.tts.PAUSE";
    public static final String RESUME = "app.leotruyen.reader.tts.RESUME";
    public static final String STOP = "app.leotruyen.reader.tts.STOP";
    public static final String PREVIOUS = "app.leotruyen.reader.tts.PREVIOUS";
    public static final String NEXT = "app.leotruyen.reader.tts.NEXT";
    public static final String SEEK = "app.leotruyen.reader.tts.SEEK";
    public static final String SET_VOICE = "app.leotruyen.reader.tts.SET_VOICE";
    public static final String SET_RATE = "app.leotruyen.reader.tts.SET_RATE";
    public static final String SET_SLEEP = "app.leotruyen.reader.tts.SET_SLEEP";
    private static final String CHANNEL = "native-tts-playback";
    private static final int NOTIFICATION = 1081;

    public static final class State {
        public final String status, chapterId, voice, error;
        public final int index, total;
        public final float rate;
        State(String status, String chapterId, int index, int total, String voice, float rate, String error) {
            this.status = status; this.chapterId = chapterId; this.index = index; this.total = total;
            this.voice = voice; this.rate = rate; this.error = error;
        }
    }
    private static volatile State current = new State("idle", "", 0, 0, "", 1f, "");
    private static volatile boolean running;
    public static State snapshot() { return current; }
    public static boolean isRunning() { return running; }

    private final Handler main = new Handler(Looper.getMainLooper());
    private final List<String> chunks = new ArrayList<>();
    private TextToSpeech tts;
    private MediaSession session;
    private AudioManager audioManager;
    private AudioFocusRequest focusRequest;
    private boolean ready, playing, paused, pausedByFocus, foreground;
    private int index, generation;
    private String activeUtterance = "", chapterId = "", title = "", voiceName = "";
    private float rate = 1f;
    private Runnable sleepTask;

    private final AudioManager.OnAudioFocusChangeListener focusListener = change -> main.post(() -> {
        if (change == AudioManager.AUDIOFOCUS_LOSS || change == AudioManager.AUDIOFOCUS_LOSS_TRANSIENT) {
            pausedByFocus = playing;
            pauseReading();
        } else if (change == AudioManager.AUDIOFOCUS_LOSS_TRANSIENT_CAN_DUCK) {
            pausedByFocus = playing;
            pauseReading();
        } else if (change == AudioManager.AUDIOFOCUS_GAIN && pausedByFocus) {
            pausedByFocus = false;
            resumeReading();
        }
    });

    @Override public void onCreate() {
        super.onCreate();
        running = true;
        audioManager = (AudioManager) getSystemService(AUDIO_SERVICE);
        ((NotificationManager) getSystemService(NOTIFICATION_SERVICE)).createNotificationChannel(
            new NotificationChannel(CHANNEL, "Đọc truyện", NotificationManager.IMPORTANCE_LOW));
        session = new MediaSession(this, "LeoTruyenNativeTts");
        session.setCallback(new MediaSession.Callback() {
            @Override public void onPlay() { resumeReading(); }
            @Override public void onPause() { pauseReading(); }
            @Override public void onStop() { stopReading(""); }
            @Override public void onSkipToNext() { skip(1); }
            @Override public void onSkipToPrevious() { skip(-1); }
        }, main);
        tts = new TextToSpeech(this, result -> main.post(() -> {
            ready = result == TextToSpeech.SUCCESS;
            if (!ready) { stopReading("Không khởi tạo được Android TTS."); return; }
            tts.setOnUtteranceProgressListener(new UtteranceProgressListener() {
                @Override public void onStart(String utteranceId) { }
                @Override public void onDone(String utteranceId) { main.post(() -> completed(utteranceId)); }
                @Override public void onError(String utteranceId) { main.post(() -> failed(utteranceId, "Giọng đọc không tạo được âm thanh.")); }
                @Override public void onError(String utteranceId, int code) { main.post(() -> failed(utteranceId, code == TextToSpeech.ERROR_NOT_INSTALLED_YET ? "Giọng đọc chưa tải dữ liệu. Hãy cài giọng vi-VN offline." : "Android TTS lỗi " + code)); }
            });
            if (foreground && !chunks.isEmpty() && !paused) playCurrent();
        }));
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent == null || intent.getAction() == null) return START_NOT_STICKY;
        switch (intent.getAction()) {
            case START: startReading(intent); break;
            case PAUSE: pauseReading(); break;
            case RESUME: resumeReading(); break;
            case STOP: stopReading(""); break;
            case PREVIOUS: skip(-1); break;
            case NEXT: skip(1); break;
            case SEEK:
                if (foreground && !chunks.isEmpty()) {
                    activeUtterance = ""; generation++; tts.stop();
                    index = Math.max(0, Math.min(chunks.size() - 1, intent.getIntExtra("index", 0)));
                    if (playing) playCurrent(); else publish("");
                }
                break;
            case SET_VOICE:
                voiceName = intent.getStringExtra("voice");
                if (playing) playCurrent(); else publish("");
                break;
            case SET_RATE:
                rate = (float) Math.max(.5, Math.min(2, intent.getDoubleExtra("rate", 1)));
                if (playing) playCurrent(); else publish("");
                break;
            case SET_SLEEP: scheduleSleep(intent.getLongExtra("sleepUntil", 0)); break;
            default: break;
        }
        return START_NOT_STICKY;
    }

    private void startReading(Intent intent) {
        try {
            File file = new File(intent.getStringExtra("queuePath"));
            if (!file.getCanonicalPath().startsWith(getCacheDir().getCanonicalPath() + File.separator) || file.length() > 2_000_000) throw new IllegalArgumentException("Hàng đợi không hợp lệ.");
            JSONObject payload = new JSONObject(new String(Files.readAllBytes(file.toPath()), StandardCharsets.UTF_8));
            file.delete();
            JSONArray rows = payload.getJSONArray("chunks");
            if (rows.length() == 0 || rows.length() > 1000) throw new IllegalArgumentException("Chương không có đoạn đọc.");
            activeUtterance = ""; generation++;
            if (tts != null) tts.stop();
            chunks.clear();
            for (int i = 0; i < rows.length(); i++) {
                String text = rows.getString(i);
                if (text.isBlank() || text.length() > 3000) throw new IllegalArgumentException("Đoạn đọc không hợp lệ.");
                chunks.add(text);
            }
            chapterId = payload.optString("chapterId");
            title = payload.optString("title", "Chương đang đọc");
            index = Math.min(chunks.size() - 1, Math.max(0, intent.getIntExtra("startIndex", 0)));
            voiceName = intent.getStringExtra("voice");
            if (voiceName == null || voiceName.isEmpty()) voiceName = getSharedPreferences("native-tts", 0).getString("voice", "");
            rate = (float) Math.max(.5, Math.min(2, intent.getDoubleExtra("rate", getSharedPreferences("native-tts", 0).getFloat("rate", 1f))));
            scheduleSleep(intent.getLongExtra("sleepUntil", 0));
            paused = false; playing = false; pausedByFocus = false;
            session.setMetadata(new MediaMetadata.Builder().putString(MediaMetadata.METADATA_KEY_TITLE, title).putString(MediaMetadata.METADATA_KEY_ARTIST, "Leo Truyện").build());
            session.setActive(true);
            foreground = true;
            if (Build.VERSION.SDK_INT >= 29) startForeground(NOTIFICATION, notification(), ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK);
            else startForeground(NOTIFICATION, notification());
            publish("");
            if (ready) playCurrent();
        } catch (Exception error) {
            stopReading(error.getMessage() == null ? "Không đọc được hàng đợi TTS." : error.getMessage());
        }
    }

    private Voice selectedVoice() {
        if (!ready || tts.getVoices() == null) return null;
        Voice fallback = null;
        for (Voice voice : tts.getVoices()) {
            if (!"vi".equals(voice.getLocale().getLanguage()) || voice.isNetworkConnectionRequired()
                || voice.getFeatures().contains(TextToSpeech.Engine.KEY_FEATURE_NOT_INSTALLED)) continue;
            if (voice.getName().equals(voiceName)) return voice;
            if (fallback == null || "VN".equals(voice.getLocale().getCountry())) fallback = voice;
        }
        return voiceName == null || voiceName.isEmpty() ? fallback : null;
    }

    private void playCurrent() {
        if (!ready || !foreground || chunks.isEmpty()) return;
        Voice voice = selectedVoice();
        if (voice == null) { stopReading("Chưa có giọng vi-VN offline đã cài. Hãy tải dữ liệu giọng đọc."); return; }
        if (!requestFocus()) { stopReading("Không lấy được quyền phát âm thanh."); return; }
        if (tts.setVoice(voice) != TextToSpeech.SUCCESS || tts.setSpeechRate(rate) != TextToSpeech.SUCCESS) {
            stopReading("Không sử dụng được giọng đọc đã chọn."); return;
        }
        voiceName = voice.getName();
        activeUtterance = ++generation + ":" + index;
        playing = true; paused = false;
        publish("");
        if (tts.speak(chunks.get(index), TextToSpeech.QUEUE_FLUSH, null, activeUtterance) != TextToSpeech.SUCCESS)
            stopReading("Android TTS từ chối đoạn đọc.");
    }

    private void completed(String utteranceId) {
        if (!playing || !utteranceId.equals(activeUtterance)) return;
        if (++index >= chunks.size()) { stopReading(""); return; }
        playCurrent();
    }
    private void failed(String utteranceId, String message) {
        if (playing && utteranceId.equals(activeUtterance)) stopReading(message);
    }
    private void pauseReading() {
        if (!playing) return;
        activeUtterance = ""; generation++; tts.stop();
        playing = false; paused = true;
        publish("");
    }
    private void resumeReading() {
        if (paused && foreground) playCurrent(); // Android TTS resumes at the current chunk, not mid-word.
    }
    private void skip(int delta) {
        if (!foreground || chunks.isEmpty()) return;
        activeUtterance = ""; generation++; tts.stop();
        index = Math.max(0, Math.min(chunks.size() - 1, index + delta));
        if (playing) playCurrent(); else publish("");
    }
    private void stopReading(String error) {
        if (sleepTask != null) main.removeCallbacks(sleepTask);
        sleepTask = null;
        activeUtterance = ""; generation++;
        if (tts != null) tts.stop();
        playing = false; paused = false; pausedByFocus = false;
        if (session != null) session.setActive(false);
        abandonFocus();
        foreground = false;
        current = new State(error.isEmpty() ? "idle" : "error", chapterId, index, chunks.size(), voiceName, rate, error);
        stopForeground(STOP_FOREGROUND_REMOVE);
        stopSelf();
    }

    private void scheduleSleep(long until) {
        if (sleepTask != null) main.removeCallbacks(sleepTask);
        sleepTask = null;
        if (until > 0) {
            sleepTask = () -> stopReading("");
            main.postDelayed(sleepTask, Math.max(0, until - System.currentTimeMillis()));
        }
    }

    private void publish(String error) {
        current = new State(playing ? "playing" : paused ? "paused" : foreground ? "loading" : "idle", chapterId, index, chunks.size(), voiceName, rate, error);
        if (session != null) session.setPlaybackState(new PlaybackState.Builder()
            .setActions(PlaybackState.ACTION_PLAY | PlaybackState.ACTION_PAUSE | PlaybackState.ACTION_PLAY_PAUSE | PlaybackState.ACTION_STOP | PlaybackState.ACTION_SKIP_TO_NEXT | PlaybackState.ACTION_SKIP_TO_PREVIOUS)
            .setState(playing ? PlaybackState.STATE_PLAYING : paused ? PlaybackState.STATE_PAUSED : PlaybackState.STATE_BUFFERING, index, rate).build());
        if (foreground) ((NotificationManager) getSystemService(NOTIFICATION_SERVICE)).notify(NOTIFICATION, notification());
    }
    private boolean requestFocus() {
        AudioAttributes attributes = new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_MEDIA).setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build();
        if (Build.VERSION.SDK_INT >= 26) {
            if (focusRequest == null) focusRequest = new AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN).setAudioAttributes(attributes).setOnAudioFocusChangeListener(focusListener, main).build();
            return audioManager.requestAudioFocus(focusRequest) == AudioManager.AUDIOFOCUS_REQUEST_GRANTED;
        }
        return audioManager.requestAudioFocus(focusListener, AudioManager.STREAM_MUSIC, AudioManager.AUDIOFOCUS_GAIN) == AudioManager.AUDIOFOCUS_REQUEST_GRANTED;
    }
    private void abandonFocus() {
        if (Build.VERSION.SDK_INT >= 26 && focusRequest != null) audioManager.abandonAudioFocusRequest(focusRequest);
        else audioManager.abandonAudioFocus(focusListener);
    }
    private PendingIntent control(String action, int code) {
        Intent intent = new Intent(this, NativeTtsService.class).setAction(action);
        return PendingIntent.getService(this, code, intent, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }
    private Notification notification() {
        PendingIntent open = PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class), PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Builder(this, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_media_play)
            .setContentTitle(title.isEmpty() ? "Leo Truyện" : title)
            .setContentText(playing ? "Đang đọc · " + (index + 1) + "/" + chunks.size() : paused ? "Đã tạm dừng" : "Đang chuẩn bị giọng đọc")
            .setContentIntent(open).setVisibility(Notification.VISIBILITY_PUBLIC).setOngoing(playing)
            .addAction(new Notification.Action.Builder(android.R.drawable.ic_media_previous, "Trước", control(PREVIOUS, 1)).build())
            .addAction(new Notification.Action.Builder(playing ? android.R.drawable.ic_media_pause : android.R.drawable.ic_media_play, playing ? "Tạm dừng" : "Phát", control(playing ? PAUSE : RESUME, 2)).build())
            .addAction(new Notification.Action.Builder(android.R.drawable.ic_media_next, "Tiếp", control(NEXT, 3)).build())
            .addAction(new Notification.Action.Builder(android.R.drawable.ic_menu_close_clear_cancel, "Dừng", control(STOP, 4)).build())
            .setStyle(new Notification.MediaStyle().setMediaSession(session.getSessionToken()).setShowActionsInCompactView(0, 1, 2))
            .build();
    }

    @Override public void onDestroy() {
        running = false;
        activeUtterance = "";
        if (tts != null) { tts.stop(); tts.shutdown(); }
        abandonFocus();
        if (session != null) session.release();
        if (!current.status.equals("error"))
            current = new State("idle", chapterId, index, chunks.size(), voiceName, rate, "");
        super.onDestroy();
    }
    @Override public IBinder onBind(Intent intent) { return null; }
}
