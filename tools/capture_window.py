"""抓一个进程的主窗口截图 —— 用于界面确认，不依赖窗口是否被遮挡、是否有焦点。

用法：
    python tools/capture_window.py <pid> <out.png>

实现要点（每一条都是踩坑换来的，删掉任何一条都会得到「看起来正常但其实不是它」的图）：

1. **用 `PrintWindow(PW_RENDERFULLCONTENT=0x2)`**。WebView2 走 DirectComposition，
   普通 `PrintWindow` 常得到全黑；加上这个标志后不依赖窗口可见性与焦点，
   因此不受「IDE 抢前台」影响。
2. **边界必须取 `DWMWA_EXTENDED_FRAME_BOUNDS`**。`GetWindowRect` 含 DWM 的隐形
   resize 边框（本机实测四周各差 7px），按它截图会把窗口外的内容带进来。
3. **最小化窗口的 DWM 边界在 -32000 附近**（Windows 把最小化窗口放到屏幕外），
   必须先 `SW_RESTORE` 再重新取边界。
4. **抓完做颜色数自检**：≤2 种颜色即判全黑/空白，直接返回非 0 —— 一张全黑图
   如果被当成「界面没渲染」，会引出完全错误的结论。

退出码：0 成功 / 2 找不到窗口 / 3 抓取失败 / 4 疑似全黑。
"""

import ctypes
import sys
import time
from ctypes import wintypes

from PIL import Image

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
dwmapi = ctypes.windll.dwmapi

PW_RENDERFULLCONTENT = 0x00000002
DIB_RGB_COLORS = 0
DWMWA_EXTENDED_FRAME_BOUNDS = 9
SW_RESTORE = 9

EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


def find_main_window(pid):
    """找该进程可见窗口里面积最大的那个（跳过 tooltip / 隐藏窗口）。"""
    best = None

    def cb(hwnd, _lp):
        nonlocal best
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value != pid or not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        b = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, b, n + 1)
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        area = max(0, r.right - r.left) * max(0, r.bottom - r.top)
        if best is None or area > best[0]:
            best = (area, hwnd, b.value)
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return best


def visible_rect(hwnd):
    """窗口的**可见**边界（用 DWM 扩展边框，而非含隐形边框的 GetWindowRect）。"""
    r = wintypes.RECT()
    hr = dwmapi.DwmGetWindowAttribute(
        hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r)
    )
    if hr != 0:  # 失败时退回旧行为，但要说出来
        print(f"WARN: DwmGetWindowAttribute 失败 hr={hr}，退回 GetWindowRect（会多出边框）")
        user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r


def capture(hwnd, w, h):
    hdc = user32.GetWindowDC(hwnd)
    if not hdc:
        return None, "GetWindowDC 失败"
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    if not bmp:
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(hwnd, hdc)
        return None, "CreateCompatibleBitmap 失败"
    old = gdi32.SelectObject(mdc, bmp)
    ok = user32.PrintWindow(hwnd, mdc, PW_RENDERFULLCONTENT)
    gdi32.SelectObject(mdc, old)

    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth = w
    bi.biHeight = -h  # 负 = top-down
    bi.biPlanes = 1
    bi.biBitCount = 32
    bi.biCompression = 0
    buf = ctypes.create_string_buffer(w * h * 4)
    lines = gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), DIB_RGB_COLORS)

    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mdc)
    user32.ReleaseDC(hwnd, hdc)

    if lines == 0:
        return None, "GetDIBits 失败"
    img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
    return img, f"PrintWindow={ok} lines={lines}"


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    pid = int(sys.argv[1])
    out = sys.argv[2]

    found = find_main_window(pid)
    if not found:
        print(f"FAIL: 进程 {pid} 找不到可见主窗口")
        return 2
    _area, hwnd, title = found

    # 最小化时必须在恢复之后再取边界，否则读到的是屏幕外的 -32000 坐标
    if user32.IsIconic(hwnd):
        print("WARN: 窗口处于最小化，先 SW_RESTORE 再取边界")
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.4)

    r = visible_rect(hwnd)
    w, h = r.right - r.left, r.bottom - r.top
    print(f"窗口 hwnd={hwnd} title={title!r} 可见边界=({r.left},{r.top},{r.right},{r.bottom}) size={w}x{h}")
    if w <= 0 or h <= 0:
        print("FAIL: 可见边界非正，窗口可能仍未恢复")
        return 3

    img, note = capture(hwnd, w, h)
    if img is None:
        print("FAIL:", note)
        return 3
    colors = img.getcolors(maxcolors=1 << 22)
    n = len(colors) if colors else ">4M"
    print(f"{note} 颜色数={n}")
    img.save(out)
    print(f"已保存 {out} size={img.size}")
    if isinstance(n, int) and n <= 2:
        print("WARN: 颜色数 <=2，疑似全黑/空白 —— 不能用。")
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
