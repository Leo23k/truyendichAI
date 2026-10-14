# Leo Truyện Android Reader

APK chạy bằng bundle local, không redirect sang website. Khi có mạng, app gọi API để tải thư viện, mục lục và chương; khi mất mạng, IndexedDB của app hiển thị các nội dung đã lưu.

Trên trang truyện/Ebook, dùng nút `Lưu trên thiết bị` hoặc `Tải tất cả chương` để đọc khi không có mạng. Native Android TTS được đăng ký trong Capacitor để Reader offline có thể đọc nền bằng foreground service.

## Build

```powershell
npm install
npm run build
npx cap sync android
cd android
.\gradlew.bat assembleRelease
```

APK release được tạo tại `android/app/build/outputs/apk/release/app-release-unsigned.apk`.

Bản release hiện chưa ký vì project chưa có keystore phát hành. Để phát hành hoặc cài đặt như APK chính thức, cấu hình signing config bằng keystore riêng của chủ project; không dùng debug key cho bản phát hành.

## Chạy

Mở `android-app/android` bằng Android Studio hoặc cài APK đã ký trên thiết bị Android. Không cần emulator để build.

Reader Android tải chương cần đọc vào IndexedDB của app. Dữ liệu IndexedDB của website/PWA không tự chia sẻ sang app Android vì khác origin; hãy tải lại chương trong Android Reader khi cần offline.

Native TTS chỉ chọn voice tiếng Việt đã cài và không yêu cầu mạng. Nếu chưa có voice offline, nút cài dữ liệu TTS sẽ mở màn hình cài voice của Android.
