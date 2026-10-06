#!/usr/bin/env python3
"""
经典蓝牙 HID 游戏手柄 —— 伪装成 Xbox 无线控制器

为什么是这条路（前面全部验证过）：
  * iOS 的 GameController 只认 MFi 和「经典蓝牙 HID」手柄，不认通用 BLE HID
    —— 所以 BLE 那条路（main.py）走不通。
  * BlueZ 不提供 HID 设备(device/server)角色，它自己用 HID 的 UUID 做主机。
  * 解法（bluez issue #1168）：用 --noplugin=input 让出 UUID 和 PSM，
    自己通过 ProfileManager1 发布 HID SDP 记录、自己监听 L2CAP 的两个通道。

已实测通过的前提：
  ✓ ProfileManager1.RegisterProfile 能发布带自定义描述的 HID 记录
  ✓ PSM 0x11 / 0x13 注册 profile 后仍可自己 bind（通道归我们）

运行（一次性授权后，以普通用户运行，GUI 才正常）：
    sudo setcap cap_net_bind_service=+ep /usr/bin/python3.14

    cd ~/virtual-handpiece && python3 xbox_hid_gamepad.py

  说明：PSM 0x11/0x13 小于 0x1000，属于特权端口，内核要求
  CAP_NET_BIND_SERVICE。SDP 发布（ProfileManager1）普通用户就能做，
  所以只给 python 授这一个 capability 即可，不用 sudo 也不需要动 X 授权。
  撤销：sudo setcap -r /usr/bin/python3.14
"""

import asyncio
import os
import socket
import struct
import sys
import threading
import time
import tkinter as tk

from dbus_next.aio import MessageBus
from dbus_next import BusType, Variant
from dbus_next.service import ServiceInterface, method

# ---------------- 常量 ----------------
HID_UUID = "00001124-0000-1000-8000-00805f9b34fb"
DID_UUID = "00001200-0000-1000-8000-00805f9b34fb"      # Device ID (PnP) Profile
PSM_CONTROL = 0x11
PSM_INTERRUPT = 0x13
BDADDR_ANY = "00:00:00:00:00:00"
ADAPTER = "/org/bluez/hci0"
PROFILE_PATH = "/com/spacecheese/xboxhid"
DID_PROFILE_PATH = "/com/spacecheese/xboxdid"
DEVICE_NAME = "Xbox Wireless Controller"

# Xbox One S 控制器的身份 —— iOS 的 GameController 靠这组 VID/PID 认型号。
# 依据 xpadneo issue #624 的真实 dump：
#   "Xbox One S Wireless Controller (model 1708), VID:PID 045E:02FD,
#    classic Bluetooth (BR/EDR), device version 0x0903"
# 注意：0x02FD 是 BR/EDR（经典蓝牙）的 PID；0x0B13 是 BLE 固件那侧的。
XBOX_VENDOR_ID = 0x045E        # Microsoft
XBOX_PRODUCT_ID = 0x02FD       # Xbox One S，经典蓝牙 (BR/EDR)
XBOX_VERSION = 0x0903

# HIDP 事务头：高 4 位 = 事务类型，低 4 位 = 参数
# 依据 Linux 内核 net/bluetooth/hidp/core.c：
#   #define HIDP_HEADER_TRANS_MASK 0xf0
#   #define HIDP_HEADER_PARAM_MASK 0x0f
#   #define HIDP_TRANS_DATA 0xa0
#   #define HIDP_DATA_RTYPE_INPUT 0x01
T_HANDSHAKE = 0x0
T_HID_CONTROL = 0x1
T_GET_REPORT = 0x4
T_SET_REPORT = 0x5
T_GET_PROTOCOL = 0x6
T_SET_PROTOCOL = 0x7
T_GET_IDLE = 0x8
T_SET_IDLE = 0x9
T_DATA = 0xA

# 参数定义
R_HANDSHAKE_OK = 0x00            # Successful
R_HANDSHAKE_UNSUPPORTED = 0x03   # Err Unsupported Request

HDR_HANDSHAKE_OK = (T_HANDSHAKE << 4) | R_HANDSHAKE_OK              # 0x00
HDR_HANDSHAKE_UNSUPPORTED = (T_HANDSHAKE << 4) | R_HANDSHAKE_UNSUPPORTED  # 0x03
# 输入报告固定 0xA1 = DATA(0xA0) + Input(0x01)
HDR_DATA_INPUT = (T_DATA << 4) | 0x01                              # 0xA1

# ---- 日志解码表（对照 HID Profile 1.1.1）----
TRANS_NAMES = {
    0x0: "HANDSHAKE", 0x1: "HID_CONTROL", 0x4: "GET_REPORT",
    0x5: "SET_REPORT", 0x6: "GET_PROTOCOL", 0x7: "SET_PROTOCOL",
    0x8: "GET_IDLE", 0x9: "SET_IDLE", 0xA: "DATA", 0xB: "DATC",
}
HID_CONTROL_NAMES = {
    0: "NOP", 1: "Hard Reset", 2: "Soft Reset", 3: "Suspend",
    4: "Exit Suspend", 5: "Virtual Cable Unplug",
}
REPORT_TYPE_NAMES = {0: "Other", 1: "Input", 2: "Output", 3: "Feature"}


def describe_transaction(ttype, param, payload):
    """把收到的事务翻译成人话，方便看 iOS 到底在做什么。"""
    name = TRANS_NAMES.get(ttype, f"type={ttype}")
    if ttype == T_HID_CONTROL:
        name += f" / {HID_CONTROL_NAMES.get(param, f'param={param}')}"
    elif ttype in (T_GET_REPORT, T_SET_REPORT):
        name += f" / {REPORT_TYPE_NAMES.get(param, param)}"
    elif ttype == T_SET_PROTOCOL:
        name += " / " + ("Report" if param else "Boot")
    elif ttype == T_HANDSHAKE:
        name += f" / param={param}"
    if payload:
        name += f"  payload={payload[:8].hex()}"
    return name

# ---------------- HID 报告描述符 ----------------
# 原厂字节：Xbox One S (model 1708) 经典蓝牙(045E:02FD) 的 334 字节描述符，
# 来自 xpadneo issue #624 的真实 dump（两份独立来源互相印证）。
# 之所以必须用原厂字节而不是功能等价的自制描述符：主机会比对报告描述符的
# 指纹来认定手柄型号（BlueRetro 项目明确记录了这个机制）。
_DESC_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          os.path.join("hid_descriptors", "xbox_one_s.bin"))
with open(_DESC_FILE, "rb") as _f:
    REPORT_DESCRIPTOR = _f.read()  # 见 hid_descriptors/xbox_one_s.bin
assert REPORT_DESCRIPTOR[-1] == 0xC0, "描述符结尾必须是 End Collection"

DPAD_UP, DPAD_DOWN, DPAD_LEFT, DPAD_RIGHT = 12, 13, 14, 15
BUTTON_MASK = 0x0FFF


def build_sdp_record():
    """HID 服务记录 —— 结构照搬可用的开源实现 BTGamepad 的 sdp_record_gamepad.xml。

    关键点（我最初写错过）：
      * 0x0004 ProtocolDescriptorList 只放【控制通道】L2CAP PSM 0x0011 + HIDP
      * 0x000d AdditionalProtocolDescriptorList 放【中断通道】L2CAP PSM 0x0013
        这是 HID Profile 的硬性要求，不能把两个 PSM 都塞进 0x0004
      * 0x0202 HIDDeviceSubclass = 0x08
      * 0x0009 Profile 版本 = 0x0100
      * 0x0206 描述符用 text(hex) 承载（与可用实现一致）
    """
    hexdesc = REPORT_DESCRIPTOR.hex()
    return f"""<?xml version="1.0" encoding="UTF-8" ?>
<record>
  <attribute id="0x0001"><sequence><uuid value="0x1124"/></sequence></attribute>

  <!-- 控制通道：L2CAP PSM 0x0011 -->
  <attribute id="0x0004"><sequence>
    <sequence><uuid value="0x0100"/><uint16 value="0x0011"/></sequence>
    <sequence><uuid value="0x0011"/></sequence>
  </sequence></attribute>

  <attribute id="0x0005"><sequence><uuid value="0x1002"/></sequence></attribute>
  <attribute id="0x0006"><sequence>
    <uint16 value="0x656e"/>
    <uint16 value="0x006a"/>
    <uint16 value="0x0100"/>
  </sequence></attribute>
  <attribute id="0x0009"><sequence>
    <sequence>
      <uuid value="0x1124"/>
      <uint16 value="0x0100"/>
    </sequence>
  </sequence></attribute>

  <!-- 中断通道：L2CAP PSM 0x0013（必须在这里，不能在 0x0004） -->
  <attribute id="0x000d"><sequence>
    <sequence>
      <sequence><uuid value="0x0100"/><uint16 value="0x0013"/></sequence>
      <sequence><uuid value="0x0011"/></sequence>
    </sequence>
  </sequence></attribute>

  <attribute id="0x0100"><text value="{DEVICE_NAME}"/></attribute>
  <attribute id="0x0101"><text value="{DEVICE_NAME}"/></attribute>
  <attribute id="0x0102"><text value="Microsoft"/></attribute>

  <!-- ==== HID 专有属性 ==== -->
  <attribute id="0x0200"><uint16 value="0x0100"/></attribute>
  <attribute id="0x0201"><uint16 value="0x0111"/></attribute>
  <attribute id="0x0202"><uint8 value="0x08"/></attribute>
  <attribute id="0x0203"><uint8 value="0x00"/></attribute>
  <attribute id="0x0204"><boolean value="true"/></attribute>
  <attribute id="0x0205"><boolean value="true"/></attribute>
  <attribute id="0x0206"><sequence><sequence>
    <uint8 value="0x22"/>
    <text encoding="hex" value="{hexdesc}"/>
  </sequence></sequence></attribute>
  <attribute id="0x0207"><sequence><sequence>
    <uint16 value="0x0409"/>
    <uint16 value="0x0100"/>
  </sequence></sequence></attribute>
  <attribute id="0x020b"><uint16 value="0x0100"/></attribute>
  <attribute id="0x020c"><uint16 value="0x0c80"/></attribute>
  <attribute id="0x020d"><boolean value="false"/></attribute>
  <attribute id="0x020e"><boolean value="true"/></attribute>
  <attribute id="0x020f"><uint16 value="0x0640"/></attribute>
  <attribute id="0x0210"><uint16 value="0x0320"/></attribute>
</record>"""


def build_did_record():
    """Device ID (PnP) Profile 记录（UUID 0x1200）。

    蓝牙 HID 设备的 VID/PID 是通过这条记录传递的，而 iOS 的 GameController
    正是按 VID/PID 匹配已知手柄型号 —— 缺了它，iOS 只会把它当成一个
    「能连上的 HID 设备」，不会当成 Xbox 手柄。
    """
    return f"""<?xml version="1.0" encoding="UTF-8" ?>
<record>
  <attribute id="0x0001"><sequence><uuid value="0x1200"/></sequence></attribute>
  <attribute id="0x0004"><sequence>
    <sequence><uuid value="0x0100"/></sequence>
    <sequence><uuid value="0x0001"/></sequence>
  </sequence></attribute>
  <attribute id="0x0005"><sequence><uuid value="0x1002"/></sequence></attribute>
  <attribute id="0x0009"><sequence>
    <sequence><uuid value="0x1200"/></sequence>
  </sequence></attribute>
  <attribute id="0x0200"><uint16 value="0x0103"/></attribute>
  <attribute id="0x0201"><uint16 value="0x0002"/></attribute>
  <attribute id="0x0202"><uint16 value="0x{XBOX_VENDOR_ID:04X}"/></attribute>
  <attribute id="0x0203"><uint16 value="0x{XBOX_PRODUCT_ID:04X}"/></attribute>
  <attribute id="0x0204"><uint16 value="0x{XBOX_VERSION:04X}"/></attribute>
</record>"""


# ============================================================
# HIDP 服务端：两条 L2CAP 通道 + 协议状态机
# ============================================================
class HidpServer:
    def __init__(self):
        self._buttons = 0
        self._lx = self._ly = self._rx = self._ry = 0
        self._lock = threading.Lock()
        self.control_conn = None
        self.interrupt_conn = None
        self._running = False
        self.connected = threading.Event()
        self.protocol_mode = 1          # 1 = Report

    # ---------- 通道 ----------
    @staticmethod
    def _listen(psm):
        s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET,
                          socket.BTPROTO_L2CAP)
        s.bind((BDADDR_ANY, psm))
        s.listen(1)
        return s

    def start(self):
        self._running = True
        self._ctrl_srv = self._listen(PSM_CONTROL)
        self._intr_srv = self._listen(PSM_INTERRUPT)
        print(f"[HIDP] 已监听 control=0x{PSM_CONTROL:02X} "
              f"interrupt=0x{PSM_INTERRUPT:02X}")
        threading.Thread(target=self._accept_loop,
                         args=(self._ctrl_srv, "control"), daemon=True).start()
        threading.Thread(target=self._accept_loop,
                         args=(self._intr_srv, "interrupt"), daemon=True).start()

    def _accept_loop(self, srv, which):
        while self._running:
            try:
                conn, addr = srv.accept()
            except OSError:
                break
            print(f"[HIDP] {which} 通道已连接: {addr[0]}")
            if which == "control":
                self.control_conn = conn
                threading.Thread(target=self._control_loop,
                                 args=(conn,), daemon=True).start()
            else:
                self.interrupt_conn = conn
                self.connected.set()

    # ---------- 控制通道：应答主机的事务 ----------
    def _control_loop(self, conn):
        while self._running:
            try:
                data = conn.recv(1024)
            except OSError:
                break
            if not data:
                break
            ttype = data[0] >> 4
            param = data[0] & 0x0F          # 参数在低 4 位！
            print(f"[HIDP] control <- 0x{data[0]:02X}  "
                  f"{describe_transaction(ttype, param, data[1:])}")
            try:
                self._handle_control(conn, ttype, param, data[1:])
            except OSError:
                break
        print("[HIDP] control 通道断开")

    def _handle_control(self, conn, ttype, param, payload):
        if ttype in (T_SET_PROTOCOL, T_SET_IDLE, T_HID_CONTROL, T_SET_REPORT):
            if ttype == T_SET_PROTOCOL and payload:
                self.protocol_mode = payload[0]
            conn.send(bytes([HDR_HANDSHAKE_OK]))
        elif ttype == T_GET_REPORT:
            # 参数: 1 = Input, 2 = Output, 3 = Feature
            if param == 1:
                conn.send(bytes([HDR_DATA_INPUT]) + self._build_report())
            else:
                conn.send(bytes([HDR_HANDSHAKE_UNSUPPORTED]))
        elif ttype == T_GET_PROTOCOL:
            conn.send(bytes([(T_DATA << 4) | 0x00, self.protocol_mode]))
        else:
            conn.send(bytes([HDR_HANDSHAKE_UNSUPPORTED]))

    # ---------- 输入报告 ----------
    def _hat(self):
        """方向键 -> Xbox Hat switch。

        原厂描述符里 Hat 的 Logical Minimum=1 / Maximum=8（带 Null State），
        所以 1=上 2=右上 3=右 4=右下 5=下 6=左下 7=左 8=左上，0=松开。
        """
        up = bool(self._buttons & (1 << DPAD_UP))
        down = bool(self._buttons & (1 << DPAD_DOWN))
        left = bool(self._buttons & (1 << DPAD_LEFT))
        right = bool(self._buttons & (1 << DPAD_RIGHT))
        if up and right:
            return 2
        if right and down:
            return 4
        if down and left:
            return 6
        if left and up:
            return 8
        if up:
            return 1
        if right:
            return 3
        if down:
            return 5
        if left:
            return 7
        return 0

    def _build_report(self):
        """Xbox One S 报告 ID 1，共 16 字节 —— 严格按 334 字节描述符的布局：

            byte 0-1    X        16-bit 0..65535
            byte 2-3    Y        16-bit
            byte 4-5    Z        16-bit
            byte 6-7    Rz       16-bit
            byte 8-9    Brake    10-bit + 6 位填充  (左扳机)
            byte 10-11  Accel    10-bit + 6 位填充  (右扳机)
            byte 12     Hat      4-bit + 4 位填充
            byte 13-14  按钮 1..15  15-bit + 1 位填充
            byte 15     AC Back  1-bit + 7 位填充
        """
        def axis(v):
            # -127..127 -> 0..65535，中点严格 32768（+127 做四舍五入）
            v = max(-127, min(127, int(v)))
            return ((v + 127) * 65535 + 127) // 254

        b = self._buttons
        lt = 1023 if (b & (1 << 6)) else 0     # LT -> Brake
        rt = 1023 if (b & (1 << 7)) else 0     # RT -> Accelerator

        # UI 索引 -> Xbox 按钮位（LT/RT 走模拟扳机，方向键走 Hat）
        xbtn = 0
        for src_i, dst_bit in ((0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (5, 6),
                               (8, 7), (9, 8), (10, 9), (11, 10)):
            if b & (1 << src_i):
                xbtn |= 1 << (dst_bit - 1)

        r = bytearray(16)
        r[0:2] = axis(self._lx).to_bytes(2, "little")
        r[2:4] = axis(self._ly).to_bytes(2, "little")
        r[4:6] = axis(self._rx).to_bytes(2, "little")
        r[6:8] = axis(self._ry).to_bytes(2, "little")
        r[8:10] = (lt & 0x03FF).to_bytes(2, "little")
        r[10:12] = (rt & 0x03FF).to_bytes(2, "little")
        r[12] = self._hat() & 0x0F
        r[13:15] = (xbtn & 0x7FFF).to_bytes(2, "little")
        r[15] = 0x00                          # AC Back 未按
        return bytes(r)

    def _notify(self):
        report = self._build_report()
        conn = self.interrupt_conn
        print(f"[HID] {report.hex()}  {'已送达' if conn else '无连接'}")
        if conn is None:
            return
        try:
            with self._lock:
                conn.send(bytes([HDR_DATA_INPUT]) + report)
        except OSError:
            self.interrupt_conn = None
            self.connected.clear()

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

    def stop(self):
        self._running = False
        for s in ("_ctrl_srv", "_intr_srv"):
            srv = getattr(self, s, None)
            if srv:
                try:
                    srv.close()
                except OSError:
                    pass


# ============================================================
# D-Bus：发布 SDP 记录 + 让适配器可被发现
# ============================================================
class Profile(ServiceInterface):
    def __init__(self, server):
        super().__init__("org.bluez.Profile1")
        self.server = server

    @method()
    def Release(self):
        print("[Profile1] Release()")

    @method()
    def NewConnection(self, device: "o", fd: "h", properties: "a{sv}"):
        # SDP 记录里的 PSM 归我们自己监听，正常不会走到这里
        print(f"[Profile1] NewConnection device={device} fd={fd}（BlueZ 接管了通道）")

    @method()
    def RequestDisconnection(self, device: "o"):
        print(f"[Profile1] RequestDisconnection device={device}")


async def setup_bluez(server):
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    profile = Profile(server)
    bus.export(PROFILE_PATH, profile)
    did_profile = Profile(server)
    bus.export(DID_PROFILE_PATH, did_profile)

    root = bus.get_proxy_object("org.bluez", "/org/bluez",
                                await bus.introspect("org.bluez", "/org/bluez"))
    pm = root.get_interface("org.bluez.ProfileManager1")

    # bluetoothd 刚重启时适配器可能还没上电，此时注册 profile 会报
    # org.bluez.Error.NotPowered，所以必须先等它真正 powered。
    adapter = bus.get_proxy_object("org.bluez", ADAPTER,
                                   await bus.introspect("org.bluez", ADAPTER))
    ad = adapter.get_interface("org.bluez.Adapter1")
    if not await ad.get_powered():
        print("[BlueZ] 适配器未上电，正在上电...")
        try:
            await ad.set_powered(True)
        except Exception as e:
            print(f"[BlueZ] 上电请求失败: {e}")
    for _ in range(40):                      # 最多等 20 秒
        if await ad.get_powered():
            break
        await asyncio.sleep(0.5)
    if not await ad.get_powered():
        raise RuntimeError("蓝牙适配器无法上电，请检查 rfkill / bluetooth.service")
    print("[BlueZ] 适配器已上电")

    options = {
        "Role": Variant("s", "server"),
        "ServiceRecord": Variant("s", build_sdp_record()),
        "RequireAuthentication": Variant("b", True),
        "RequireAuthorization": Variant("b", False),
        "Name": Variant("s", DEVICE_NAME),
    }
    await pm.call_register_profile(PROFILE_PATH, HID_UUID, options)
    print("[BlueZ] HID SDP 记录已发布")

    did_options = {
        "Role": Variant("s", "server"),
        "ServiceRecord": Variant("s", build_did_record()),
        "RequireAuthentication": Variant("b", False),
        "RequireAuthorization": Variant("b", False),
        "Name": Variant("s", "Device ID"),
    }
    try:
        await pm.call_register_profile(DID_PROFILE_PATH, DID_UUID, did_options)
        print(f"[BlueZ] DID(PnP) 记录已发布: Microsoft "
              f"{XBOX_VENDOR_ID:04X}:{XBOX_PRODUCT_ID:04X}")
    except Exception as e:
        print(f"[BlueZ] DID 记录发布失败（不致命，但 iOS 可能认不出型号）: {e}")

    await ad.set_alias(DEVICE_NAME)
    await ad.set_pairable(True)
    await ad.set_discoverable(True)
    print(f"[BlueZ] 适配器已改名为 {DEVICE_NAME}，并设为可发现/可配对")
    return bus, pm


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

    def __init__(self, pad):
        self.pad = pad
        self.root = tk.Tk()
        self.root.title(f"经典蓝牙 HID 手柄（伪装 {DEVICE_NAME}）")
        self.root.geometry("560x560")
        self.root.resizable(False, False)
        self._build()

    def _build(self):
        self.status = tk.Label(self.root, text="等待主机连接...",
                               fg="orange", font=("Arial", 12))
        self.status.pack(pady=5)

        frame = tk.Frame(self.root)
        frame.pack(pady=5)
        for i, (name, idx) in enumerate(self.BUTTONS):
            b = tk.Button(frame, text=name, width=6, height=2)
            b.grid(row=i // 4, column=i % 4, padx=3, pady=3)
            b.bind("<ButtonPress-1>", lambda e, x=idx: self.pad.set_button(x, True))
            b.bind("<ButtonRelease-1>", lambda e, x=idx: self.pad.set_button(x, False))

        sticks = tk.Frame(self.root)
        sticks.pack(pady=10)
        self.lx = self._scale(sticks, "左X", 0, 1, lambda _: self._left())
        self.ly = self._scale(sticks, "左Y", 0, 2, lambda _: self._left())
        self.rx = self._scale(sticks, "右X", 1, 1, lambda _: self._right())
        self.ry = self._scale(sticks, "右Y", 1, 2, lambda _: self._right())

        threading.Thread(target=self._watch, daemon=True).start()

    def _scale(self, parent, label, col, row, cmd):
        tk.Label(parent, text=label, font=("Arial", 10)).grid(
            row=row * 2 - 2, column=col)
        s = tk.Scale(parent, from_=-127, to=127, orient=tk.HORIZONTAL,
                     length=180, command=cmd)
        s.grid(row=row * 2 - 1, column=col, padx=5)
        return s

    def _left(self):
        self.pad.set_left_stick(self.lx.get(), self.ly.get())

    def _right(self):
        self.pad.set_right_stick(self.rx.get(), self.ry.get())

    def _watch(self):
        while True:
            self.pad.connected.wait()          # 等到主机连上
            self.status.config(text="主机已连接，可以操作游戏了", fg="green")
            while self.pad.connected.is_set():  # 一直等到断开
                time.sleep(0.3)
            self.status.config(text="连接断开，等待重连...", fg="orange")

    def run(self):
        self.root.mainloop()


def start_bluez_thread(server):
    """在后台线程里跑 D-Bus 事件循环。

    Profile1 对象和 bus 连接必须一直活着，BlueZ 才会保留那条 SDP 记录 ——
    所以不能像普通脚本那样跑完就退出。
    """
    ready = threading.Event()
    holder = {"error": None}

    def run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(setup_bluez(server))
        except Exception as e:
            holder["error"] = e
            ready.set()
            return
        ready.set()
        loop.run_forever()          # 保持 D-Bus 连接，让 SDP 记录一直有效

    threading.Thread(target=run, daemon=True).start()
    ready.wait(20)
    return holder["error"]


def main():
    server = HidpServer()
    try:
        server.start()
    except OSError as e:
        print(f"[错误] 绑定 L2CAP PSM 失败: {e}")
        print("       PSM 0x11/0x13 是特权端口，需要 CAP_NET_BIND_SERVICE。")
        print("       一次性授权（然后以普通用户运行，GUI 才正常）：")
        print("         sudo setcap cap_net_bind_service=+ep /usr/bin/python3.14")
        return 1

    err = start_bluez_thread(server)
    if err:
        print(f"[错误] 发布 SDP 记录失败: {type(err).__name__}: {err}")
        server.stop()
        return 1

    print(f"\n请在 iPhone 上：设置 → 蓝牙 → 搜索并配对「{DEVICE_NAME}」\n")
    try:
        GamepadWindow(server).run()
    finally:
        server.stop()
        print("\n清理中...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
