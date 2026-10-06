#!/usr/bin/env python3
"""把本机 SDP 服务器里的 HID 记录读回来，验证报告描述符到底发出去没有。

在 xbox_hid_gamepad.py 运行期间、另开一个终端跑：
    python3 sdp_query.py

原理：SDP 跑在 L2CAP PSM 1 上。连到 BDADDR_LOCAL（FF:FF:FF:00:00:00）就是
本机 SDP 服务器，然后发一个 ServiceSearchAttributeRequest 查 0x1124 服务的
全部属性，把原始应答打出来 —— 只要能在里面找到我们的报告描述符字节，
就说明发布是成功的，问题在 iOS 那边。
"""
import socket
import struct
import sys

BDADDR_LOCAL = "FF:FF:FF:00:00:00"
SDP_PSM = 0x0001
HID_UUID16 = 0x1124

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
try:
    from classic_gamepad import REPORT_DESCRIPTOR
except Exception:
    REPORT_DESCRIPTOR = None


def make_pdu(pdu_id, params):
    return bytes([pdu_id]) + struct.pack(">H", len(params)) + params


def build_request():
    # ServiceSearchPattern: sequence { uuid16 0x1124 }
    pattern = bytes([0x35, 0x03, 0x19]) + struct.pack(">H", HID_UUID16)
    # MaximumAttributeByteCount
    max_count = struct.pack(">H", 0xFFFF)
    # AttributeIDList: sequence { uint32 range 0x0000..0xFFFF }
    attr_ids = bytes([0x35, 0x05, 0x0A, 0x00, 0x00, 0xFF, 0xFF])
    cont = b"\x00"
    return make_pdu(0x06, pattern + max_count + attr_ids + cont)


def query(addr):
    s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET,
                      socket.BTPROTO_L2CAP)
    s.settimeout(8)
    s.connect((addr, SDP_PSM))
    s.send(build_request())
    data = s.recv(65535)
    s.close()
    return data


def main():
    req = build_request()
    print(f"请求 PDU ({len(req)} 字节): {req.hex()}")
    print()

    reply = None
    for addr in (BDADDR_LOCAL,):
        try:
            print(f"连接本机 SDP 服务器 {addr}:{SDP_PSM} ...")
            reply = query(addr)
            print(f"  ✓ 收到应答 {len(reply)} 字节")
            break
        except OSError as e:
            print(f"  ✗ 失败: [{e.errno}] {e.strerror}")

    if reply is None:
        print("\n拿不到本机 SDP 应答。可改用另一台蓝牙设备/手机上的 "
              "SDP 浏览器查这台机器。")
        return 1

    print(f"\n应答原始 hex（前 400 字节）:\n{reply[:400].hex()}\n")

    if REPORT_DESCRIPTOR:
        # SDP 里描述符是作为八位组字符串承载的，直接找原始字节序列
        needle = REPORT_DESCRIPTOR[:24]
        if needle in reply:
            print(f"  ✓ 找到报告描述符开头 {needle.hex()}")
            print("     => SDP 发布是正确的，问题在 iOS 侧")
        else:
            print(f"  ✗ 没找到报告描述符开头 {needle.hex()}")
            print("     => 记录里的描述符有问题，需要查编码方式")
            # 退一步：找 hex 文本形式
            if REPORT_DESCRIPTOR.hex().encode() in reply:
                print("     （但找到了 hex 文本形式 —— 说明编码成了字符串）")
    else:
        print("  （未能导入 REPORT_DESCRIPTOR，跳过比对）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
