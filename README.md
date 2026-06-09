# 智能车竞赛 · 完全模型组车体视频上传与数据标注平台

基于 Flask 的一体化平台，覆盖 **视频上传 → 批量切分 → 数据集导出 → 数据标注** 的完整流程。白色简洁风格，部署在 `0.0.0.0:8085`。

## 功能概览

### 用户端（`/`）
- 白色风格的视频上传界面，支持拖拽上传
- 单文件最大 **600MB**
- **最多保留一个视频**，再次上传自动替换
- 上传后可在线预览当前视频

### 管理员端（`/admin`）
- 独立登录入口（`/admin/login`），账号 **hurry** / 密码 **123321**
- 查看所有已上传视频（大小、时长、切分状态等）
- **批量切分**：可设置间隔时长（秒/帧），一键切分全部视频为图像帧
- 单个视频切分、实时切分进度
- **一键导出图像数据集**：打包所有图像为 zip（`images/` 文件夹）
- 导出图像 + 标注的完整数据集（含 LabelMe 风格 JSON 与 `labels.txt`）

### 数据标注平台（`/admin/annotate`，类 LabelMe）
- HTML5 Canvas 标注引擎，支持 **矩形、多边形、关键点、线段**
- 自定义标签管理（增删、配色），标注选定标签
- 跨所有视频的图像列表，支持按「全部 / 未标注 / 已标注」筛选
- 形状列表、选中/移动/删除、顶点拖拽编辑
- 缩放（滚轮/按钮/适应）、平移（右键拖动）
- 丰富快捷键：`V/R/P/O/L` 切换工具、`Ctrl+S` 保存、`←/→` 切换图像、`Del` 删除、`Enter` 完成多边形、`Esc` 取消
- 标注以 LabelMe 风格 JSON 持久化

## 环境要求
- Python 3.10+
- [FFmpeg](https://ffmpeg.org/)（用于视频切分，需在 `PATH` 中可用）

## 安装与运行

```bash
pip install -r requirements.txt
python app.py
```

访问：
- 用户端：http://localhost:8085/
- 管理员登录：http://localhost:8085/admin/login

## 目录结构
```
app.py                 # Flask 应用与全部路由
templates/             # 页面模板
static/css/style.css   # 样式（白色风格）
static/js/annotate.js  # 标注引擎
uploads/               # 上传的视频（运行时生成，已 gitignore）
frames/                # 切分出的图像（运行时生成）
annotations/           # 标注 JSON（运行时生成）
data/                  # 元数据（videos.json / labels.json）
```

## 说明
- 视频切分使用 FFmpeg 的 `fps=1/间隔` 抽帧，输出 JPG。
- 管理员密钥可通过环境变量 `SMARTCAR_SECRET_KEY` 配置 session 加密。
