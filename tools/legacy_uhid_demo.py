#!/usr/bin/env python3
"""
经典蓝牙 HID 游戏手柄 —— Linux 作为 HID 设备（device/server 角色）

和 main.py（BLE + GATT HID）完全不同的一条路：
  * 不碰 D-Bus / GATT / 广播
  * 通过内核的 /dev/uhid 注册一个 HID 游戏手柄
  * BlueZ 的 input 插件（profiles/input/server.c）会把它以「经典蓝牙 HID 设备」
    暴露出去，注册标准 SDP HID 记录
  * iPhone 在经典蓝牙上直接识别为标准手柄 —— 这条路径 iOS 支持最成熟

前置条件（一次性，需要 sudo）：
    sudo modprobe uhid
    echo uhid | sudo tee /etc/modules-load.d/uhid.conf
    echo 'KERNEL=="uhid", MODE="0660", GROUP="plugdev"' | sudo tee /etc/udev/rules.d/99-uhid.rules
    sudo udevadm control --reload-rules && sudo udevadm trigger

运行：
    python3 classic_hid_gamepad.py
"""

import os
import struct
import sys
import threading
import tkinter as tk

UHID_DEV = "/dev/uhid"

# include/uapi/linux/uhid.h 的枚举（已用 gcc 实测校准，勿凭记忆修改）
# 注意：UHID_FEATURE 只是 UHID_GET_REPORT 的别名，现代枚举里没有独立项，
# 所以 CREATE2 是 11 而不是 12 —— 写错会直接被内核以 EINVAL 拒绝。
UHID_LEGACY_CREATE = 0
UHID_DESTROY = 1
UHID_START = 2
UHID_STOP = 3
UHID_OPEN = 4
UHID_CLOSE = 5
UHID_OUTPUT = 6
UHID_GET_REPORT = 9
UHID_GET_REPORT_REPLY = 10
UHID_CREATE2 = 11
UHID_INPUT2 = 12
UHID_SET_REPORT = 13
UHID_SET_REPORT_REPLY = 14

BUS_BLUETOOTH = 0x05

# sizeof(struct uhid_event)：由 union 里最大的 legacy create_req 决定，
# gcc 实测 = 4380。内核要求写入长度与之完全相等。
UHID_EVENT_SIZE = 4380


# ============================================================
# 1. HID Report Descriptor：16 按钮 + Hat switch(方向键) + 左右摇杆
#    报告长度 7 字节 = 2(按钮) + 1(方向键) + 4(四个轴)
# ============================================================
REPORT_DESCRIPTOR = bytes([
    0x05, 0x01,        # Usage Page (Generic Desktop)
    0x09, 0x05,        # Usage (Game Pad)
    0xA1, 0x01,        # Collection (Application)

    # 16 个按钮 (2 字节)
    0x05, 0x09,        #   Usage Page (Button)
    0x19, 0x01,        #   Usage Minimum (Button 1)
    0x29, 0x10,        #   Usage Maximum (Button 16)
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x01,        #   Logical Maximum (1)
    0x75, 0x01,        #   Report Size (1)
    0x95, 0x10,        #   Report Count (16)
    0x81, 0x02,        #   Input (Data,Var,Abs)

    # 方向键：标准 Hat switch
    0x05, 0x01,        #   Usage Page (Generic Desktop)
    0x09, 0x39,        #   Usage (Hat switch)
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x07,        #   Logical Maximum (7)
    0x35, 0x00,        #   Physical Minimum (0)
    0x46, 0x3B, 0x01,  #   Physical Maximum (315)
    0x65, 0x14,        #   Unit (English Rotation, Degrees)
    0x75, 0x04,        #   Report Size (4)
    0x95, 0x01,        #   Report Count (1)
    0x81, 0x42,        #   Input (Data,Var,Abs,Null State)
    0x65, 0x00,        #   Unit (None)
    0x75, 0x04,        #   Report Size (4)
    0x95, 0x01,        #   Report Count (1)
    0x81, 0x03,        #   Input (Const,Var,Abs)   -- 高位补白

    # 左摇杆 X / Y
    0x05, 0x01,
    0x09, 0x30, 0x15, 0x81, 0x25, 0x7F, 0x75, 0x08, 0x95, 0x01, 0x81, 0x02,
    0x09, 0x31, 0x15, 0x81, 0x25, 0x7F, 0x75, 0x08, 0x95, 0x01, 0x81, 0x02,
    # 右摇杆 Z / Rz
    0x09, 0x32, 0x15, 0x81, 0x25, 0x7F, 0x75, 0x08, 0x95, 0x01, 0x81, 0x02,
    0x09, 0x35, 0x15, 0x81, 0x25, 0x7F, 0x75, 0x08, 0x95, 0x01, 0x81, 0x02,

    0xC0               # End Collection
])

# UI 上第 13~16 个按钮是方向键，它们不走按钮位，改走 Hat switch
DPAD_UP, DPAD_DOWN, DPAD_LEFT, DPAD_RIGHT = 12, 13, 14, 15
BUTTON_MASK = 0x0FFF          # 只保留前 12 个按钮


class UhidGamepad:
    """通过 /dev/uhid 暴露一个经典蓝牙 HID 游戏手柄。"""

    def __init__(self):
        self._buttons = 0
        self._lx = self._ly = self._rx = self._ry = 0
        self._fd = None
        self._lock = threading.Lock()
        self._reader = None
        self._running = False
        self.started = threading.Event()

    # ---------- uhid 底层 ----------
    def open(self):
        if not os.path.exists(UHID_DEV):
            raise RuntimeError(
                f"{UHID_DEV} 不存在。请先执行：sudo modprobe uhid")
        try:
            self._fd = os.open(UHID_DEV, os.O_RDWR)
        except PermissionError:
            raise RuntimeError(
                f"没有权限打开 {UHID_DEV}。请确认已执行：\n"
                "  sudo modprobe uhid\n"
                "  echo 'KERNEL==\"uhid\", MODE=\"0660\", GROUP=\"plugdev\"' | "
                "sudo tee /etc/udev/rules.d/99-uhid.rules\n"
                "  sudo udevadm control --reload-rules && sudo udevadm trigger")
        self._write_event(UHID_CREATE2, self._create2_payload())
        self._running = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _create2_payload(self):
        buf = bytearray()
        buf += b"Linux Gamepad".ljust(128, b"\0")   # name
        buf += b"\0" * 64                            # phys
        buf += b"\0" * 64                            # uniq (留空：不要让内核当成同一个设备)
        buf += struct.pack("<HH", len(REPORT_DESCRIPTOR), BUS_BLUETOOTH)
        buf += struct.pack("<IIII", 0x1D6B, 0x0246, 0x0111, 0)  # vendor/product/version/country
        buf += REPORT_DESCRIPTOR.ljust(4096, b"\0")
        return bytes(buf)

    def _write_event(self, ev_type, payload=b""):
        if self._fd is None:
            return
        buf = struct.pack("<I", ev_type) + payload
        buf = buf.ljust(UHID_EVENT_SIZE, b"\0")[:UHID_EVENT_SIZE]
        with self._lock:
            os.write(self._fd, buf)

    def _read_loop(self):
        """必须持续读 /dev/uhid，否则内核队列会堵住。"""
        while self._running:
            try:
                data = os.read(self._fd, UHID_EVENT_SIZE)
            except OSError:
                break
            if len(data) < 4:
                continue
            ev_type = struct.unpack_from("<I", data, 0)[0]
            if ev_type == UHID_START:
                self.started.set()
            elif ev_type == UHID_GET_REPORT:
                self._reply_get_report(data)
            elif ev_type == UHID_SET_REPORT:
                self._reply_set_report(data)

    def _reply_get_report(self, data):
        # struct uhid_get_report_req { __u32 id; __u8 rnum; __u8 rtype; }
        req_id = struct.unpack_from("<I", data, 4)[0]
        report = self._build_report()
        payload = struct.pack("<IHH", req_id, 0, len(report)) + report
        self._write_event(UHID_GET_REPORT_REPLY, payload)

    def _reply_set_report(self, data):
        req_id = struct.unpack_from("<I", data, 4)[0]
        self._write_event(UHID_SET_REPORT_REPLY, struct.pack("<IH", req_id, 0))

    def close(self):
        self._running = False
        try:
            self._write_event(UHID_DESTROY)
        except Exception:
            pass
        if self._fd is not None:
            try:
                os.close(self._fd)
            except Exception:
                pass

    # ---------- 报告 ----------
    def _hat(self):
        up = bool(self._buttons & (1 << DPAD_UP))
        down = bool(self._buttons & (1 << DPAD_DOWN))
        left = bool(self._buttons & (1 << DPAD_LEFT))
        right = bool(self._buttons & (1 << DPAD_RIGHT))
        if up and right:
            return 1
        if right and down:
            return 3
        if down and left:
            return 5
        if left and up:
            return 7
        if up:
            return 0
        if right:
            return 2
        if down:
            return 4
        if left:
            return 6
        return 15                      # 15 = 松开（Null state）

    def _build_report(self):
        buttons = self._buttons & BUTTON_MASK
        hat_byte = (self._hat() & 0x0F) | 0xF0   # 高位补白
        return struct.pack("<HBbbbb", buttons, hat_byte,
                           self._lx, self._ly, self._rx, self._ry)

    def _notify(self):
        report = self._build_report()
        print(f"[HID] {report.hex()}")
        # struct uhid_input2_req { __u16 size; __u8 data[4096]; }
        self._write_event(UHID_INPUT2, struct.pack("<H", len(report)) + report)

    def set_button(self, index, pressed):
        if pressed:
            self._buttons |= (1 << index)
        else:
            self._buttons &= ~(1 << index)
        self._notify()

    def set_left_stick(self, x, y):
        self._lx = max(-127, min(127, int(x)))
        self._ly = max(-127, min(127, int(y)))
        self._notify()

    def set_right_stick(self, x, y):
        self._rx = max(-127, min(127, int(x)))
        self._ry = max(-127, min(127, int(y)))
        self._notify()


# ============================================================
# Tkinter 界面
# ============================================================
class GamepadWindow:
    BUTTONS = [
        ("A", 0), ("B", 1), ("X", 2), ("Y", 3),
        ("LB", 4), ("RB", 5), ("LT", 6), ("RT", 7),
        ("Back", 8), ("Start", 9), ("L3", 10), ("R3", 11),
        ("Up", 12), ("Down", 13), ("Left", 14), ("Right", 15),
    ]

    def __init__(self, pad: UhidGamepad):
        self.pad = pad
        self.root = tk.Tk()
        self.root.title("经典蓝牙 HID 游戏手柄")
        self.root.geometry("560x560")
        self.root.resizable(False, False)
        self._build_ui()

    def _build_ui(self):
        self.status = tk.Label(self.root, text="正在初始化 uhid...",
                               fg="orange", font=("Arial", 12))
        self.status.pack(pady=5)

        frame = tk.Frame(self.root)
        frame.pack(pady=5)
        for i, (name, index) in enumerate(self.BUTTONS):
            btn = tk.Button(frame, text=name, width=6, height=2)
            btn.grid(row=i // 4, column=i % 4, padx=3, pady=3)
            btn.bind("<ButtonPress-1>", lambda e, idx=index: self.pad.set_button(idx, True))
            btn.bind("<ButtonRelease-1>", lambda e, idx=index: self.pad.set_button(idx, False))

        sticks = tk.Frame(self.root)
        sticks.pack(pady=10)
        self.lx = self._scale(sticks, "左摇杆 X", 0, 1, self._left)
        self.ly = self._scale(sticks, "左摇杆 Y", 0, 2, self._left)
        self.rx = self._scale(sticks, "右摇杆 X", 1, 1, self._right)
        self.ry = self._scale(sticks, "右摇杆 Y", 1, 2, self._right)

    def _scale(self, parent, label, col, row, cmd):
        tk.Label(parent, text=label, font=("Arial", 10)).grid(row=row * 2 - 2,
                                                             column=col)
        s = tk.Scale(parent, from_=-127, to=127, orient=tk.HORIZONTAL,
                     length=180, command=cmd)
        s.grid(row=row * 2 - 1, column=col, padx=5)
        return s

    def _left(self, _=None):
        self.pad.set_left_stick(self.lx.get(), self.ly.get())

    def _right(self, _=None):
        self.pad.set_right_stick(self.rx.get(), self.ry.get())

    def run(self):
        self.status.config(
            text="就绪：在 iPhone 经典蓝牙里连接本机（已配对过会自动连）", fg="blue")
        self.root.mainloop()


if __name__ == "__main__":
    print("=" * 52)
    print("经典蓝牙 HID 游戏手柄")
    print("=" * 52)

    pad = UhidGamepad()
    try:
        pad.open()
    except Exception as e:
        print(f"[错误] {e}")
        sys.exit(1)

    print("[uhid] 已创建经典蓝牙 HID 手柄（bus=BUS_BLUETOOTH）")
    print("[uhid] BlueZ 的 input 插件应已注册 SDP HID 记录")
    print("       可用 `sdptool browse local` 验证")

    try:
        GamepadWindow(pad).run()
    finally:
        pad.close()
