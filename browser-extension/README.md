# Gemini Companion for Edge/Chrome

## Install on Edge

1. Open `edge://extensions` (or `chrome://extensions` in Chrome).
2. Turn on **Developer mode**.
3. Choose **Load unpacked** and select this `browser-extension` folder.
4. Sign in to Leo Truyện in the browser as an administrator. Open the Gemini Web option on a translation form and choose a prompt.
5. Open `https://gemini.google.com/app` and sign in to Google normally if needed.
6. Return to the Leo Truyện tab, open the extension menu, and select **Kết nối tab Leo Truyện đang mở**.

The extension uses the existing Gemini tab session. It never asks for or stores a Google password. Keep the Leo Truyện tab open while Gemini Web jobs are running. The extension handles one chapter at a time and pauses when Gemini asks for a login/verification, shows a usage limit, or changes its interface.

After resolving a manual pause or waiting for a quota window, open the extension and select **Tiếp tục các job đã pause**. After an update, open `edge://extensions` and click **Reload** for Leo Truyện Gemini Companion. The first time you connect or check Gemini, Edge asks for the relevant site access; choose **Allow** and retry the action if needed.

For local development, use the site at `http://127.0.0.1:8000`, sign in as an administrator, and connect from that tab.
