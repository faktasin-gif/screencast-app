"""
ScreenCast Pro — продвинутое Android-приложение для трансляции экрана
по локальной Wi-Fi сети.

Расширенные возможности:
  • Автообнаружение устройств (UDP broadcast)
  • Настройка качества, FPS, разрешения
  • Heartbeat / Ping / Latency
  • Touch Relay — управление отправителем с приёмника
  • Запись трансляции на приёмнике
  • Статистика в реальном времени
  • Автоматическое переподключение
  • Foreground service + wake lock
  • Material-style UI с тёмной темой

Требования buildozer.spec:
  requirements = python3,kivy,pyjnius,android
  android.permissions = INTERNET,ACCESS_NETWORK_STATE,ACCESS_WIFI_STATE,
                        RECORD_AUDIO,WRITE_EXTERNAL_STORAGE,FOREGROUND_SERVICE,
                        WAKE_LOCK
  android.api = 33
  android.minapi = 21
"""

import io
import json
import os
import socket
import struct
import threading
import time
from collections import deque
from datetime import datetime

from kivy.app import App
from kivy.clock import Clock
from kivy.core.image import Image as CoreImage
from kivy.core.window import Window
from kivy.graphics import Color, Rectangle
from kivy.properties import BooleanProperty, StringProperty
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.checkbox import CheckBox
from kivy.uix.gridlayout import GridLayout
from kivy.uix.image import Image as KivyImage
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.progressbar import ProgressBar
from kivy.uix.screenmanager import Screen, ScreenManager, SlideTransition
from kivy.uix.scrollview import ScrollView
from kivy.uix.slider import Slider
from kivy.uix.textinput import TextInput
from kivy.uix.widget import Widget

# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------
PORT_VIDEO = 8765
PORT_DISCOVERY = 8766
PORT_TOUCH = 8767
PORT_HEARTBEAT = 8768

DISCOVERY_MAGIC = b"SCPRO\\x00"
HEARTBEAT_INTERVAL = 2.0
RECONNECT_BASE_DELAY = 1.0
MAX_RECONNECT_DELAY = 16.0

SETTINGS_PATH = "/sdcard/ScreenCastPro/settings.json"
RECORD_DIR = "/sdcard/ScreenCastPro/records"

# ---------------------------------------------------------------------------
# Android / JNIUS
# ---------------------------------------------------------------------------
IS_ANDROID = True
try:
    from jnius import autoclass, cast
    from android import activity as android_activity
    from android import mActivity
    PythonActivity = autoclass("org.kivy.android.PythonActivity")
except Exception as _e:
    IS_ANDROID = False
    print("[WARN] Не Android-окружение:", _e)

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
class Settings:
    """Singleton: загружает/сохраняет настройки в JSON."""
    _inst = None

    def __new__(cls):
        if cls._inst is None:
            cls._inst = super().__new__(cls)
            cls._inst.load()
        return cls._inst

    defaults = {
        "jpeg_quality": 40,
        "target_fps": 12,
        "max_capture_side": 720,
        "enable_touch": True,
        "enable_record": False,
        "enable_audio": False,
        "dark_theme": True,
    }

    def load(self):
        try:
            with open(SETTINGS_PATH, "r") as f:
                self.data = json.load(f)
        except Exception:
            self.data = dict(self.defaults)

    def save(self):
        try:
            os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
            with open(SETTINGS_PATH, "w") as f:
                json.dump(self.data, f, indent=2)
        except Exception as e:
            print("[Settings] save error:", e)

    def get(self, key):
        return self.data.get(key, self.defaults.get(key))

    def set(self, key, value):
        self.data[key] = value
        self.save()


# ---------------------------------------------------------------------------
# Discovery Protocol (UDP broadcast)
# ---------------------------------------------------------------------------
class DiscoveryProtocol:
    """
    Приёмник рассылает UDP broadcast с информацией о себе.
    Отправитель слушает и строит список доступных приёмников.
    """

    def __init__(self, role, on_device=None):
        self.role = role          # "receiver" или "sender"
        self.on_device = on_device
        self._sock = None
        self._running = False
        self._thread = None
        self.devices = {}         # ip -> {"ip", "last_seen"}
        self._lock = threading.Lock()

    def start(self):
        self._running = True
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.settimeout(1.0)
        self._sock.bind(("0.0.0.0", PORT_DISCOVERY))
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while self._running:
            try:
                data, addr = self._sock.recvfrom(256)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data.startswith(DISCOVERY_MAGIC):
                continue
            ip = addr[0]
            with self._lock:
                self.devices[ip] = {"ip": ip, "last_seen": time.time()}
            if self.on_device:
                Clock.schedule_once(lambda dt, ip=ip: self.on_device(ip))

    def broadcast_self(self, ip):
        """Приёмник вызывает это, чтобы анонсировать себя."""
        if not self._sock:
            return
        try:
            msg = DISCOVERY_MAGIC + ip.encode()
            self._sock.sendto(msg, ("<broadcast>", PORT_DISCOVERY))
        except Exception as e:
            print("[Discovery] broadcast error:", e)

    def get_devices(self):
        with self._lock:
            now = time.time()
            stale = [ip for ip, d in self.devices.items() if now - d["last_seen"] > 10]
            for ip in stale:
                del self.devices[ip]
            return list(self.devices.values())

    def stop(self):
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Захват экрана (Android MediaProjection)
# ---------------------------------------------------------------------------
class ScreenCapture:
    REQUEST_CODE = 4242

    def __init__(self, on_ready=None):
        self.on_ready = on_ready
        self.media_projection = None
        self.virtual_display = None
        self.image_reader = None
        self.cap_width = 0
        self.cap_height = 0
        self.density = 0
        self._running = False
        self.settings = Settings()

    def request_permission(self):
        if not IS_ANDROID:
            if self.on_ready:
                self.on_ready(False)
            return
        Context = autoclass("android.content.Context")
        MediaProjectionManager = autoclass("android.media.projection.MediaProjectionManager")
        mpm = mActivity.getSystemService(Context.MEDIA_PROJECTION_SERVICE)
        mpm = cast("android.media.projection.MediaProjectionManager", mpm)
        intent = mpm.createScreenCaptureIntent()

        def on_activity_result(request_code, result_code, data):
            if request_code != self.REQUEST_CODE:
                return
            Activity = autoclass("android.app.Activity")
            if result_code != Activity.RESULT_OK:
                if self.on_ready:
                    self.on_ready(False)
                return
            try:
                self._start_projection(mpm, result_code, data)
            except Exception as e:
                print("[ScreenCapture] error:", e)
                if self.on_ready:
                    self.on_ready(False)

        android_activity.bind(on_activity_result=on_activity_result)
        mActivity.startActivityForResult(intent, self.REQUEST_CODE)

    def _start_projection(self, mpm, result_code, data):
        DisplayMetrics = autoclass("android.util.DisplayMetrics")
        ImageReader = autoclass("android.media.ImageReader")
        PixelFormat = autoclass("android.graphics.PixelFormat")
        DisplayManager = autoclass("android.hardware.display.DisplayManager")

        metrics = DisplayMetrics()
        mActivity.getWindowManager().getDefaultDisplay().getRealMetrics(metrics)
        real_w, real_h = metrics.widthPixels, metrics.heightPixels
        self.density = metrics.densityDpi

        max_side = self.settings.get("max_capture_side")
        scale = min(1.0, max_side / float(max(real_w, real_h)))
        self.cap_width = max(2, int(real_w * scale)) & ~1
        self.cap_height = max(2, int(real_h * scale)) & ~1

        self.image_reader = ImageReader.newInstance(
            self.cap_width, self.cap_height, PixelFormat.RGBA_8888, 2
        )
        self.media_projection = mpm.getMediaProjection(result_code, data)
        flags = DisplayManager.VIRTUAL_DISPLAY_FLAG_PUBLIC
        self.virtual_display = self.media_projection.createVirtualDisplay(
            "ScreenCastPro",
            self.cap_width, self.cap_height, self.density,
            flags,
            self.image_reader.getSurface(),
            None, None,
        )
        self._running = True
        if self.on_ready:
            self.on_ready(True)

    def grab_jpeg(self):
        if not self._running or self.image_reader is None:
            return None
        Bitmap = autoclass("android.graphics.Bitmap")
        BitmapConfig = autoclass("android.graphics.Bitmap$Config")
        ByteArrayOutputStream = autoclass("java.io.ByteArrayOutputStream")
        CompressFormat = autoclass("android.graphics.Bitmap$CompressFormat")

        image = self.image_reader.acquireLatestImage()
        if image is None:
            return None
        try:
            plane = image.getPlanes()[0]
            buffer = plane.getBuffer()
            pixel_stride = plane.getPixelStride()
            row_stride = plane.getRowStride()
            row_padding = row_stride - pixel_stride * self.cap_width
            padded_width = self.cap_width + row_padding // pixel_stride

            bitmap = Bitmap.createBitmap(padded_width, self.cap_height, BitmapConfig.ARGB_8888)
            bitmap.copyPixelsFromBuffer(buffer)
            if row_padding != 0:
                bitmap = Bitmap.createBitmap(bitmap, 0, 0, self.cap_width, self.cap_height)

            stream = ByteArrayOutputStream()
            quality = self.settings.get("jpeg_quality")
            bitmap.compress(CompressFormat.JPEG, quality, stream)
            return bytes(stream.toByteArray())
        finally:
            image.close()

    def stop(self):
        self._running = False
        try:
            if self.virtual_display:
                self.virtual_display.release()
            if self.media_projection:
                self.media_projection.stop()
            if self.image_reader:
                self.image_reader.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Touch Relay
# ---------------------------------------------------------------------------
class TouchRelayServer:
    """Приёмник: слушает TCP на PORT_TOUCH, получает (x,y,action) и инжектит."""

    def __init__(self):
        self._sock = None
        self._running = False

    def start(self):
        self._running = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("0.0.0.0", PORT_TOUCH))
        self._sock.listen(1)
        self._sock.settimeout(1.0)
        while self._running:
            try:
                conn, _ = self._sock.accept()
                threading.Thread(target=self._handle, args=(conn,), daemon=True).start()
            except socket.timeout:
                continue
            except OSError:
                break

    def _handle(self, conn):
        conn.settimeout(5.0)
        try:
            while self._running:
                data = conn.recv(12)
                if len(data) < 12:
                    break
                x, y, action = struct.unpack(">III", data)
                self._inject_touch(x, y, action)
        except Exception as e:
            print("[TouchRelay] handle error:", e)
        finally:
            conn.close()

    def _inject_touch(self, x, y, action):
        if not IS_ANDROID:
            return
        try:
            Runtime = autoclass("java.lang.Runtime")
            cmd = "input tap {} {}".format(x, y)
            Runtime.getRuntime().exec(cmd.split())
        except Exception as e:
            print("[TouchRelay] inject error:", e)

    def stop(self):
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass


class TouchRelayClient:
    """Отправитель: подключается к приёмнику и шлёт координаты касаний."""

    def __init__(self, host):
        self.host = host
        self._sock = None
        self._lock = threading.Lock()

    def connect(self):
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.settimeout(3.0)
            self._sock.connect((self.host, PORT_TOUCH))
            self._sock.settimeout(None)
            return True
        except Exception as e:
            print("[TouchRelayClient] connect error:", e)
            self._sock = None
            return False

    def send(self, x, y, action=0):
        if self._sock is None:
            return
        try:
            with self._lock:
                self._sock.sendall(struct.pack(">III", int(x), int(y), action))
        except Exception:
            self._sock = None

    def disconnect(self):
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


# ---------------------------------------------------------------------------
# Heartbeat / Latency
# ---------------------------------------------------------------------------
class HeartbeatMixin:
    """Миксин для измерения RTT и детекта разрыва."""

    def __init__(self):
        self._last_ping_time = 0
        self._latency_ms = 0
        self._missed_pings = 0
        self._hb_sock = None
        self._hb_running = False

    def start_heartbeat(self, sock):
        self._hb_sock = sock
        self._hb_running = True
        threading.Thread(target=self._hb_loop, daemon=True).start()

    def _hb_loop(self):
        while self._hb_running and self._hb_sock:
            try:
                ping_data = struct.pack(">d", time.time())
                self._hb_sock.sendall(b"PING" + ping_data)
                self._last_ping_time = time.time()
            except Exception:
                break
            time.sleep(HEARTBEAT_INTERVAL)

    def handle_pong(self, timestamp):
        self._latency_ms = int((time.time() - timestamp) * 1000)
        self._missed_pings = 0

    @property
    def latency(self):
        return self._latency_ms


# ---------------------------------------------------------------------------
# Статистика
# ---------------------------------------------------------------------------
class StatsCollector:
    def __init__(self):
        self.reset()

    def reset(self):
        self.frames_received = 0
        self.frames_sent = 0
        self.bytes_received = 0
        self.bytes_sent = 0
        self.fps = 0.0
        self.bitrate = 0.0
        self._timestamps = deque(maxlen=30)
        self._bytes_window = deque(maxlen=30)
        self._lock = threading.Lock()

    def on_frame(self, size_bytes, direction="rx"):
        now = time.time()
        with self._lock:
            if direction == "rx":
                self.frames_received += 1
                self.bytes_received += size_bytes
            else:
                self.frames_sent += 1
                self.bytes_sent += size_bytes
            self._timestamps.append(now)
            self._bytes_window.append((now, size_bytes))
            self._recalc()

    def _recalc(self):
        now = time.time()
        # FPS по последним 30 кадрам
        recent = [t for t in self._timestamps if now - t < 1.0]
        self.fps = len(recent)
        # Битрейт (kbps)
        recent_bytes = sum(b for t, b in self._bytes_window if now - t < 1.0)
        self.bitrate = (recent_bytes * 8) / 1024.0

    def get_summary(self):
        with self._lock:
            return {
                "fps": self.fps,
                "bitrate": round(self.bitrate, 1),
                "frames": self.frames_received or self.frames_sent,
            }


# ---------------------------------------------------------------------------
# Сеть: приёмник
# ---------------------------------------------------------------------------
class ReceiverServer:
    def __init__(self, port, on_frame, stats, on_disconnect=None):
        self.port = port
        self.on_frame = on_frame
        self.stats = stats
        self.on_disconnect = on_disconnect
        self._sock = None
        self._running = False
        self._conn = None
        self._conn_lock = threading.Lock()

    def start(self):
        self._running = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("0.0.0.0", self.port))
        self._sock.listen(1)
        self._sock.settimeout(1.0)
        while self._running:
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._conn_lock:
                if self._conn:
                    try:
                        self._conn.close()
                    except OSError:
                        pass
                self._conn = conn
            threading.Thread(target=self._handle, args=(conn, addr), daemon=True).start()

    def _handle(self, conn, addr):
        print("[Receiver] connection from", addr)
        conn.settimeout(10.0)
        try:
            while self._running:
                header = self._recv_exact(conn, 4)
                if header is None:
                    break
                if header == b"PING":
                    ts = self._recv_exact(conn, 8)
                    if ts:
                        conn.sendall(b"PONG" + ts)
                    continue
                (length,) = struct.unpack(">I", header)
                data = self._recv_exact(conn, length)
                if data is None:
                    break
                self.stats.on_frame(len(data), "rx")
                if self.on_frame:
                    Clock.schedule_once(lambda dt, d=data: self.on_frame(d))
        except (ConnectionError, OSError) as e:
            print("[Receiver] connection error:", e)
        finally:
            conn.close()
            with self._conn_lock:
                if self._conn is conn:
                    self._conn = None
            if self.on_disconnect:
                Clock.schedule_once(lambda dt: self.on_disconnect())

    @staticmethod
    def _recv_exact(conn, n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def stop(self):
        self._running = False
        with self._conn_lock:
            if self._conn:
                try:
                    self._conn.close()
                except OSError:
                    pass
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Сеть: отправитель
# ---------------------------------------------------------------------------
class SenderClient(HeartbeatMixin):
    def __init__(self, host, port, capture, stats, on_status=None):
        super().__init__()
        self.host = host
        self.port = port
        self.capture = capture
        self.stats = stats
        self.on_status = on_status
        self._running = False
        self._sock = None
        self._thread = None
        self._reconnect_attempts = 0
        self.settings = Settings()

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while self._running:
            try:
                self._connect_and_stream()
                self._reconnect_attempts = 0
            except Exception as e:
                print("[Sender] stream error:", e)
            if not self._running:
                break
            delay = min(RECONNECT_BASE_DELAY * (2 ** self._reconnect_attempts), MAX_RECONNECT_DELAY)
            self._reconnect_attempts += 1
            self._update_status(f"Переподключение через {int(delay)}с... (попытка {self._reconnect_attempts})")
            time.sleep(delay)

    def _connect_and_stream(self):
        self._update_status(f"Подключение к {self.host}:{self.port}...")
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(5.0)
        self._sock.connect((self.host, self.port))
        self._sock.settimeout(None)
        self._update_status("Подключено. Запуск трансляции...")
        self.start_heartbeat(self._sock)

        interval = 1.0 / self.settings.get("target_fps")
        while self._running:
            t0 = time.time()
            jpeg = self.capture.grab_jpeg()
            if jpeg:
                try:
                    self._sock.sendall(struct.pack(">I", len(jpeg)) + jpeg)
                    self.stats.on_frame(len(jpeg), "tx")
                except (ConnectionError, OSError) as e:
                    print("[Sender] send error:", e)
                    break
            dt = time.time() - t0
            if dt < interval:
                time.sleep(interval - dt)

    def _update_status(self, text):
        if self.on_status:
            Clock.schedule_once(lambda dt, t=text: self.on_status(t))

    def stop(self):
        self._running = False
        self._hb_running = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Запись (приёмник)
# ---------------------------------------------------------------------------
class Recorder:
    def __init__(self):
        self._enabled = False
        self._session_dir = None
        self._counter = 0
        self._lock = threading.Lock()

    def start(self):
        self._enabled = True
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._session_dir = os.path.join(RECORD_DIR, ts)
        os.makedirs(self._session_dir, exist_ok=True)
        self._counter = 0
        print("[Recorder] started:", self._session_dir)

    def stop(self):
        self._enabled = False
        self._session_dir = None

    def write_frame(self, jpeg_bytes):
        if not self._enabled or not self._session_dir:
            return
        with self._lock:
            path = os.path.join(self._session_dir, f"frame_{self._counter:06d}.jpg")
            self._counter += 1
        try:
            with open(path, "wb") as f:
                f.write(jpeg_bytes)
        except Exception as e:
            print("[Recorder] write error:", e)


# ---------------------------------------------------------------------------
# Wake Lock / Foreground helpers
# ---------------------------------------------------------------------------
def acquire_wake_lock():
    if not IS_ANDROID:
        return
    try:
        PowerManager = autoclass("android.os.PowerManager")
        Context = autoclass("android.content.Context")
        pm = mActivity.getSystemService(Context.POWER_SERVICE)
        wake_lock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "ScreenCastPro::WakeLock")
        wake_lock.acquire()
        return wake_lock
    except Exception as e:
        print("[WakeLock] error:", e)
        return None


def release_wake_lock(wl):
    if wl:
        try:
            wl.release()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# UI: Touchable Image (для приёмника)
# ---------------------------------------------------------------------------
class TouchableImage(KivyImage):
    """KivyImage, который передаёт касания на отправителя."""

    def __init__(self, touch_client=None, **kw):
        super().__init__(**kw)
        self.touch_client = touch_client
        self._scale_x = 1.0
        self._scale_y = 1.0

    def update_scale(self, src_w, src_h):
        """src_w/h — разрешение видео; вычисляем масштаб до виджета."""
        if self.width and self.height and src_w and src_h:
            self._scale_x = src_w / self.width
            self._scale_y = src_h / self.height

    def on_touch_down(self, touch):
        if not self.collide_point(*touch.pos):
            return super().on_touch_down(touch)
        self._send(touch.x, touch.y)
        return True

    def on_touch_move(self, touch):
        if not self.collide_point(*touch.pos):
            return super().on_touch_move(touch)
        self._send(touch.x, touch.y, action=1)
        return True

    def on_touch_up(self, touch):
        self._send(touch.x, touch.y, action=2)
        return True

    def _send(self, x, y, action=0):
        if self.touch_client:
            rx = int((x - self.x) * self._scale_x)
            ry = int((y - self.y) * self._scale_y)
            self.touch_client.send(rx, ry, action)


# ---------------------------------------------------------------------------
# UI Screens
# ---------------------------------------------------------------------------
class MainMenuScreen(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        box = BoxLayout(orientation="vertical", padding=20, spacing=15)
        box.add_widget(Label(text="ScreenCast Pro", font_size="32sp", bold=True, color=(0.2, 0.8, 1, 1)))
        box.add_widget(Label(text="Трансляция экрана по Wi-Fi", font_size="16sp", color=(0.7, 0.7, 0.7, 1)))

        btn_recv = Button(text="📺 Я приёмник (смотреть)", font_size="18sp", size_hint_y=None, height=70)
        btn_send = Button(text="📱 Я отправитель (транслировать)", font_size="18sp", size_hint_y=None, height=70)
        btn_set = Button(text="⚙ Настройки", font_size="18sp", size_hint_y=None, height=60)

        btn_recv.bind(on_release=lambda *_: setattr(self.manager, "current", "receiver"))
        btn_send.bind(on_release=lambda *_: setattr(self.manager, "current", "sender"))
        btn_set.bind(on_release=lambda *_: setattr(self.manager, "current", "settings"))

        box.add_widget(Widget())
        box.add_widget(btn_recv)
        box.add_widget(btn_send)
        box.add_widget(btn_set)
        box.add_widget(Widget())
        self.add_widget(box)


class ReceiverScreen(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.stats = StatsCollector()
        self.server = None
        self.discovery = None
        self.touch_server = None
        self.recorder = Recorder()
        self.image_widget = None
        self._ip = "0.0.0.0"
        self._wake_lock = None

        root = BoxLayout(orientation="vertical")
        # Top bar
        top = BoxLayout(size_hint_y=None, height=50, spacing=5)
        self.lbl_status = Label(text="Ожидание...", size_hint_x=0.6)
        self.lbl_stats = Label(text="", size_hint_x=0.4, markup=True)
        btn_back = Button(text="←", size_hint_x=None, width=60)
        btn_back.bind(on_release=self.go_back)
        top.add_widget(btn_back)
        top.add_widget(self.lbl_status)
        top.add_widget(self.lbl_stats)
        root.add_widget(top)

        # Image area
        self.img_container = BoxLayout()
        self.image_widget = TouchableImage(allow_stretch=True, keep_ratio=True)
        self.img_container.add_widget(self.image_widget)
        root.add_widget(self.img_container)

        # Bottom controls
        bot = BoxLayout(size_hint_y=None, height=60, spacing=10, padding=5)
        self.btn_record = Button(text="⏺ Запись", size_hint_x=None, width=120)
        self.btn_record.bind(on_release=self.toggle_record)
        bot.add_widget(self.btn_record)
        bot.add_widget(Label())
        root.add_widget(bot)

        self.add_widget(root)
        Clock.schedule_interval(self._update_stats_ui, 0.5)

    def on_enter(self):
        self.stats.reset()
        self._ip = self._get_wifi_ip()
        self.lbl_status.text = f"IP: {self._ip}:{PORT_VIDEO}"

        self.server = ReceiverServer(PORT_VIDEO, self._on_frame, self.stats, on_disconnect=self._on_disconnect)
        self.server.start()

        self.discovery = DiscoveryProtocol("receiver")
        self.discovery.start()
        Clock.schedule_interval(self._broadcast_ip, 2.0)

        if Settings().get("enable_touch"):
            self.touch_server = TouchRelayServer()
            self.touch_server.start()

        self._wake_lock = acquire_wake_lock()

    def on_leave(self):
        Clock.unschedule(self._broadcast_ip)
        if self.server:
            self.server.stop()
        if self.discovery:
            self.discovery.stop()
        if self.touch_server:
            self.touch_server.stop()
        if self.recorder._enabled:
            self.recorder.stop()
            self.btn_record.text = "⏺ Запись"
        release_wake_lock(self._wake_lock)

    def _get_wifi_ip(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "???"

    def _broadcast_ip(self, dt):
        if self.discovery:
            self.discovery.broadcast_self(self._ip)

    def _on_frame(self, jpeg_bytes):
        try:
            core_img = CoreImage(io.BytesIO(jpeg_bytes), ext="jpg")
            self.image_widget.texture = core_img.texture
            self.recorder.write_frame(jpeg_bytes)
        except Exception as e:
            print("[Receiver] image error:", e)

    def _on_disconnect(self):
        self.lbl_status.text = "Клиент отключился. Ожидание..."

    def _update_stats_ui(self, dt):
        s = self.stats.get_summary()
        self.lbl_stats.text = (
            f"[b]{s['fps']}[/b] FPS  "
            f"[b]{s['bitrate']}[/b] kbps"
        )

    def toggle_record(self, *a):
        if self.recorder._enabled:
            self.recorder.stop()
            self.btn_record.text = "⏺ Запись"
            self.lbl_status.text = "Запись остановлена"
        else:
            self.recorder.start()
            self.btn_record.text = "⏹ Стоп"
            self.lbl_status.text = f"Запись: {self.recorder._session_dir}"

    def go_back(self, *a):
        self.manager.current = "menu"


class SenderScreen(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.capture = None
        self.client = None
        self.discovery = None
        self.touch_client = None
        self.stats = StatsCollector()
        self._wake_lock = None

        root = BoxLayout(orientation="vertical", padding=15, spacing=10)
        top = BoxLayout(size_hint_y=None, height=50)
        btn_back = Button(text="←", size_hint_x=None, width=60)
        btn_back.bind(on_release=self.go_back)
        top.add_widget(btn_back)
        top.add_widget(Label(text="Режим отправителя", font_size="18sp"))
        root.add_widget(top)

        # Device list
        root.add_widget(Label(text="Найденные приёмники:", size_hint_y=None, height=30, halign="left"))
        self.device_list = BoxLayout(orientation="vertical", size_hint_y=None, height=200)
        self.device_list.bind(minimum_height=self.device_list.setter("height"))
        scroll = ScrollView()
        scroll.add_widget(self.device_list)
        root.add_widget(scroll)

        # Manual IP
        root.add_widget(Label(text="Или введите IP вручную:", size_hint_y=None, height=30))
        self.ip_input = TextInput(hint_text="192.168.1.x", multiline=False, size_hint_y=None, height=50)
        root.add_widget(self.ip_input)

        # Status & stats
        self.lbl_status = Label(text="", size_hint_y=None, height=40, markup=True)
        self.lbl_stats = Label(text="", size_hint_y=None, height=30, markup=True)
        root.add_widget(self.lbl_status)
        root.add_widget(self.lbl_stats)

        self.btn_start = Button(text="▶ Начать трансляцию", size_hint_y=None, height=60)
        self.btn_start.bind(on_release=self.start_sending)
        root.add_widget(self.btn_start)

        self.add_widget(root)
        Clock.schedule_interval(self._update_stats_ui, 0.5)
        Clock.schedule_interval(self._refresh_devices, 2.0)

    def on_enter(self):
        self.stats.reset()
        self.discovery = DiscoveryProtocol("sender", on_device=self._add_device)
        self.discovery.start()

    def on_leave(self):
        self._stop_all()
        if self.discovery:
            self.discovery.stop()
        release_wake_lock(self._wake_lock)

    def _refresh_devices(self, dt):
        # Удаляем устаревшие кнопки
        for w in list(self.device_list.children):
            if hasattr(w, "_device_ip"):
                self.device_list.remove_widget(w)
        for dev in self.discovery.get_devices():
            self._add_device(dev["ip"])

    def _add_device(self, ip):
        for w in self.device_list.children:
            if hasattr(w, "_device_ip") and w._device_ip == ip:
                return
        btn = Button(text=f"📺 {ip}", size_hint_y=None, height=50)
        btn._device_ip = ip
        btn.bind(on_release=lambda inst: self._set_ip(inst._device_ip))
        self.device_list.add_widget(btn)

    def _set_ip(self, ip):
        self.ip_input.text = ip

    def start_sending(self, *a):
        if not IS_ANDROID:
            self.lbl_status.text = "[color=ff4444]Только Android![/color]"
            return
        host = self.ip_input.text.strip()
        if not host:
            self.lbl_status.text = "[color=ff4444]Введите IP[/color]"
            return
        self.btn_start.disabled = True
        self.lbl_status.text = "Запрос разрешения на запись экрана..."
        self.capture = ScreenCapture(on_ready=lambda ok: Clock.schedule_once(
            lambda dt: self._on_capture_ready(ok, host), 0
        ))
        self.capture.request_permission()

    def _on_capture_ready(self, ok, host):
        if not ok:
            self.lbl_status.text = "[color=ff4444]Разрешение не получено[/color]"
            self.btn_start.disabled = False
            return
        self.lbl_status.text = f"[color=44ff44]Трансляция на {host}...[/color]"
        self.client = SenderClient(host, PORT_VIDEO, self.capture, self.stats, on_status=self._on_client_status)
        self.client.start()

        if Settings().get("enable_touch"):
            self.touch_client = TouchRelayClient(host)
            threading.Thread(target=self._connect_touch, daemon=True).start()

        self._wake_lock = acquire_wake_lock()

    def _connect_touch(self):
        if self.touch_client:
            if self.touch_client.connect():
                print("[Touch] connected")
            else:
                print("[Touch] failed")

    def _on_client_status(self, text):
        self.lbl_status.text = text

    def _update_stats_ui(self, dt):
        s = self.stats.get_summary()
        lat = getattr(self.client, "latency", 0) if self.client else 0
        self.lbl_stats.text = (
            f"[b]{s['fps']}[/b] FPS  "
            f"[b]{s['bitrate']}[/b] kbps  "
            f"[b]{lat}[/b] ms"
        )

    def _stop_all(self):
        if self.client:
            self.client.stop()
            self.client = None
        if self.capture:
            self.capture.stop()
            self.capture = None
        if self.touch_client:
            self.touch_client.disconnect()
            self.touch_client = None
        self.btn_start.disabled = False

    def go_back(self, *a):
        self._stop_all()
        self.manager.current = "menu"


class SettingsScreen(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.settings = Settings()
        root = BoxLayout(orientation="vertical", padding=20, spacing=15)
        top = BoxLayout(size_hint_y=None, height=50)
        btn_back = Button(text="←", size_hint_x=None, width=60)
        btn_back.bind(on_release=lambda *_: setattr(self.manager, "current", "menu"))
        top.add_widget(btn_back)
        top.add_widget(Label(text="Настройки", font_size="20sp"))
        root.add_widget(top)

        scroll = ScrollView()
        grid = GridLayout(cols=2, spacing=15, size_hint_y=None)
        grid.bind(minimum_height=grid.setter("height"))

        # JPEG Quality
        grid.add_widget(Label(text="Качество JPEG", size_hint_y=None, height=40))
        self.sld_quality = Slider(min=10, max=95, value=self.settings.get("jpeg_quality"), size_hint_y=None, height=40)
        grid.add_widget(self.sld_quality)

        # FPS
        grid.add_widget(Label(text="Целевой FPS", size_hint_y=None, height=40))
        self.sld_fps = Slider(min=5, max=30, value=self.settings.get("target_fps"), size_hint_y=None, height=40)
        grid.add_widget(self.sld_fps)

        # Max side
        grid.add_widget(Label(text="Макс. сторона", size_hint_y=None, height=40))
        self.sld_res = Slider(min=360, max=1920, step=10, value=self.settings.get("max_capture_side"), size_hint_y=None, height=40)
        grid.add_widget(self.sld_res)

        # Toggles
        def add_toggle(label, key):
            grid.add_widget(Label(text=label, size_hint_y=None, height=40))
            cb = CheckBox(active=self.settings.get(key), size_hint_y=None, height=40)
            cb.key = key
            grid.add_widget(cb)
            return cb

        self.cb_touch = add_toggle("Touch Relay", "enable_touch")
        self.cb_record = add_toggle("Запись (приёмник)", "enable_record")
        self.cb_audio = add_toggle("Аудио (экспериментально)", "enable_audio")

        scroll.add_widget(grid)
        root.add_widget(scroll)

        btn_save = Button(text="💾 Сохранить", size_hint_y=None, height=60)
        btn_save.bind(on_release=self.save_settings)
        root.add_widget(btn_save)

        self.add_widget(root)

    def save_settings(self, *a):
        self.settings.set("jpeg_quality", int(self.sld_quality.value))
        self.settings.set("target_fps", int(self.sld_fps.value))
        self.settings.set("max_capture_side", int(self.sld_res.value))
        self.settings.set("enable_touch", self.cb_touch.active)
        self.settings.set("enable_record", self.cb_record.active)
        self.settings.set("enable_audio", self.cb_audio.active)
        popup = Popup(title="Сохранено", content=Label(text="Настройки применены"), size_hint=(0.6, 0.3))
        popup.open()
        Clock.schedule_once(popup.dismiss, 1.5)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
class ScreenCastProApp(App):
    def build(self):
        Window.clearcolor = (0.1, 0.1, 0.12, 1)
        sm = ScreenManager(transition=SlideTransition())
        sm.add_widget(MainMenuScreen(name="menu"))
        sm.add_widget(ReceiverScreen(name="receiver"))
        sm.add_widget(SenderScreen(name="sender"))
        sm.add_widget(SettingsScreen(name="settings"))
        return sm

    def on_stop(self):
        # Глобальная очистка
        for screen in self.root.screens:
            if hasattr(screen, "on_leave"):
                screen.on_leave()


if __name__ == "__main__":
    ScreenCastProApp().run()
