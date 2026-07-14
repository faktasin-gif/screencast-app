[app]
title = ScreenCast
package.name = screencast
package.domain = org.example
source.dir = .
source.include_exts = py,png,jpg,kv,atlas
version = 0.1
requirements = python3,kivy,pyjnius
orientation = portrait
fullscreen = 0

android.permissions = INTERNET,FOREGROUND_SERVICE
android.api = 31
android.minapi = 21
android.ndk = 25b
android.archs = arm64-v8a
android.allow_backup = True

[buildozer]
log_level = 2
warn_on_root = 1
