[app]

# Название приложения
title = ScreenCast Pro

# Имя пакета (должно быть уникальным, без пробелов)
package.name = screencastpro

# Домен (обратная нотация)
package.domain = com.example.screencastpro

# Версия
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,ttf
source.include_patterns = assets/*,images/*.png

# Версия
version = 1.0.0

# Требования
requirements = python3,kivy,pyjnius,android

# Иконка (опционально)
# icon.filename = %(source.dir)s/assets/icon.png

# Ориентация
orientation = portrait

# Разрешения Android
android.permissions = INTERNET,ACCESS_NETWORK_STATE,ACCESS_WIFI_STATE,RECORD_AUDIO,WRITE_EXTERNAL_STORAGE,FOREGROUND_SERVICE,WAKE_LOCK

# API уровни
android.api = 33
android.minapi = 21
android.sdk = 33
android.ndk = 25b

# Архитектуры
android.archs = arm64-v8a, armeabi-v7a

# Включение Java-источников (если понадобится foreground service)
# android.add_src = %(source.dir)s/java/

# Необходимо для работы с JNI
android.gradle_dependencies = com.android.support:support-compat:28.0.0

# Для Kivy
fullscreen = 0
android.numeric_version = 1

[buildozer]

# Путь к логам
log_level = 2

# Директория сборки
build_dir = ./.buildozer

# Директория для bin-файлов
bin_dir = ./bin

# Используемый NDK
android.accept_sdk_license = True
