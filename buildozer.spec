[app]

title = ScreenCast Pro
package.name = screencastpro
package.domain = com.example.screencastpro
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,ttf
version = 1.0.0
requirements = python3,kivy,pyjnius,android
orientation = portrait
fullscreen = 0

android.permissions = INTERNET,ACCESS_NETWORK_STATE,ACCESS_WIFI_STATE,RECORD_AUDIO,WRITE_EXTERNAL_STORAGE,FOREGROUND_SERVICE,WAKE_LOCK
android.api = 33
android.minapi = 21
android.sdk = 33
android.ndk = 25b
android.archs = arm64-v8a, armeabi-v7a
android.accept_sdk_license = True
android.debug_artifact = apk
android.gradle_dependencies = com.android.support:support-compat:28.0.0

[buildozer]

log_level = 2
warn_on_root = 1
build_dir = ./.buildozer
bin_dir = ./bin

# (str) Path to build output (i.e. .apk, .aab, .ipa) storage
# bin_dir = ./bin
