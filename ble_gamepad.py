#!/usr/bin/env python3
"""
BLE HID 游戏手柄（HID over GATT）—— 免 root

把 Linux 电脑变成蓝牙游戏手柄，供 Android / Windows / macOS / Linux 主机使用。
16 按钮 + 方向键(Hat) + 双摇杆 + 模拟扳机，Tkinter 界面操作。

用法:
    python3 ble_gamepad.py

可选环境变量:
    GAMEPAD_FORCE_LE=AA:BB:CC:DD:EE:FF
        把该主机的首选承载强制为 LE（Linux 上部分双模主机如 iPhone
        会用经典蓝牙链路抢占 LE，导致 GATT 手柄不可用）。
执行: python3 ble_gamepad.py
"""

import asyncio
import logging
import os
import struct
import threading
import time
import tkinter as tk

from dbus_next import Variant

from bluez_peripheral.gatt.service import Service
from bluez_peripheral.gatt.characteristic import characteristic, CharacteristicFlags as CharFlags
from bluez_peripheral.gatt.descriptor import DescriptorFlags
from bluez_peripheral.advert import Advertisement, PacketType
from bluez_peripheral.agent import NoIoAgent
from bluez_peripheral.util import get_message_bus, Adapter


ADVERT_NAME = "Linux Gamepad"
HID_SERVICE_UUID = "00001812-0000-1000-8000-00805f9b34fb"

# iPhone 这类双模设备会维持一条经典蓝牙（BR/EDR）链路，导致 LE 一直不建立，
# 而 HID over GATT 只能走 LE —— 结果就是「能被识别成手柄，但游戏里毫无反应」。
# BlueZ 的 PreferredBearer 是「按设备」保存的，设为 le 就强制这个设备走 LE，
# 并且完全不影响本机对其他经典设备（比如 A2DP 音箱）的使用。
# 填需要强制的设备地址；留空列表则跳过。
FORCE_LE_BEARER_FOR = [
    a.strip() for a in os.environ.get("GAMEPAD_FORCE_LE", "").split(",") if a.strip()
]

# BlueZ 5.85 通告 SupportedFeatures=CanSetTxPower，注册广告时会查询
# LEAdvertisement1.TxPower；bluez-peripheral 没有声明该属性，dbus_next 于是打印
# 一段 UNKNOWN_PROPERTY traceback。它不影响注册，属于噪音，这里只过滤这一条。
class _BenignBlueZNoiseFilter(logging.Filter):
    _IGNORED = ('does not have property "TxPower"',)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            return not any(s in record.getMessage() for s in self._IGNORED)
        except Exception:
            return True


logging.getLogger().addFilter(_BenignBlueZNoiseFilter())


# ============================================================
# 1. HID Report Descriptor：16 按钮 + 左右摇杆
# ============================================================
GAMEPAD_REPORT_DESCRIPTOR = bytes([
    0x05, 0x01,        # Usage Page (Generic Desktop Ctrls)
    0x09, 0x05,        # Usage (Game Pad)
    0xA1, 0x01,        # Collection (Application)
    0x85, 0x01,        #   Report ID (1)

    # ---- 16 个按钮（2 字节） ----
    0x05, 0x09,        #   Usage Page (Button)
    0x19, 0x01,        #   Usage Minimum (Button 1)
    0x29, 0x10,        #   Usage Maximum (Button 16)
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x01,        #   Logical Maximum (1)
    0x75, 0x01,        #   Report Size (1)
    0x95, 0x10,        #   Report Count (16)
    0x81, 0x02,        #   Input (Data,Var,Abs)

    # ---- 左右摇杆 X / Y / Z / Rz（4 字节）----
    0x05, 0x01,        #   Usage Page (Generic Desktop)
    0x09, 0x30,        #   Usage (X)
    0x09, 0x31,        #   Usage (Y)
    0x09, 0x32,        #   Usage (Z)
    0x09, 0x35,        #   Usage (Rz)
    0x15, 0x81,        #   Logical Minimum (-127)
    0x25, 0x7F,        #   Logical Maximum (127)
    0x75, 0x08,        #   Report Size (8)
    0x95, 0x04,        #   Report Count (4)
    0x81, 0x02,        #   Input (Data,Var,Abs)

    # ---- 模拟扳机 Rx / Ry（2 字节，0..255）----
    # iOS 的 GCExtendedGamepad 要求 leftTrigger/rightTrigger 存在；
    # 缺这两个轴时 iOS 可能不把这个 HID 设备组装成手柄。
    0x05, 0x01,        #   Usage Page (Generic Desktop)
    0x09, 0x33,        #   Usage (Rx)
    0x09, 0x34,        #   Usage (Ry)
    0x15, 0x00,        #   Logical Minimum (0)
    0x26, 0xFF, 0x00,  #   Logical Maximum (255)
    0x75, 0x08,        #   Report Size (8)
    0x95, 0x02,        #   Report Count (2)
    0x81, 0x02,        #   Input (Data,Var,Abs)

    # ---- 方向键：标准 Hat switch（1 字节，低 4 位有效）----
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
    0x81, 0x03,        #   Input (Const,Var,Abs)  -- 高位补白

    0xC0               # End Collection
])


# ============================================================
# 2. HID over GATT 服务
# ============================================================
class HIDService(Service):
    def __init__(self):
        super().__init__(
            "00001812-0000-1000-8000-00805f9b34fb",
            primary=True,
            includes=[],
        )
        self._buttons = 0
        self._lx = 0
        self._ly = 0
        self._rx = 0
        self._ry = 0
        self._protocol_mode = 0x01   # 0x00=Boot, 0x01=Report

    @characteristic("00002A4A-0000-1000-8000-00805f9b34fb", CharFlags.READ)
    def hid_information(self, options):
        return bytes([0x11, 0x01, 0x00, 0x02])

    # Apple 的 HID 规范要求 Report Map 必须「加密可读」。iOS 发现 HID 服务后，
    # 若看到 Report Map 是明文可读，会判定这不是合规的 HID 设备，从而拒绝
    # 继续 HID 初始化 —— 表现正是「连上 LE，却从不订阅输入报告」。
    # 设备已经会绑定（NoIoAgent），所以加密链路是现成的。
    @characteristic("00002A4B-0000-1000-8000-00805f9b34fb",
                    CharFlags.READ | CharFlags.ENCRYPT_READ)
    def report_map(self, options):
        return GAMEPAD_REPORT_DESCRIPTOR

    @characteristic("00002A4C-0000-1000-8000-00805f9b34fb", CharFlags.WRITE_WITHOUT_RESPONSE)
    def hid_control_point(self, options):
        return b""

    # HOGP：主机建立 HID 时会写 HID Control Point（0x00 退出挂起 / 0x01 挂起）。
    # bluez-peripheral 的 WriteValue 会直接调用 setter_func，没有 setter 就是
    # None(...)，于是向主机返回 D-Bus 错误。
    @hid_control_point.setter
    def hid_control_point(self, value, options):
        print(f"[HID] HID Control Point <- {bytes(value).hex()}")

    @characteristic("00002A4E-0000-1000-8000-00805f9b34fb", CharFlags.READ | CharFlags.WRITE_WITHOUT_RESPONSE)
    def protocol_mode(self, options):
        return bytes([self._protocol_mode])

    # 关键修复：iOS 在 HID 初始化时会写 Protocol Mode = 0x01(Report Protocol)，
    # 然后才订阅 Input Report。之前这个特征只有 getter 没有 setter，写入必然
    # 失败，iOS 于是放弃 HID 初始化、永不订阅 —— 表现为“能被识别成手柄，
    # 但游戏里毫无反应”（实测 _notify 一直是 False，protocol_mode 值一直是空）。
    @protocol_mode.setter
    def protocol_mode(self, value, options):
        self._protocol_mode = value[0] if value else 0x01
        print(f"[HID] Protocol Mode <- {self._protocol_mode:#04x}")

    @characteristic("00002A4D-0000-1000-8000-00805f9b34fb", CharFlags.NOTIFY | CharFlags.READ)
    def input_report(self, options):
        return self._build_report()

    # HOGP 规范强制要求：每个 Report 特征都必须带 Report Reference 描述符
    # (0x2908)，否则主机无法把 GATT 特征对应成 HID 报告 —— iOS/Android 会
    # 直接忽略这个 HID 服务。值 = [Report ID, Report Type]，1=Input。
    # 本报告描述符没有使用 Report ID，所以 Report ID = 0x00。
    @input_report.descriptor(
        "00002908-0000-1000-8000-00805f9b34fb",
        DescriptorFlags.READ | DescriptorFlags.ENCRYPT_READ,
    )
    def input_report_reference(self, options):
        # [Report ID, Report Type]：描述符里声明了 Report ID = 1，所以这里是 0x01
        return bytes([0x01, 0x01])

    def _hat(self):
        """把方向键的 4 个按钮折算成标准 Hat switch 值。

        0=上 1=右上 2=右 3=右下 4=下 5=左下 6=左 7=左上 15=松开
        """
        up = bool(self._buttons & (1 << 12))
        down = bool(self._buttons & (1 << 13))
        left = bool(self._buttons & (1 << 14))
        right = bool(self._buttons & (1 << 15))
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
        return 15

    def _build_report(self):
        # 报告 10 字节：ReportID(1) + 按钮(2) + X/Y/Z/Rz(4) + Rx/Ry 扳机(2) + Hat(1)
        # 方向键只走 Hat，不占用按钮位（iOS 需要这样才认成完整手柄）。
        buttons = self._buttons & 0x0FFF
        hat_byte = (self._hat() & 0x0F) | 0xF0
        lt = 255 if (self._buttons & (1 << 6)) else 0     # LT
        rt = 255 if (self._buttons & (1 << 7)) else 0     # RT
        return (bytes([0x01])
                + struct.pack("<Hbbbb", buttons, self._lx, self._ly,
                              self._rx, self._ry)
                + bytes([lt, rt, hat_byte]))

    def _notify(self):
        report = self._build_report()
        # 把订阅状态打出来：iOS 订阅了才会真正收到通知。
        # 若一直是 False，说明 HID 初始化没完成，报告发不出去。
        print(f"[HID] {report.hex()}  已订阅={self.input_report._notify}")
        self.input_report.changed(report)

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
# 3. 蓝牙线程
# ============================================================
class BluetoothHIDThread(threading.Thread):
    def __init__(self, hid_service: HIDService):
        super().__init__(daemon=True)
        self.hid_service = hid_service
        self.loop = None
        self._ready = threading.Event()

    def run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._main())
        except Exception as e:
            print(f"[蓝牙线程] 异常退出: {e}")

    @staticmethod
    async def _wait_powered(adapter, timeout: float = 20.0) -> bool:
        """轮询等待适配器真正上电。

        BlueZ 5.85 有 off-enabling/off-disabling 等中间态：`power on` 返回后
        Powered 仍可能短暂为 no，此时注册广告会被控制器拒绝（Rejected 0x0b）。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if await adapter.get_powered():
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.5)
        return False

    async def _ensure_adapter_ready(self, adapter, timeout: float = 20.0):
        """上电 + 设为可发现/可配对，并让 GAP 设备名与广播名一致。"""
        if not await self._wait_powered(adapter, timeout=2.0):
            print("[蓝牙线程] 适配器未上电，正在请求上电...")
            try:
                await adapter.set_powered(True)
            except Exception as e:
                print(f"[蓝牙线程] 上电请求失败: {e}")
            if not await self._wait_powered(adapter, timeout=timeout):
                raise RuntimeError(
                    "蓝牙适配器无法上电，请检查 rfkill list / bluetooth.service 状态"
                )

        # 只设为可配对（BlueZ 的 bondable，LE 绑定也需要它）。
        # 注意：不要改适配器 Alias、也不要开 Discoverable：
        #   * 改 Alias 会让「经典蓝牙」那一侧也顶着同一个名字出现，
        #     iPhone 上就会多出一个同名设备；
        #   * 经典侧可发现会让 iPhone 建立经典绑定，反过来抢占 LE 链路
        #     （LE.Connected 永远是 no，GATT HID 就永远不可达）。
        # LE 的可见性由广播自身的 discoverable=True 负责。
        try:
            await adapter._adapter_interface.set_pairable(True)
            print("[蓝牙线程] 已设为可配对")
        except Exception as e:
            print(f"[蓝牙线程] 设置可配对失败（不致命）: {e}")

    @staticmethod
    async def _enforce_le_bearer(bus, adapter):
        """后台任务：持续保证指定设备走 LE。

        双模设备（iPhone）一旦握着经典蓝牙链路，LE 就不会建立，而 HID over
        GATT 只能走 LE —— 现象就是 iOS 从不订阅输入报告（日志里
        「已订阅=False」），按键怎么点游戏都没反应。

        PreferredBearer 是「按设备」保存的，设成 le 不影响本机连音箱。
        但刚重新配对后这个属性会被重置成 last-used，所以不能只在启动时设一次，
        必须持续盯着：发现不是 le 就改回来，并踢掉现有连接逼它重新以 LE 建立。
        """
        try:
            base = adapter._proxy.path
        except Exception:
            base = "/org/bluez/hci0"

        while True:
            for address in FORCE_LE_BEARER_FOR:
                path = f"{base}/dev_{address.replace(':', '_')}"
                try:
                    introspection = await bus.introspect("org.bluez", path)
                    proxy = bus.get_proxy_object("org.bluez", path, introspection)
                    props = proxy.get_interface("org.freedesktop.DBus.Properties")

                    bearer = (await props.call_get(
                        "org.bluez.Device1", "PreferredBearer")).value
                    if bearer != "le":
                        await props.call_set(
                            "org.bluez.Device1", "PreferredBearer",
                            Variant("s", "le"))
                        print(f"[蓝牙线程] {address} 承载不是 LE，已强制改回")
                        if (await props.call_get(
                                "org.bluez.Device1", "Connected")).value:
                            await proxy.get_interface(
                                "org.bluez.Device1").call_disconnect()
                            print(f"[蓝牙线程] 已断开 {address}，让它重连到 LE")
                except Exception:
                    pass  # 尚未配对时该路径不存在，属正常
            await asyncio.sleep(3)

    @staticmethod
    async def _kick_connection(bus, adapter):
        """把已连接的设备踢下线，逼它重新连接。

        iOS 只在「建立连接的那一刻」做 GATT 服务发现，并把结果缓存到这次连接里。
        因为它那边有旧绑定，经常在我们注册 HID 服务之前就自行连上了 —— 那一刻
        BlueZ 的属性数据库还是空的，iOS 什么都发现不到，之后也不会重新发现。
        结果就是它永远不订阅输入报告（日志里「已订阅=False」），游戏毫无反应。

        所以在服务 + 广告就绪之后，主动断一次，让 iOS 重新连、重新发现。
        """
        try:
            base = adapter._proxy.path
        except Exception:
            base = "/org/bluez/hci0"

        for address in FORCE_LE_BEARER_FOR:
            path = f"{base}/dev_{address.replace(':', '_')}"
            try:
                introspection = await bus.introspect("org.bluez", path)
                proxy = bus.get_proxy_object("org.bluez", path, introspection)
                props = proxy.get_interface("org.freedesktop.DBus.Properties")
                if (await props.call_get("org.bluez.Device1", "Connected")).value:
                    await proxy.get_interface("org.bluez.Device1").call_disconnect()
                    print(f"[蓝牙线程] 已断开 {address}，让它重新连接并重新发现 HID 服务")
            except Exception:
                pass

    async def _watch_subscription(self):
        """订阅状态一变就打印。

        这是判断「iOS 有没有真的开始接收报告」的唯一硬指标：
        bluez-peripheral 的 changed() 里有 `if self._notify` 判断，
        _notify 为 False 时一个字节都不会发出去，游戏必然毫无反应。
        """
        last = None
        while True:
            current = self.hid_service.input_report._notify
            if current != last:
                print(f"[蓝牙线程] iOS 输入报告订阅状态 -> {current}")
                last = current
            await asyncio.sleep(1)

    async def _main(self):
        bus = await get_message_bus()
        adapter = await Adapter.get_first(bus)
        print(f"[蓝牙线程] 适配器: {await adapter.get_address()}")

        await self._ensure_adapter_ready(adapter)
        asyncio.ensure_future(self._enforce_le_bearer(bus, adapter))
        asyncio.ensure_future(self._watch_subscription())

        await self.hid_service.register(bus)
        print("[蓝牙线程] HID 服务已注册")

        agent = NoIoAgent()
        await agent.register(bus)
        print("[蓝牙线程] 配对 Agent 已注册")

        advert = Advertisement(
            ADVERT_NAME,
            [HID_SERVICE_UUID],
            0x03C4,                     # Appearance: Gamepad
            0,                          # Timeout 0 = 一直广播，不退订
            discoverable=True,          # 置 LE General Discoverable 标志
            packet_type=PacketType.PERIPHERAL,   # 可连接广播（ADV_IND）
        )

        # 退避重试：bluez-peripheral 0.1.7 注册失败时不会 unexport 已导出的
        # 对象，所以必须每次换一个新的 D-Bus 路径。注意路径要通过 register()
        # 的 path 参数传入 —— 写 advert.path 属性是无效的（0.1.7 不读它）。
        last_error = None
        for attempt in range(1, 6):
            path = f"/com/spacecheese/bluez_peripheral/advert{attempt}"
            try:
                await advert.register(bus, adapter, path=path)
                last_error = None
                print(f"[蓝牙线程] 广告已注册，设备名: {ADVERT_NAME}")
                break
            except Exception as e:
                last_error = e
                print(f"[蓝牙线程] 广告注册失败（第 {attempt}/5 次）: {e}")
                await self._wait_powered(adapter, timeout=5.0)
                await asyncio.sleep(2 * attempt)
        if last_error is not None:
            raise RuntimeError(f"广告注册失败，已重试 5 次: {last_error}")

        # 服务 + 广告都就绪了，把可能早先自行连上来的 iPhone 踢掉，
        # 让它在数据库完整之后重新连接并重新发现 HID 服务。
        await self._kick_connection(bus, adapter)

        self._ready.set()

        while True:
            await asyncio.sleep(1)

    def wait_ready(self, timeout=15):
        return self._ready.wait(timeout=timeout)

    def set_button(self, index, pressed):
        if self.loop:
            asyncio.run_coroutine_threadsafe(
                self._async_set_button(index, pressed), self.loop
            )

    async def _async_set_button(self, index, pressed):
        self.hid_service.set_button(index, pressed)

    def set_left_stick(self, x, y):
        if self.loop:
            asyncio.run_coroutine_threadsafe(
                self._async_set_left_stick(x, y), self.loop
            )

    async def _async_set_left_stick(self, x, y):
        self.hid_service.set_left_stick(x, y)

    def set_right_stick(self, x, y):
        if self.loop:
            asyncio.run_coroutine_threadsafe(
                self._async_set_right_stick(x, y), self.loop
            )

    async def _async_set_right_stick(self, x, y):
        self.hid_service.set_right_stick(x, y)


# ============================================================
# 4. Tkinter 界面
# ============================================================
class GamepadWindow:
    BUTTONS = [
        ("A", 0), ("B", 1), ("X", 2), ("Y", 3),
        ("LB", 4), ("RB", 5), ("LT", 6), ("RT", 7),
        ("Back", 8), ("Start", 9), ("L3", 10), ("R3", 11),
        ("Up", 12), ("Down", 13), ("Left", 14), ("Right", 15),
    ]

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Linux 蓝牙游戏手柄")
        self.root.geometry("560x560")
        self.root.resizable(False, False)

        self.hid_service = HIDService()
        self.bt_thread = BluetoothHIDThread(self.hid_service)

        self._build_ui()

    def _build_ui(self):
        self.status_label = tk.Label(
            self.root, text="蓝牙初始化中...", fg="orange", font=("Arial", 12),
        )
        self.status_label.pack(pady=5)

        btn_frame = tk.Frame(self.root)
        btn_frame.pack(pady=5)
        for i, (name, index) in enumerate(self.BUTTONS):
            btn = tk.Button(btn_frame, text=name, width=6, height=2)
            btn.grid(row=i // 4, column=i % 4, padx=3, pady=3)
            btn.bind("<ButtonPress-1>", lambda e, idx=index: self._on_press(idx))
            btn.bind("<ButtonRelease-1>", lambda e, idx=index: self._on_release(idx))

        stick_frame = tk.Frame(self.root)
        stick_frame.pack(pady=10)

        tk.Label(stick_frame, text="左摇杆", font=("Arial", 11, "bold")).grid(
            row=0, column=0, columnspan=2
        )
        self.lx_scale = tk.Scale(
            stick_frame, from_=-127, to=127, orient=tk.HORIZONTAL,
            label="X", length=180, command=self._on_left_stick,
        )
        self.lx_scale.grid(row=1, column=0, padx=5)
        self.ly_scale = tk.Scale(
            stick_frame, from_=-127, to=127, orient=tk.HORIZONTAL,
            label="Y", length=180, command=self._on_left_stick,
        )
        self.ly_scale.grid(row=2, column=0, padx=5)

        tk.Label(stick_frame, text="右摇杆", font=("Arial", 11, "bold")).grid(
            row=0, column=1, columnspan=2
        )
        self.rx_scale = tk.Scale(
            stick_frame, from_=-127, to=127, orient=tk.HORIZONTAL,
            label="X", length=180, command=self._on_right_stick,
        )
        self.rx_scale.grid(row=1, column=1, padx=5)
        self.ry_scale = tk.Scale(
            stick_frame, from_=-127, to=127, orient=tk.HORIZONTAL,
            label="Y", length=180, command=self._on_right_stick,
        )
        self.ry_scale.grid(row=2, column=1, padx=5)

    def _on_press(self, index):
        self.bt_thread.set_button(index, True)

    def _on_release(self, index):
        self.bt_thread.set_button(index, False)

    def _on_left_stick(self, _=None):
        self.bt_thread.set_left_stick(self.lx_scale.get(), self.ly_scale.get())

    def _on_right_stick(self, _=None):
        self.bt_thread.set_right_stick(self.rx_scale.get(), self.ry_scale.get())

    def run(self):
        self.bt_thread.start()

        def _enable_when_ready():
            if self.bt_thread.wait_ready(timeout=20):
                self.status_label.config(
                    text="就绪，iPhone 搜索 'Linux Gamepad'", fg="blue",
                )
            else:
                self.status_label.config(
                    text="蓝牙初始化失败，看终端输出", fg="red",
                )

        threading.Thread(target=_enable_when_ready, daemon=True).start()
        self.root.mainloop()


if __name__ == "__main__":
    print("=" * 50)
    print("Linux 蓝牙 HID 游戏手柄 - 完整版")
    print("=" * 50)

    app = GamepadWindow()
    app.run()
