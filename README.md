# brushset2abr

把 Procreate 的 `.brushset` 笔刷转换成 Photoshop 的 `.abr` 笔刷文件。

当前版本：v15

## 文件说明

- `brushset2abr.py` —— 转换源码（需要 Python 3.9+ 和 Pillow）
- `Images/` —— Procreate 内置材质库，源码运行时的必需依赖
- `brushset2abr/` —— 打包好的 Windows 程序，双击 `brushset2abr.exe` 即用
  （已包含运行库和内置材质库）
- `使用说明.md` —— 详细使用说明
- `LICENSE` —— CC BY-NC 4.0（源自 Brush-Converter 项目，可自行调整）

## 快速开始

**exe 版**：进入 `brushset2abr/` 双击 `brushset2abr.exe`，选择 `.brushset`
文件即可转换，也可直接把笔刷拖到 exe 上。

**源码版**：

```bat
pip install Pillow
python brushset2abr.py "笔刷.brushset"
```

## 转换内容

笔尖图像、纹理（按 Procreate 颗粒设置判断是否“应用到每个笔尖”）、间距、
尺寸/角度/圆度/不透明度/流量、压感、双重画笔、笔尖方向、预览图汇总。
