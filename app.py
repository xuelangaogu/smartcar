"""智能车竞赛 - 完全模型组车体视频上传与数据标注平台.

功能模块:
- 用户端: 上传视频 (最大 600M, 最多一个, 可替换), 白色风格
- 管理员端: 登录 (hurry / 123321), 查看所有视频, 批量切分, 导出图像数据集
- 标注平台: 类 LabelMe 的全功能数据标注工具 (矩形/多边形/点/线, 自定义标签)

部署: 0.0.0.0:8085
"""

import json
import os
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
from datetime import datetime
from functools import wraps
from io import BytesIO

from flask import (
    Flask,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
FRAMES_DIR = os.path.join(BASE_DIR, "frames")
ANNOT_DIR = os.path.join(BASE_DIR, "annotations")
DATA_DIR = os.path.join(BASE_DIR, "data")

VIDEOS_JSON = os.path.join(DATA_DIR, "videos.json")
LABELS_JSON = os.path.join(DATA_DIR, "labels.json")

MAX_CONTENT_LENGTH = 600 * 1024 * 1024  # 600 MB
ALLOWED_EXT = {".mp4", ".avi", ".mov", ".mkv", ".flv", ".wmv", ".webm", ".m4v", ".mpeg", ".mpg"}

ADMIN_USERNAME = "hurry"
ADMIN_PASSWORD = "123321"

DEFAULT_LABELS = [
    {"name": "赛道", "color": "#e6194b"},
    {"name": "斑马线", "color": "#3cb44b"},
    {"name": "障碍物", "color": "#4363d8"},
    {"name": "锥桶", "color": "#f58231"},
    {"name": "车辆", "color": "#911eb4"},
]

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
app.secret_key = os.environ.get("SMARTCAR_SECRET_KEY", "smartcar-secret-key-change-me")

# 切分任务进度 (内存中, 简单全局状态)
split_progress = {"running": False, "total": 0, "done": 0, "message": "", "current": ""}
split_lock = threading.Lock()


# ----------------------------------------------------------------------------
# 存储辅助函数
# ----------------------------------------------------------------------------
def ensure_dirs():
    for d in (UPLOAD_DIR, FRAMES_DIR, ANNOT_DIR, DATA_DIR):
        os.makedirs(d, exist_ok=True)


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_videos():
    return load_json(VIDEOS_JSON, {})


def save_videos(videos):
    save_json(VIDEOS_JSON, videos)


def load_labels():
    labels = load_json(LABELS_JSON, None)
    if labels is None:
        save_json(LABELS_JSON, DEFAULT_LABELS)
        return list(DEFAULT_LABELS)
    return labels


def save_labels(labels):
    save_json(LABELS_JSON, labels)


def human_size(num):
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num) < 1024.0:
            return f"{num:3.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} TB"


def get_ffmpeg():
    return shutil.which("ffmpeg") or "ffmpeg"


def get_ffprobe():
    return shutil.which("ffprobe") or "ffprobe"


def probe_duration(path):
    """返回视频时长(秒), 失败返回 None。"""
    try:
        out = subprocess.run(
            [
                get_ffprobe(),
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return float(out.stdout.strip())
    except (subprocess.SubprocessError, ValueError):
        return None


def frames_for(video_id):
    """返回某视频的帧文件名列表 (已排序)。"""
    d = os.path.join(FRAMES_DIR, video_id)
    if not os.path.isdir(d):
        return []
    return sorted(f for f in os.listdir(d) if f.lower().endswith((".jpg", ".jpeg", ".png")))


# ----------------------------------------------------------------------------
# 认证
# ----------------------------------------------------------------------------
def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "未授权"}), 401
            return redirect(url_for("admin_login", next=request.path))
        return f(*args, **kwargs)

    return wrapper


# ----------------------------------------------------------------------------
# 用户端
# ----------------------------------------------------------------------------
@app.route("/")
def index():
    videos = load_videos()
    # 用户端: 展示当前(最近)的一个视频
    current = None
    if videos:
        current = sorted(videos.values(), key=lambda v: v["uploaded_at"], reverse=True)[0]
        current = dict(current)
        current["size_h"] = human_size(current["size"])
    return render_template("index.html", current=current)


@app.route("/upload", methods=["POST"])
def upload():
    file = request.files.get("video")
    if not file or file.filename == "":
        return jsonify({"error": "未选择文件"}), 400

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_EXT:
        return jsonify({"error": f"不支持的格式 {ext}, 请上传视频文件"}), 400

    ensure_dirs()
    videos = load_videos()

    # 最多一个视频: 替换 -> 删除旧视频及其帧、标注
    for vid, info in list(videos.items()):
        old_path = os.path.join(UPLOAD_DIR, info["stored_name"])
        if os.path.exists(old_path):
            os.remove(old_path)
        shutil.rmtree(os.path.join(FRAMES_DIR, vid), ignore_errors=True)
        shutil.rmtree(os.path.join(ANNOT_DIR, vid), ignore_errors=True)
    videos = {}

    video_id = uuid.uuid4().hex[:12]
    safe = secure_filename(file.filename) or "video"
    stored_name = f"{video_id}_{safe}"
    save_path = os.path.join(UPLOAD_DIR, stored_name)
    file.save(save_path)

    size = os.path.getsize(save_path)
    duration = probe_duration(save_path)
    videos[video_id] = {
        "id": video_id,
        "original_name": file.filename,
        "stored_name": stored_name,
        "size": size,
        "duration": duration,
        "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "frame_count": 0,
        "interval": None,
    }
    save_videos(videos)
    return jsonify({"ok": True, "video_id": video_id, "redirect": url_for("index")})


@app.route("/media/<video_id>")
def media(video_id):
    videos = load_videos()
    info = videos.get(video_id)
    if not info:
        abort(404)
    return send_from_directory(UPLOAD_DIR, info["stored_name"])


# ----------------------------------------------------------------------------
# 管理员端
# ----------------------------------------------------------------------------
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session["is_admin"] = True
            nxt = request.args.get("next") or url_for("admin_dashboard")
            return redirect(nxt)
        error = "用户名或密码错误"
    return render_template("admin_login.html", error=error)


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin_login"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    videos = load_videos()
    items = []
    for v in sorted(videos.values(), key=lambda x: x["uploaded_at"], reverse=True):
        item = dict(v)
        item["size_h"] = human_size(v["size"])
        item["frame_count"] = len(frames_for(v["id"]))
        items.append(item)
    total_frames = sum(i["frame_count"] for i in items)
    return render_template("admin_dashboard.html", videos=items, total_frames=total_frames)


def _split_video(video_id, info, interval):
    """对单个视频按间隔(秒)切分为帧。"""
    out_dir = os.path.join(FRAMES_DIR, video_id)
    shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir, exist_ok=True)
    src = os.path.join(UPLOAD_DIR, info["stored_name"])
    fps_expr = f"1/{interval}" if interval >= 1 else str(round(1.0 / interval, 6))
    pattern = os.path.join(out_dir, "frame_%05d.jpg")
    cmd = [
        get_ffmpeg(),
        "-i",
        src,
        "-vf",
        f"fps={fps_expr}",
        "-q:v",
        "2",
        "-start_number",
        "1",
        pattern,
        "-y",
    ]
    subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    return len(frames_for(video_id))


def _run_batch_split(video_ids, interval):
    global split_progress
    videos = load_videos()
    with split_lock:
        split_progress = {
            "running": True,
            "total": len(video_ids),
            "done": 0,
            "message": "开始切分...",
            "current": "",
        }
    for vid in video_ids:
        info = videos.get(vid)
        if not info:
            with split_lock:
                split_progress["done"] += 1
            continue
        with split_lock:
            split_progress["current"] = info["original_name"]
            split_progress["message"] = f"正在切分: {info['original_name']}"
        count = _split_video(vid, info, interval)
        videos = load_videos()
        if vid in videos:
            videos[vid]["frame_count"] = count
            videos[vid]["interval"] = interval
            save_videos(videos)
        with split_lock:
            split_progress["done"] += 1
    with split_lock:
        split_progress["running"] = False
        split_progress["message"] = "切分完成"
        split_progress["current"] = ""


@app.route("/admin/split", methods=["POST"])
@admin_required
def admin_split():
    if split_progress["running"]:
        return jsonify({"error": "已有切分任务进行中"}), 409
    try:
        interval = float(request.form.get("interval", "1"))
    except ValueError:
        return jsonify({"error": "间隔时长无效"}), 400
    if interval <= 0:
        return jsonify({"error": "间隔时长必须大于 0"}), 400

    scope = request.form.get("scope", "all")  # all 或 单个 video_id
    videos = load_videos()
    if scope == "all":
        video_ids = list(videos.keys())
    else:
        video_ids = [scope] if scope in videos else []
    if not video_ids:
        return jsonify({"error": "没有可切分的视频"}), 400

    t = threading.Thread(target=_run_batch_split, args=(video_ids, interval), daemon=True)
    t.start()
    return jsonify({"ok": True, "total": len(video_ids)})


@app.route("/admin/split/progress")
@admin_required
def admin_split_progress():
    with split_lock:
        return jsonify(dict(split_progress))


@app.route("/admin/frames/<video_id>/<path:frame>")
@admin_required
def admin_frame(video_id, frame):
    return send_from_directory(os.path.join(FRAMES_DIR, video_id), frame)


@app.route("/admin/export/images")
@admin_required
def admin_export_images():
    """导出整个图像数据集为 zip (一个文件夹, 包含所有图像)。"""
    videos = load_videos()
    mem = BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as zf:
        for vid, info in videos.items():
            base = os.path.splitext(info["original_name"])[0]
            base = secure_filename(base) or vid
            for frame in frames_for(vid):
                src = os.path.join(FRAMES_DIR, vid, frame)
                arcname = os.path.join("images", f"{base}__{frame}")
                zf.write(src, arcname)
    mem.seek(0)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(
        mem,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"smartcar_images_{ts}.zip",
    )


@app.route("/admin/export/dataset")
@admin_required
def admin_export_dataset():
    """导出图像 + 标注 (LabelMe 风格 json) 的完整数据集。"""
    videos = load_videos()
    mem = BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as zf:
        for vid, info in videos.items():
            base = os.path.splitext(info["original_name"])[0]
            base = secure_filename(base) or vid
            for frame in frames_for(vid):
                src = os.path.join(FRAMES_DIR, vid, frame)
                stem = os.path.splitext(frame)[0]
                zf.write(src, os.path.join("images", f"{base}__{frame}"))
                ann_path = os.path.join(ANNOT_DIR, vid, stem + ".json")
                if os.path.exists(ann_path):
                    zf.write(ann_path, os.path.join("annotations", f"{base}__{stem}.json"))
        zf.writestr("labels.txt", "\n".join(label["name"] for label in load_labels()))
    mem.seek(0)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(
        mem,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"smartcar_dataset_{ts}.zip",
    )


# ----------------------------------------------------------------------------
# 标注平台 (类 LabelMe)
# ----------------------------------------------------------------------------
@app.route("/admin/annotate")
@admin_required
def annotate():
    return render_template("annotate.html")


@app.route("/api/frames")
@admin_required
def api_frames():
    """返回所有可标注的帧 (跨所有视频)。"""
    videos = load_videos()
    result = []
    for v in sorted(videos.values(), key=lambda x: x["uploaded_at"]):
        vid = v["id"]
        frame_list = frames_for(vid)
        for frame in frame_list:
            stem = os.path.splitext(frame)[0]
            ann_path = os.path.join(ANNOT_DIR, vid, stem + ".json")
            annotated = os.path.exists(ann_path)
            n_shapes = 0
            if annotated:
                data = load_json(ann_path, {})
                n_shapes = len(data.get("shapes", []))
            result.append(
                {
                    "video_id": vid,
                    "video_name": v["original_name"],
                    "frame": frame,
                    "url": url_for("admin_frame", video_id=vid, frame=frame),
                    "annotated": annotated and n_shapes > 0,
                    "shapes": n_shapes,
                }
            )
    return jsonify({"frames": result, "count": len(result)})


@app.route("/api/labels", methods=["GET", "POST", "DELETE"])
@admin_required
def api_labels():
    labels = load_labels()
    if request.method == "GET":
        return jsonify(labels)
    data = request.get_json(silent=True) or {}
    if request.method == "POST":
        name = (data.get("name") or "").strip()
        color = data.get("color") or "#e6194b"
        if not name:
            return jsonify({"error": "标签名不能为空"}), 400
        if any(label["name"] == name for label in labels):
            return jsonify({"error": "标签已存在"}), 400
        labels.append({"name": name, "color": color})
        save_labels(labels)
        return jsonify(labels)
    # DELETE
    name = (data.get("name") or "").strip()
    labels = [label for label in labels if label["name"] != name]
    save_labels(labels)
    return jsonify(labels)


@app.route("/api/annotation/<video_id>/<frame>", methods=["GET", "POST"])
@admin_required
def api_annotation(video_id, frame):
    videos = load_videos()
    if video_id not in videos:
        return jsonify({"error": "视频不存在"}), 404
    stem = os.path.splitext(frame)[0]
    ann_dir = os.path.join(ANNOT_DIR, video_id)
    ann_path = os.path.join(ann_dir, stem + ".json")
    if request.method == "GET":
        data = load_json(ann_path, None)
        if data is None:
            data = {
                "version": "1.0",
                "videoId": video_id,
                "imagePath": frame,
                "shapes": [],
            }
        return jsonify(data)
    # POST 保存
    data = request.get_json(silent=True) or {}
    os.makedirs(ann_dir, exist_ok=True)
    payload = {
        "version": "1.0",
        "videoId": video_id,
        "imagePath": frame,
        "imageWidth": data.get("imageWidth"),
        "imageHeight": data.get("imageHeight"),
        "shapes": data.get("shapes", []),
        "updatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_json(ann_path, payload)
    return jsonify({"ok": True})


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    ensure_dirs()
    load_labels()
    app.run(host="0.0.0.0", port=8085, debug=False, threaded=True)
