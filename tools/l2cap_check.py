#!/usr/bin/env python3
"""可行性检查：能否在 HIDP 的 control(0x11) / interrupt(0x13) 两个 PSM 上
建立 L2CAP 监听 socket。

必须用 sudo 运行（两个 PSM 都小于 0x1000，属于特权端口，内核要求
CAP_NET_BIND_SERVICE）：

    sudo python3 l2cap_check.py
"""
import socket
import sys

HIDP_CONTROL = 0x11
HIDP_INTERRUPT = 0x13

BDADDR_ANY = "00:00:00:00:00:00"


def try_bind(psm, name):
    for socktype, tn in ((socket.SOCK_SEQPACKET, "SEQPACKET"),
                         (socket.SOCK_RAW, "RAW")):
        try:
            s = socket.socket(socket.AF_BLUETOOTH, socktype
                              if hasattr(socket, "BTPROTO_L2CAP") else socktype,
                              socket.BTPROTO_L2CAP)
        except OSError as e:
            print(f"  ✗ {name} 创建 socket 失败: [{e.errno}] {e.strerror}")
            return None
        try:
            s.bind((BDADDR_ANY, psm))
            s.listen(1)
            print(f"  ✓ PSM 0x{psm:02X} ({name}) 用 {tn} 绑定并监听成功")
            return s
        except OSError as e:
            print(f"  ✗ PSM 0x{psm:02X} ({name}) {tn}: [{e.errno}] {e.strerror}")
            s.close()
    return None


def main():
    print("AF_BLUETOOTH =", socket.AF_BLUETOOTH,
          " BTPROTO_L2CAP =", socket.BTPROTO_L2CAP)
    print(f"当前 euid = {__import__('os').geteuid()}"
          f"{' (root)' if __import__('os').geteuid() == 0 else ' (非 root)'}")
    print()

    ctrl = try_bind(HIDP_CONTROL, "HID Control")
    intr = try_bind(HIDP_INTERRUPT, "HID Interrupt")
    print()
    if ctrl and intr:
        print("结论：两个 PSM 都能监听 —— HIDP 服务端可以自己实现。")
        ctrl.close()
        intr.close()
        return 0
    print("结论：仍拿不到特权 PSM。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
