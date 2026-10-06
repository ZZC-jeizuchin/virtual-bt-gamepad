# virtual-bt-gamepad

把一台 Linux 电脑变成**蓝牙游戏手柄**——不需要额外的硬件，不用买手柄。

用 Python + BlueZ 实现，提供 **BLE (HID over GATT)** 与 **经典蓝牙 HID** 两条路线。
主机侧支持 Android / Windows / macOS / Linux。

> **English**: Turn a Linux PC into a Bluetooth gamepad with no extra hardware.
> Two backends: BLE HID over GATT (no root) and Classic Bluetooth HID (root).
> Hosts: Android, Windows, macOS, Linux. **iOS is not supported** — see
> [为什么 iOS 不行](#为什么-ios-不行).

---

## 功能

* 16 个按钮 + 方向键（标准 Hat switch）+ 左右摇杆 + 模拟扳机
* Tkinter 图形界面，鼠标点按 / 拖动即可（也方便做成自动化输入源）
* 报告描述符与报告格式均按 HID 规范编写，经过内核 `hid-generic` 解析验证
* BLE 版**不需要 root**；经典版需要 root（HIDP 的 PSM 是特权端口）

## 两条路线怎么选

| | `ble_gamepad.py` | `classic_gamepad.py` |
|---|---|---|
| 传输 | BLE / HID over GATT (HOGP) | 经典蓝牙 / HID Profile (HIDP) |
| 需要 root | ❌ 不需要 | ✅ 需要（PSM 0x11/0x13 是特权端口） |
| 需要改系统配置 | ❌ 不需要 | ✅ 需要（见下） |
| 依赖 | `bluez-peripheral` + `dbus-next` | `dbus-next`（HIDP 服务端自己实现） |
| 主机兼容 | Android / Windows / macOS / Linux | Android / Windows / Linux |
| 推荐度 | ⭐ **首选，装好就能跑** | 需要 root 时的备选；也能伪装成 Xbox 手柄 |

---

## 安装

```bash
sudo apt install python3-tk bluez
pip install -r requirements.txt
```

> ⚠️ **注意库版本**：`bluez-peripheral` 有两个不兼容的大版本。
> 本项目的 BLE 版是为 **0.1.x**（使用 `dbus-next`）写的，API 是
> `from bluez_peripheral.util import Adapter`。
> 0.2.x 改用了 `dbus-fast` 且模块布局不同（`bluez_peripheral.adapter`），直接跑会
> `ImportError`。如果系统里同时装了多份，Python 会优先加载 user-site 的那份，
> 用 `python3 -c "import bluez_peripheral; print(bluez_peripheral.__file__)"` 确认。

---

## 用法

### BLE 版（推荐，免 root）

```bash
python3 ble_gamepad.py
```

运行后窗口出现，在**主机**（手机/电脑）的蓝牙设置里搜索并配对
`Linux Gamepad`，然后点窗口里的按钮 / 拖动摇杆即可。

```bash
# 可选：某些双模主机（如 iPhone）会抓着经典蓝牙链路不放，导致 LE 连不上。
# 把该主机的首选承载强制为 LE：
GAMEPAD_FORCE_LE=AA:BB:CC:DD:EE:FF python3 ble_gamepad.py
```

### 经典蓝牙 HID 版（需要 root）

经典版需要两处一次性系统配置：

```bash
# 1) 让出 HID 的 UUID 和 PnP(DID) 记录 —— BlueZ 默认把它们占给了 HID 主机角色
sudo mkdir -p /etc/systemd/system/bluetooth.service.d
sudo tee /etc/systemd/system/bluetooth.service.d/override.conf >/dev/null <<'EOF'
[Service]
ExecStart=
ExecStart=/usr/libexec/bluetooth/bluetoothd --noplugin=input,deviceinfo
EOF
sudo systemctl daemon-reload && sudo systemctl restart bluetooth

# 2) 给 python 授 CAP_NET_BIND_SERVICE —— PSM 0x11/0x13 小于 0x1000 属特权端口
sudo setcap cap_net_bind_service=+ep "$(readlink -f "$(which python3)")"
```

然后：

```bash
# 设备类设成 Peripheral/Gamepad（main.conf 的 Class 在 BlueZ 5.85 上不生效）
sudo hciconfig hci0 class 0x000508

python3 classic_gamepad.py
```

程序会自动：发布 HID 服务记录 + PnP(VID/PID) 记录、监听 PSM 0x11/0x13、
把适配器改名为 `Xbox Wireless Controller` 并设为可发现/可配对。

> 经典版默认伪装成 **Xbox One S**（VID:PID `045E:02FD`、版本 `0903`、
> 334 字节原厂报告描述符、Xbox 原生 16 字节报告格式）。
> 在 Android 上会被识别为 Xbox 手柄；这是刻意的选择，见下面的 iOS 章节。

**用完想恢复系统：**

```bash
sudo rm -f /etc/systemd/system/bluetooth.service.d/override.conf
sudo systemctl daemon-reload && sudo systemctl restart bluetooth
sudo setcap -r "$(readlink -f "$(which python3)")"
bluetoothctl reset-alias
sudo hciconfig hci0 class 0x00010c
```

---

## 兼容性

| 主机 | BLE 版 | 经典版 | 备注 |
|---|---|---|---|
| **Android** | ✅ | ✅ | 推荐 Android 10+；系统会把通用 HID 手柄映射为标准 Gamepad |
| Linux | ✅ | ✅ | 可用 `jstest` / `evtest` / `hid-recorder` 验证 |
| Windows 10/11 | ✅ | ✅ | 通用 HID 手柄 |
| macOS | ✅ | ⚠️ | BLE 走 `GCController`；经典 HID 支持不如 BLE |
| **iOS / iPadOS** | ❌ | ❌ | 见下 |

### 为什么 iOS 不行

这是本项目刻意记录的**失败结论**，供后来者少走弯路。

iOS 的 GameController 框架**只支持 MFi 认证手柄和一份很短的官方白名单**
（iOS 13 起加入 PS4 DualShock 4 与 Xbox One S；iOS 14.5 起加入 Xbox Series X
与 Elite 2），并且**按 VID/PID 匹配**。通用 HID 手柄不在支持范围内。

我们把 iPhone 这条路径推到了当前环境能做到的极限，逐层验证过：

| 环节 | 结果 |
|---|---|
| BLE HID 服务注册 + iOS 订阅输入报告 | ✅ iOS 会订阅 |
| 经典 HIDP 服务端（自建，BlueZ 没有该角色） | ✅ 两条 L2CAP 通道由 iPhone 主动连接 |
| SDP 服务记录（0x1124 + 控制通道 0x0011 + 中断通道 0x0013） | ✅ iOS 完整解析 |
| HID 设备创建 | ✅ iOS 主动下发 `SET_PROTOCOL / Report`（`0x71`） |
| 报告传输 | ✅ 轴/按钮实时送达 |
| 伪装成 Xbox One S：VID/PID `045E:02FD`、版本 `0903`、**334 字节原厂描述符**、Xbox 原生报告格式 | ✅ 与真机逐字节对齐 |
| **被 GameController 归类为手柄** | ❌ **始终不成立** |

结论：**iOS 的识别判据不止 VID/PID 与报告描述符**，还有我们无法从外部观测的
一层（很可能依赖真机的厂商特定响应或认证流程）。没有真机对照时，无法复现。

> 如果你手上有真的 Xbox One S 手柄，把它的**完整 SDP 记录** dump 出来与本项目
> 生成的记录逐条对比，是最有希望突破的方向。

---

## 项目结构

```
.
├── ble_gamepad.py          # BLE HID over GATT（免 root，推荐）
├── classic_gamepad.py      # 经典蓝牙 HID + Xbox 伪装（需 root）
├── hid_descriptors/
│   ├── xbox_one_s.bin          # 334 字节 Xbox One S 原厂报告描述符
│   └── xbox_one_s_306B_older.bin
├── tools/
│   ├── l2cap_check.py      # 检查能否绑定 HIDP 的 PSM 0x11/0x13
│   ├── sdp_check.py        # 检查能否通过 ProfileManager1 发布 HID SDP 记录
│   ├── sdp_query.py        # 读回本机 SDP 记录（验证描述符是否发布成功）
│   └── legacy_uhid_demo.py # 早期 /dev/uhid 方案（对经典蓝牙无效，留作参考）
├── requirements.txt
└── README.md
```

## 实现要点（踩过的坑）

1. **BlueZ 没有 HID 设备（device/server）角色**，它只实现 HID 主机。
   想做蓝牙 HID 设备，只能用 `ProfileManager1.RegisterProfile` 发布 SDP 记录，
   再自己用原始 L2CAP socket 监听 PSM `0x11` / `0x13`。
   见 [bluez#1168](https://github.com/bluez/bluez/issues/1168)。
2. HID 的 UUID `0x1124` 和 PnP 的 `0x1200` 默认被 BlueZ 占着，
   必须 `--noplugin=input,deviceinfo` 让出来，否则报 `UUID already registered`。
3. **控制通道写在 SDP 的 `0x0004`，中断通道必须写在 `0x000d`
   （AdditionalProtocolDescriptorList）**，不能都塞进 `0x0004`。
   这是 HID Profile 的硬性要求，写错会导致主机建不出 HID 设备。
4. HIDP 事务头：**高 4 位类型、低 4 位参数**（`0xA1` = DATA|Input）。
   依据 Linux 内核 `net/bluetooth/hidp/core.c` 的
   `HIDP_HEADER_TRANS_MASK 0xf0` / `HIDP_HEADER_PARAM_MASK 0x0f`。
5. `/dev/uhid` 只能用于 **BLE** 的 HID 设备；对经典蓝牙无效。
6. `main.conf` 的 `Class` 选项在 BlueZ 5.85 上不生效，需要 `hciconfig hci0 class`。

## 参考

* [bluez/bluez#1168 — HID Device Compatibility (peripheral)](https://github.com/bluez/bluez/issues/1168)
* [Alkaid-Benetnash/EmuBTHID](https://github.com/Alkaid-Benetnash/EmuBTHID) — 经典蓝牙 HID 设备（键鼠）可参考实现
* [007durgesh219/BTGamepad](https://github.com/007durgesh219/BTGamepad) — 本项目的 SDP 记录结构参照了它
* [atar-axis/xpadneo#624](https://github.com/atar-axis/xpadneo/issues/624) — Xbox One S 报告描述符的权威 dump
* [Bluetooth HID Profile 1.1.1](https://www.bluetooth.com/specifications/specs/hid-profile-1-1-1/)

## License

MIT
