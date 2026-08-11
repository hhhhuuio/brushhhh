# brushset2abr 使用说明

把 Procreate 的 `.brushset` 笔刷转换成 Photoshop 的 `.abr` 笔刷文件。


## 获取工具

两种方式任选一种：

- **exe 版（推荐，下载即用）**：从 Releases 下载 `brushset2abr.zip`，解压后得到一个 `brushset2abr` 文件夹，里面有 `brushset2abr.exe`、`Images` 文件夹和运行库。
- **源码版**：下载 `brushset2abr.py` 和 `Images` 文件夹，自己运行（见下文“源码运行”）。

## 方式一：exe 版

### 图形界面

1. 双击 `brushset2abr.exe`。
2. 点击“选择 .brushset 文件”，选中要转换的笔刷。
3. 转换完成后，在 brushset 同目录下生成同名 `.abr` 文件，以及“预览汇总”文件夹。

### 命令行

```bat
brushset2abr.exe "笔刷.brushset"
```

指定输出文件：

```bat
brushset2abr.exe "笔刷.brushset" -o "输出.abr"
```

### 拖拽

直接把 `.brushset` 文件拖到 `brushset2abr.exe` 图标上即可转换。

## 方式二：源码运行

### 安装环境

需要 Python 3.9 或更高版本（Windows 官方安装包自带 tkinter，无需额外安装）。

安装 Pillow 依赖：

```bat
pip install Pillow
```

### 运行

把 `brushset2abr.py` 和 `Images` 文件夹放在同一目录，然后执行：

```bat
python brushset2abr.py "笔刷.brushset"
```

指定输出文件：

```bat
python brushset2abr.py "笔刷.brushset" -o "输出.abr"
```

## 参数说明

| 参数 | 作用 |
| --- | --- |
| `-o, --output` | 指定输出 `.abr` 文件路径，不填则生成在 brushset 同目录 |
| `--no-invert` | 所有笔尖和预览图都不反色 |
| `--force-invert` | 所有笔尖和预览图强制反色 |

不填反色参数时，程序会自动判断是否需要反色。

## 转换内容

1. 笔尖图像（Shape）
2. 纹理（Grain，挂到 Photoshop 的“纹理”里）
3. 间距（按 v7 算法开平方换算，1%~1000%）
4. 尺寸 / 角度 / 圆度 / 不透明度 / 流量
5. 压感：最小尺寸、最小不透明度、压力控制（由压感曲线换算）
6. 双重画笔：笔尖、尺寸、角度、圆度、间距、应用模式、数量、散布
7. 纹理：缩放、亮度、对比度、反色，统一“应用到每个笔尖”
8. 笔尖方向（跟随笔画 / 固定角度 / 旋转），自动判断是否反色
9. 笔刷预览图汇总文件夹（预览图与笔尖同步反色）

## 注意事项

- **`Images` 文件夹必须和程序放在一起**：部分 Procreate 笔刷使用内置材质，工具会从 `Images` 文件夹读取。找不到材质时笔刷照常转换，只是不带纹理。
- 笔尖图像超过 1024 像素、纹理超过 512 像素时，会自动缩小，避免文件过大。
- 如果转换过程中有需要留意的项（例如双重画笔模式回退），完成提示里会列出；命令行模式下会打印“提示：”。
- 生成的 `.abr` 可直接双击导入 Photoshop，也可以在 Photoshop 的画笔面板菜单中选择“导入画笔”。
