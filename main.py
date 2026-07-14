"""
ScreenCast — простое Android-приложение для трансляции экрана телефона
на другой телефон по локальной Wi-Fi сети.

Роли:
  - Приёмник (Receiver): поднимает TCP-сервер, принимает JPEG-кадры
    и показывает их на экране.
  - Отправитель (Sender): запрашивает у системы разрешение на запись
    экрана (MediaProjection), захватывает кадры и шлёт их по TCP
    на IP приёмника.

ВАЖНО (прочитай перед сборкой):
1. Оба телефона должны быть в одной Wi-Fi сети (это не интернет-звонок,
   а прямая передача по локальной сети).
2. Работает только на Android, т.к. используется системный API
   MediaProjection через pyjnius. В обычном Python на ПК роль
   отправителя работать не будет (кнопка покажет предупреждение).
3. На Android 10+ (API 29+) MediaProjection формально требует
   foreground service с типом mediaProjection. В этом каркасе
   сервис не поднимается отдельным Java-классом (это вышло бы за
   рамки "простого" приложения на чистом Kivy) — на части устройств
   и прошивок это сработает и так, на некоторых система может
   выбросить SecurityException. Если столкнёшься с этим — дай знать,
   добавим отдельный foreground-service класс через pyjnius.
"""

import io
import socket
import struct
import threading
import time

from kivy.app import App
from kivy.clock import Clock
from kivy.core.image import Image as CoreImage
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.image import Image as KivyImage
from kivy.uix.label import Label
from kivy.uix.textinput import TextInput

PORT = 8765
JPEG_QUALITY = 40
TARGET_FPS = 12
MAX_CAPTURE_SIDE = 720  # уменьшаем разрешение захвата, чтобы не грузить сеть/CPU

IS_ANDROID = True
try:
    from jnius import autoclass, cast
    from android import activity as android_activity
    from android import mActivity
except Exception:
    IS_ANDROID = False


# ---------------------------------------------------------------------------
# Захват экрана (только Android, сторона отправителя)
# ---------------------------------------------------------------------------
class ScreenCapture:
    """Обёртка над Android MediaProjection API."""

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

    def request_permission(self):
        Context = autoclass('android.content.Context')
        MediaProjectionManager = autoclass(
            'android.media.projection.MediaProjectionManager'
        )

        mpm = mActivity.getSystemService(Context.MEDIA_PROJECTION_SERVICE)
        mpm = cast('android.media.projection.MediaProjectionManager', mpm)
        intent = mpm.createScreenCaptureIntent()

        def on_activity_result(request_code, result_code, data):
            if request_code != self.REQUEST_CODE:
                return
            Activity = autoclass('android.app.Activity')
            if result_code != Activity.RESULT_OK:
                if self.on_ready:
                    self.on_ready(False)
                return
            try:
                self._start_projection(mpm, result_code, data)
            except Exception as e:
                print('ScreenCapture error:', e)
                if self.on_ready:
                    self.on_ready(False)

        android_activity.bind(on_activity_result=on_activity_result)
        mActivity.startActivityForResult(intent, self.REQUEST_CODE)

    def _start_projection(self, mpm, result_code, data):
        DisplayMetrics = autoclass('android.util.DisplayMetrics')
        ImageReader = autoclass('android.media.ImageReader')
        PixelFormat = autoclass('android.graphics.PixelFormat')
        DisplayManager = autoclass('android.hardware.display.DisplayManager')

        metrics = DisplayMetrics()
        mActivity.getWindowManager().getDefaultDisplay().getRealMetrics(metrics)
        real_w, real_h = metrics.widthPixels, metrics.heightPixels
        self.density = metrics.densityDpi

        scale = min(1.0, MAX_CAPTURE_SIDE / float(max(real_w, real_h)))
        self.cap_width = max(2, int(real_w * scale)) & ~1
        self.cap_height = max(2, int(real_h * scale)) & ~1

        self.image_reader = ImageReader.newInstance(
            self.cap_width, self.cap_height, PixelFormat.RGBA_8888, 2
        )

        self.media_projection = mpm.getMediaProjection(result_code, data)
        flags = DisplayManager.VIRTUAL_DISPLAY_FLAG_PUBLIC
        self.virtual_display = self.media_projection.createVirtualDisplay(
            'ScreenCastCapture',
            self.cap_width, self.cap_height, self.density,
            flags,
            self.image_reader.getSurface(),
            None, None,
        )
        self._running = True
        if self.on_ready:
            self.on_ready(True)

    def grab_jpeg(self):
        """Забирает последний кадр и кодирует его в JPEG (bytes) либо None."""
        if not self._running or self.image_reader is None:
            return None

        Bitmap = autoclass('android.graphics.Bitmap')
        BitmapConfig = autoclass('android.graphics.Bitmap$Config')
        ByteArrayOutputStream = autoclass('java.io.ByteArrayOutputStream')
        CompressFormat = autoclass('android.graphics.Bitmap$CompressFormat')

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

            bitmap = Bitmap.createBitmap(
                padded_width, self.cap_height, BitmapConfig.ARGB_8888
            )
            bitmap.copyPixelsFromBuffer(buffer)

            if row_padding != 0:
                bitmap = Bitmap.createBitmap(
                    bitmap, 0, 0, self.cap_width, self.cap_height
                )

            stream = ByteArrayOutputStream()
            bitmap.compress(CompressFormat.JPEG, JPEG_QUALITY, stream)
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
# Сеть: приёмник (сервер) и отправитель (клиент)
# ---------------------------------------------------------------------------
class ReceiverServer:
    def __init__(self, port, on_frame):
        self.port = port
        self.on_frame = on_frame
        self._sock = None
        self._running = False

    def start(self):
        self._running = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(('0.0.0.0', self.port))
        self._sock.listen(1)
        self._sock.settimeout(1.0)
        while self._running:
            try:
                conn, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self._handle_conn(conn)

    def _handle_conn(self, conn):
        conn.settimeout(5.0)
        try:
            while self._running:
                header = self._recv_exact(conn, 4)
                if header is None:
                    break
                (length,) = struct.unpack('>I', header)
                data = self._recv_exact(conn, length)
                if data is None:
                    break
                self.on_frame(data)
        except (ConnectionError, OSError):
            pass
        finally:
            conn.close()

    @staticmethod
    def _recv_exact(conn, n):
        buf = b''
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def stop(self):
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass


class SenderClient:
    def __init__(self, host, port, capture, fps=TARGET_FPS):
        self.host = host
        self.port = port
        self.capture = capture
        self.fps = fps
        self._running = False

    def start(self):
        self._running = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5.0)
            sock.connect((self.host, self.port))
            sock.settimeout(None)
            interval = 1.0 / self.fps
            while self._running:
                t0 = time.time()
                jpeg = self.capture.grab_jpeg()
                if jpeg:
                    sock.sendall(struct.pack('>I', len(jpeg)) + jpeg)
                dt = time.time() - t0
                if dt < interval:
                    time.sleep(interval - dt)
        except (ConnectionError, OSError) as e:
            print('SenderClient error:', e)
        finally:
            if sock:
                sock.close()

    def stop(self):
        self._running = False


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
class ScreenCastApp(App):
    def build(self):
        self.root_box = BoxLayout(orientation='vertical', padding=10, spacing=10)

        mode_box = BoxLayout(size_hint_y=None, height=60, spacing=10)
        btn_receiver = Button(text='Я приёмник\n(смотрю экран)')
        btn_sender = Button(text='Я отправитель\n(показываю свой экран)')
        btn_receiver.bind(on_release=lambda *_: self.show_receiver_ui())
        btn_sender.bind(on_release=lambda *_: self.show_sender_ui())
        mode_box.add_widget(btn_receiver)
        mode_box.add_widget(btn_sender)

        self.status_label = Label(text='Выберите режим', size_hint_y=None, height=40)
        self.content_box = BoxLayout(orientation='vertical', spacing=10)

        self.root_box.add_widget(mode_box)
        self.root_box.add_widget(self.status_label)
        self.root_box.add_widget(self.content_box)

        self.server = None
        self.client = None
        self.capture = None
        self.image_widget = None

        return self.root_box

    # ---- Приёмник ----
    def show_receiver_ui(self):
        self.content_box.clear_widgets()
        self.image_widget = KivyImage()
        info = Label(
            text=f'Жду подключения на порту {PORT}...',
            size_hint_y=None, height=30,
        )
        self.content_box.add_widget(info)
        self.content_box.add_widget(self.image_widget)

        if self.server:
            self.server.stop()
        self.server = ReceiverServer(PORT, self._on_frame_received)
        self.server.start()
        self.status_label.text = 'Режим: приёмник — узнай свой IP в настройках Wi-Fi и назови его отправителю'

    def _on_frame_received(self, jpeg_bytes):
        Clock.schedule_once(lambda dt: self._update_image(jpeg_bytes))

    def _update_image(self, jpeg_bytes):
        try:
            core_img = CoreImage(io.BytesIO(jpeg_bytes), ext='jpg')
            self.image_widget.texture = core_img.texture
        except Exception as e:
            print('image update error:', e)

    # ---- Отправитель ----
    def show_sender_ui(self):
        self.content_box.clear_widgets()
        self.ip_input = TextInput(
            hint_text='IP приёмника, например 192.168.1.50',
            multiline=False, size_hint_y=None, height=50,
        )
        start_btn = Button(text='Начать трансляцию экрана', size_hint_y=None, height=60)
        start_btn.bind(on_release=lambda *_: self.start_sending())
        self.sender_status = Label(text='')

        self.content_box.add_widget(self.ip_input)
        self.content_box.add_widget(start_btn)
        self.content_box.add_widget(self.sender_status)
        self.status_label.text = 'Режим: отправитель'

    def start_sending(self):
        if not IS_ANDROID:
            self.sender_status.text = (
                'Захват экрана работает только на Android-устройстве'
            )
            return
        host = self.ip_input.text.strip()
        if not host:
            self.sender_status.text = 'Введите IP приёмника'
            return

        self.sender_status.text = 'Запрашиваем разрешение на запись экрана...'
        self.capture = ScreenCapture(
            on_ready=lambda ok: Clock.schedule_once(
                lambda dt: self._on_capture_ready(ok, host)
            )
        )
        self.capture.request_permission()

    def _on_capture_ready(self, ok, host):
        if not ok:
            self.sender_status.text = 'Разрешение не получено или произошла ошибка'
            return
        self.sender_status.text = f'Трансляция запущена -> {host}:{PORT}'
        self.client = SenderClient(host, PORT, self.capture)
        self.client.start()

    def on_stop(self):
        if self.server:
            self.server.stop()
        if self.client:
            self.client.stop()
        if self.capture:
            self.capture.stop()


if __name__ == '__main__':
    ScreenCastApp().run()
