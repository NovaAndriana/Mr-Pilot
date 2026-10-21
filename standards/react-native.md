# Standar Mobile (React Native)

Semua aturan **Frontend React + TypeScript** (`react.md`) dan `general.md` berlaku, ditambah:

## Struktur & penamaan
- Wajib: folder per fitur: `mobile/src/api`, `navigation`, `components`, `features/<fitur>/{screens,components,hooks}`.
- Wajib: screen berakhiran `Screen` (`QuotaScreen.tsx`); parameter route diberi tipe di satu tempat (`RootStackParamList`).
- Wajib: kode khusus platform memakai `Platform.select` atau file `.ios.tsx` / `.android.tsx`, bukan `if (Platform.OS ...)` yang tersebar.
- Disarankan: style memakai `StyleSheet.create` / sistem styling proyek, bukan object inline besar.

## Komponen, state & performa
- Wajib: list panjang memakai `FlatList`/`FlashList` dengan `keyExtractor` dari id, bukan `ScrollView` + `map`.
- Wajib: listener, timer, dan subscription dibersihkan di cleanup `useEffect`.
- Disarankan: item list `React.memo`, callback `useCallback`, tidak membuat function/object berat di `renderItem`.
- Disarankan: gambar diberi ukuran tetap dan di-cache; file besar (PDF dokumen) tidak dimuat penuh ke memori; animasi memakai native driver / Reanimated.

## API, offline & kompatibilitas
- Wajib: semua request lewat satu API client (base URL per environment, auth, refresh token, timeout, interceptor error).
- Wajib: setiap request menangani loading, error, data kosong, dan tidak ada koneksi, dengan pesan jelas dan tombol coba lagi.
- Wajib: response diperlakukan defensif: field baru opsional, enum/status yang tidak dikenal punya tampilan default, bukan crash.
- Wajib: app versi lama di Play Store/App Store tetap dipakai; perubahan yang butuh backend baru dicek bersama BE (apa yang dilihat user di versi lama).
- Disarankan: fitur yang belum siap disembunyikan dengan feature flag / minimum app version dari backend.
- Disarankan: aksi penting (tanda tangan, pembayaran) dilindungi dari tap ganda dan request ganda.

## Keamanan & izin
- Wajib: token dan data login di Keychain/Keystore (secure storage), bukan AsyncStorage.
- Wajib: tidak ada secret di bundle JS; konfigurasi per environment lewat react-native-config / build flavor.
- Wajib: data sensitif (NIK, foto KTP, selfie, dokumen) tidak disimpan di cache/galeri lebih lama dari perlu dan dihapus saat logout.
- Wajib: permission diminta saat fiturnya dipakai, dengan penjelasan, dan ada alur kalau user menolak. Permission baru di `AndroidManifest.xml` / `Info.plist` disebut di MR beserta alasannya.

## Build & rilis
- Wajib: build Android dan iOS lulus di CI sebelum merge.
- Wajib: perubahan native (Podfile, gradle, `AndroidManifest.xml`, `Info.plist`, library dengan kode native) disebut di MR karena tidak bisa dikirim lewat OTA update.
- Wajib: `versionCode` / `versionName` / `CFBundleVersion` hanya diubah di MR release.
- Disarankan: alur utama (login, tanda tangan dokumen) punya test E2E (Detox/Maestro).
