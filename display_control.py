"""Network-preserving blackout for a Surface whose monitor-off enters S0 sleep.

The overlay and minimum backlight are intentional: this mode does not claim
that the physical LCD is powered off. No SC_MONITORPOWER command is issued.
"""
import ctypes
import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

BASE = Path(__file__).resolve().parent


class DisplayControl:
    def __init__(self):
        self._off = False
        self._error = None
        self._ready = threading.Event()
        self._done = threading.Event()
        self._command_lock = threading.Lock()
        self._hwnd = None
        self._command_error = None
        self._brightness = None
        if os.name == 'nt':
            threading.Thread(target=self._listen, name='display-blackout', daemon=True).start()
        else:
            self._error = '此设备不支持 Windows 屏幕控制'
            self._ready.set()

    def status(self):
        self._ready.wait(2)
        return {'available': bool(self._hwnd) and self._error is None,
                'off': self._off, 'state': 'off' if self._off else 'on',
                'mode': 'blackout', 'brightness': self._brightness,
                'message': self._error}

    def set_off(self, off):
        if not self.status()['available']:
            raise OSError(self._error or '屏幕控制尚未就绪')
        with self._command_lock:
            if self._off == off:
                return self.status()
            self._done.clear()
            self._command_error = None
            if not self._user.PostMessageW(self._hwnd, 0x8001, int(off), 0):
                raise ctypes.WinError(ctypes.get_last_error())
            if not self._done.wait(20):
                raise OSError('屏幕控制超时')
            if self._command_error:
                raise OSError(self._command_error)
            return self.status()

    def _backlight(self, off):
        executable = str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
        result = subprocess.run([executable, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File',
                                 str(BASE / 'display-backlight.ps1'), '-Action', 'off' if off else 'on'],
                                cwd=BASE, capture_output=True, timeout=16,
                                creationflags=0x08000000)
        if result.returncode:
            raise OSError('背光调整失败')
        try:
            self._brightness = json.loads(result.stdout.decode('utf-8-sig').strip())['brightness']
        except (ValueError, KeyError):
            self._brightness = None

    def _listen(self):
        try:
            self._listen_windows()
        except Exception:
            self._error = '屏幕控制初始化失败，请重启相册服务后重试'
            self._hwnd = None
            self._ready.set()

    def _listen_windows(self):
        from ctypes import wintypes as wt
        user = ctypes.WinDLL('user32', use_last_error=True)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        gdi = ctypes.WinDLL('gdi32', use_last_error=True)
        self._user = user
        result_type = ctypes.c_ssize_t
        proc_type = ctypes.WINFUNCTYPE(result_type, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

        class WindowClass(ctypes.Structure):
            _fields_ = [('style', wt.UINT), ('proc', proc_type), ('class_extra', ctypes.c_int),
                        ('window_extra', ctypes.c_int), ('instance', wt.HINSTANCE),
                        ('icon', wt.HICON), ('cursor', wt.HANDLE), ('background', wt.HBRUSH),
                        ('menu_name', wt.LPCWSTR), ('class_name', wt.LPCWSTR)]
        user.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        user.DefWindowProcW.restype = result_type
        user.RegisterClassW.argtypes = [ctypes.POINTER(WindowClass)]
        user.RegisterClassW.restype = wt.ATOM
        user.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
        user.CreateWindowExW.restype = wt.HWND
        user.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        user.PostMessageW.restype = wt.BOOL
        user.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
        user.GetMessageW.restype = wt.BOOL
        user.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
        user.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
        user.DispatchMessageW.restype = result_type
        user.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, wt.UINT]
        user.SetWindowPos.restype = wt.BOOL
        user.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
        user.SetForegroundWindow.argtypes = [wt.HWND]
        user.SetCursor.argtypes = [wt.HANDLE]
        user.SetCursor.restype = wt.HANDLE
        user.SetThreadDpiAwarenessContext.argtypes = [wt.HANDLE]
        user.SetThreadDpiAwarenessContext.restype = wt.HANDLE
        user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        kernel.GetModuleHandleW.argtypes = [wt.LPCWSTR]
        kernel.GetModuleHandleW.restype = wt.HMODULE
        kernel.CloseHandle.argtypes = [wt.HANDLE]
        gdi.GetStockObject.argtypes = [ctypes.c_int]
        gdi.GetStockObject.restype = wt.HANDLE

        class DetailedReason(ctypes.Structure):
            _fields_ = [('module', wt.HMODULE), ('id', wt.ULONG), ('count', wt.ULONG),
                        ('strings', ctypes.POINTER(wt.LPWSTR))]
        class Reason(ctypes.Union):
            _fields_ = [('simple', wt.LPWSTR), ('detailed', DetailedReason)]
        class ReasonContext(ctypes.Structure):
            _fields_ = [('version', wt.ULONG), ('flags', wt.DWORD), ('reason', Reason)]
        kernel.PowerCreateRequest.argtypes = [ctypes.POINTER(ReasonContext)]
        kernel.PowerCreateRequest.restype = wt.HANDLE
        kernel.PowerSetRequest.argtypes = [wt.HANDLE, ctypes.c_int]
        kernel.PowerSetRequest.restype = wt.BOOL
        kernel.PowerClearRequest.argtypes = [wt.HANDLE, ctypes.c_int]
        kernel.PowerClearRequest.restype = wt.BOOL
        context = ReasonContext()
        context.version, context.flags = 0, 1
        context.reason.simple = 'PhotoFrame network-preserving blackout'
        power_handle = kernel.PowerCreateRequest(ctypes.byref(context))
        if not power_handle or power_handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        active = set()

        def release():
            for request in list(active):
                kernel.PowerClearRequest(power_handle, request)
                active.remove(request)

        def apply(off):
            try:
                if off:
                    # DisplayRequired prevents S0 entry; the LCD remains powered,
                    # but the black overlay and backlight at minimum hide content.
                    for request in (0, 1, 3):
                        if request not in active:
                            if not kernel.PowerSetRequest(power_handle, request):
                                raise ctypes.WinError(ctypes.get_last_error())
                            active.add(request)
                    if not user.SetWindowPos(self._hwnd, ctypes.c_void_p(-1),
                                             user.GetSystemMetrics(76), user.GetSystemMetrics(77),
                                             user.GetSystemMetrics(78), user.GetSystemMetrics(79), 0x40):
                        raise ctypes.WinError(ctypes.get_last_error())
                    user.SetForegroundWindow(self._hwnd)
                    self._backlight(True)
                    self._off = True
                else:
                    self._backlight(False)
                    user.ShowWindow(self._hwnd, 0)
                    self._off = False
                    release()
            except Exception:
                self._command_error = '屏幕调整失败，请稍后重试'
                if off:
                    user.ShowWindow(self._hwnd, 0)
                    self._off = False
                    release()
                    try:
                        self._backlight(False)
                    except Exception:
                        pass
            finally:
                self._done.set()

        @proc_type
        def window_proc(hwnd, message, wparam, lparam):
            if message == 0x8001:
                apply(bool(wparam))
                return 0
            if message in (0x201, 0x204, 0x100, 0x10):  # click, key, or close wakes
                if self._off:
                    apply(False)
                return 0
            if message == 0x20 and self._off:
                user.SetCursor(None)
                return 1
            return user.DefWindowProcW(hwnd, message, wparam, lparam)

        try:
            # Recover the previous brightness if the service was stopped in blackout.
            if (BASE / 'display-backlight-state.json').exists():
                self._backlight(False)
            instance = kernel.GetModuleHandleW(None)
            class_name = 'PhotoFrameBlackout_' + uuid.uuid4().hex
            window_class = WindowClass()
            window_class.proc, window_class.instance = window_proc, instance
            window_class.class_name, window_class.background = class_name, gdi.GetStockObject(4)
            if not user.RegisterClassW(ctypes.byref(window_class)):
                raise ctypes.WinError(ctypes.get_last_error())
            hwnd = user.CreateWindowExW(0x88, class_name, 'PhotoFrame blackout', 0x80000000,
                                        0, 0, 0, 0, None, None, instance, None)
            if not hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            self._hwnd = hwnd
            self._ready.set()
            message = wt.MSG()
            while user.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                user.TranslateMessage(ctypes.byref(message))
                user.DispatchMessageW(ctypes.byref(message))
        finally:
            release()
            kernel.CloseHandle(power_handle)
            self._hwnd = None
            self._ready.set()
