# Mobile React Native Standard

Semua aturan **Frontend React + TypeScript** berlaku, ditambah:

## Build check
- Build Android & iOS lulus di CI sebelum merge.
- Perubahan native (Podfile, gradle, AndroidManifest, Info.plist) disebut di deskripsi MR.

## Platform-specific code
- Kode khusus platform memakai `Platform.select` atau file `.ios.tsx`/`.android.tsx`, bukan `if` yang tersebar.

## Environment & secret
- Konfigurasi per environment (dev/staging/prod) lewat react-native-config / build flavor, bukan konstanta di kode.
- Secret tidak disimpan di bundle JS. Token user disimpan di Keychain/Keystore (secure storage), bukan AsyncStorage.

## App versioning & release
- Perubahan `versionCode`/`versionName`/`CFBundleVersion` hanya di MR release.

## Performa
- List panjang memakai `FlatList`/`FlashList`, bukan `ScrollView` + `map`.
- Hindari inline function/object berat di render list. Pakai `useCallback`/`useMemo` jika perlu.

## Lain-lain
- Tidak ada `console.log` di kode yang di-merge.
