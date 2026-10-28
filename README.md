# DTBO Tuner

小米 17 Pro Max（popsicle / SM8850）DTBO 充电参数调节 + fastboot 刷入辅助工具。

把官方的 `dtbo` 镜像拖进窗口即可解析、调参，导出可直接 `fastboot flash dtbo_a/_b` 的镜像。
图形界面基于 tkinter，打包为 Windows 独立 exe（通过 GitHub Actions 在 Windows runner 上用 PyInstaller 构建）。

## 功能

- 解析 DTBO 中的 `fragment@33` 充电参数（`jeita_para` / `batt_para_atl` / `mca_charger_thermal` 等 u32 / u32 数组节点）。
- 提供「温和 / 性能 / 自定义」预设，调整充电电流、温度墙、过压保护等。
- 一键导出修改后的 DTBO 镜像，并给出 `fastboot` 刷入命令。
- 支持拖拽文件进入窗口载入（通过原生 ctypes 拖放 + Tk 轮询，避免 GIL 重入崩溃）。

## 目录结构

```
dtbo_tuner.py            主程序（tkinter GUI）
DTBO-Tuner.spec          PyInstaller 打包定义（路径全部相对仓库根，可移植）
tools/platform-tools/    adb / fastboot 等（运行时与打包均依赖）
.github/workflows/       GitHub Actions：打 Windows exe
```

## 本地运行（源码）

需要 Windows + Python 3.12（自带 tkinter）：

```powershell
python dtbo_tuner.py --gui
```

## 本地打包（Windows）

```powershell
python -m pip install pyinstaller
pyinstaller DTBO-Tuner.spec --noconfirm --distpath dist --workpath build
# 产物在 dist/DTBO-Tuner/（DTBO-Tuner.exe + _internal/）
```

## 通过 GitHub Actions 构建

- 推送 `v*` 标签（如 `v1.0.0`）会触发构建，并在 Release 中上传 `DTBO-Tuner.zip`。
- 也可在 Actions 页面手动 `Run workflow`（artifacts 形式下载）。

## 刷入示例

```text
fastboot flash dtbo_a dtbo_tuner_out.img
fastboot reboot
```

> 注意：修改充电参数可能影响设备安全与保修，请确认数值在硬件允许范围内（界面会标注危险值与建议范围）。
