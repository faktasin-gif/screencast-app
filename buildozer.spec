[app]
title = ScreenCast
package.name = screencast
package.domain = org.example

source.dir = .
source.include_exts = py,png,jpg,kv,atlas

version = 0.1

# Основные зависимости
requirements = kivy==2.3.0, pyjnius

# Дополнительно (часто нужно)
# requirements = kivy==2.3.0, pyjnius, setuptools

orientation = portrait
fullscreen = 0

android.permissions = INTERNET, FOREGROUND_SERVICE

# Android настройки (актуальные на 2025-2026)
android.api = 34
android.minapi = 21
android.ndk = 25b
android.archs = arm64-v8a, armeabi-v7a

android.allow_backup = True

[buildozer]
log_level = 2
warn_on_root = 1
