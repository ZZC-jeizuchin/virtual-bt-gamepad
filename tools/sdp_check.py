#!/usr/bin/env python3
"""第二道关卡：能否用 ProfileManager1 发布一条带 L2CAP 双 PSM 的 HID SDP 记录。

必须用 sudo（后面还要检查 BlueZ 有没有把那两个特权 PSM 抢走），并且要让
dbus_next 能被找到：

    sudo PYTHONPATH=$HOME/.local/lib/python3.14/site-packages \
         python3 sdp_check.py

成功判据：
  1) RegisterProfile 不报错
  2) 适配器 UUID 列表里出现 1124（Human Interface Device）
  3) 记录里能带自定义 ServiceRecord（说明 Xbox 描述符/VID/PID 塞得进去）
"""
import asyncio
import os
import sys

from dbus_next.aio import MessageBus
from dbus_next import BusType, Variant
from dbus_next.service import ServiceInterface, method

ADAPTER = "/org/bluez/hci0"
PROFILE_PATH = "/com/spacecheese/xboxhid"
HID_UUID = "00001124-0000-1000-8000-00805f9b34fb"

# 最小的 HID report descriptor（先只验证机制，Xbox 描述符之后再换进来）
REPORT_DESC = (
    "05010905a10185010509190129101500250175019510810"
    "2050109300931093209351581257f750895048102050109"
    "39150025073500463b0165147504950 181426500750495018103c0"
).replace(" ", "")

SDP_RECORD = f"""<?xml version="1.0" encoding="UTF-8" ?>
<record>
  <attribute id="0x0001"><sequence><uuid value="0x1124"/></sequence></attribute>
  <attribute id="0x0004"><sequence>
    <sequence><uuid value="0x0100"/></sequence>
    <sequence><uuid value="0x0001"/></sequence>
  </sequence></attribute>
  <attribute id="0x0005"><sequence><uuid value="0x1002"/></sequence></attribute>
  <attribute id="0x0006"><sequence>
    <sequence><uuid value="0x0100"/></sequence>
    <sequence><uuid value="0x0003"/></sequence>
  </sequence></attribute>
  <attribute id="0x0009"><sequence>
    <sequence><uuid value="0x1124"/></sequence>
  </sequence></attribute>
  <attribute id="0x000d"><sequence>
    <sequence><uuid value="0x0100"/><uint16 value="0x0011"/></sequence>
    <sequence><uuid value="0x0011"/></sequence>
  </sequence><sequence>
    <sequence><uuid value="0x0100"/><uint16 value="0x0013"/></sequence>
    <sequence><uuid value="0x0011"/></sequence>
  </sequence></attribute>
  <attribute id="0x0100"><text value="Xbox Wireless Controller"/></attribute>
  <attribute id="0x0200"><uint16 value="0x0100"/></attribute>
  <attribute id="0x0201"><uint16 value="0x0111"/></attribute>
  <attribute id="0x0202"><uint8 value="0x08"/></attribute>
  <attribute id="0x0203"><uint8 value="0x00"/></attribute>
  <attribute id="0x0204"><boolean value="true"/></attribute>
  <attribute id="0x0205"><boolean value="true"/></attribute>
  <attribute id="0x0206"><sequence><sequence>
    <uint8 value="0x22"/>
    <text encoding="hex" value="{REPORT_DESC}"/>
  </sequence></sequence></attribute>
  <attribute id="0x0207"><uint16 value="0x0011"/></attribute>
  <attribute id="0x0208"><boolean value="false"/></attribute>
  <attribute id="0x020a"><boolean value="true"/></attribute>
  <attribute id="0x020b"><uint8 value="0x00"/></attribute>
  <attribute id="0x020c"><uint16 value="0x0c80"/></attribute>
  <attribute id="0x020d"><uint16 value="0x0320"/></attribute>
  <attribute id="0x020e"><uint16 value="0xffff"/></attribute>
  <attribute id="0x020f"><uint16 value="0xffff"/></attribute>
</record>"""


class TestProfile(ServiceInterface):
    def __init__(self):
        super().__init__("org.bluez.Profile1")
        self.connected = []

    @method()
    def Release(self):
        print("  [Profile1] Release()")

    @method()
    def NewConnection(self, device: "o", fd: "h", properties: "a{sv}"):
        print(f"  [Profile1] NewConnection device={device} fd={fd}  <- BlueZ 接管了这条通道")
        self.connected.append((device, fd))

    @method()
    def RequestDisconnection(self, device: "o"):
        print(f"  [Profile1] RequestDisconnection device={device}")


async def main():
    print(f"euid = {os.geteuid()}")
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    prof = TestProfile()
    bus.export(PROFILE_PATH, prof)

    # ProfileManager1 挂在 BlueZ 的根对象 /org/bluez 上，不在适配器对象上
    root = bus.get_proxy_object(
        "org.bluez", "/org/bluez",
        await bus.introspect("org.bluez", "/org/bluez"))
    pm = root.get_interface("org.bluez.ProfileManager1")

    adapter = bus.get_proxy_object(
        "org.bluez", ADAPTER, await bus.introspect("org.bluez", ADAPTER))
    props = adapter.get_interface("org.freedesktop.DBus.Properties")

    # RegisterProfile 的 options 是 a{sv}，每个值都必须包成 Variant
    options = {
        "Role": Variant("s", "server"),
        "ServiceRecord": Variant("s", SDP_RECORD),
        "RequireAuthentication": Variant("b", True),
        "RequireAuthorization": Variant("b", False),
        "Name": Variant("s", "Xbox Wireless Controller"),
    }

    print("\n[1] 调用 RegisterProfile ...")
    try:
        await pm.call_register_profile(PROFILE_PATH, HID_UUID, options)
        print("  ✓ RegisterProfile 成功")
    except Exception as e:
        print(f"  ✗ RegisterProfile 失败: {type(e).__name__}: {e}")
        return 1

    print("\n[2] 检查适配器 UUID 列表里是否出现 1124 ...")
    await asyncio.sleep(2)
    uuids = (await props.call_get("org.bluez.Adapter1", "UUIDs")).value
    hit = [u for u in uuids if u.lower().startswith("00001124")]
    if hit:
        print(f"  ✓ HID 服务记录已发布: {hit}")
    else:
        print("  ✗ 没看到 1124 —— SDP 记录没进 BlueZ 的数据库")
    print(f"  (适配器 UUID 共 {len(uuids)} 条)")

    print("\n[3] 检查 BlueZ 是否抢占了 PSM 0x11 / 0x13 ...")
    import socket
    for psm, name in ((0x11, "Control"), (0x13, "Interrupt")):
        try:
            s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET,
                              socket.BTPROTO_L2CAP)
            s.bind(("00:00:00:00:00:00", psm))
            s.listen(1)
            s.close()
            print(f"  ✓ PSM 0x{psm:02X} ({name}) 仍可自己监听 —— 通道归我们")
        except OSError as e:
            print(f"  ✗ PSM 0x{psm:02X} ({name}) 被占用: [{e.errno}] {e.strerror}"
                  "  —— BlueZ 接管了，得改用它的 fd")

    print("\n[4] 注销 ...")
    try:
        await pm.call_unregister_profile(PROFILE_PATH)
        print("  ✓ UnregisterProfile 成功")
    except Exception as e:
        print(f"  ✗ UnregisterProfile 失败: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
