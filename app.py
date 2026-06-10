"""智能车竞赛 - 完全模型组车体视频上传与数据标注平台.

功能模块:
- 用户端: 队伍注册/登录 (队伍名称 + 学校名称作为凭证), 每队上传一个视频 (≤600M, 可替换), 持久化保存
- 管理员端: 登录 (hurry / 123321), 查看所有队伍视频, 批量切分, 导出图像数据集
- 标注平台: 类 LabelMe 的全功能数据标注工具 (矩形/多边形/点/线, 自定义标签)

视频解码/切分使用 OpenCV (其 wheel 自带解码器), 无需系统安装 ffmpeg。

部署: 0.0.0.0:8085
"""

import base64
import json
import os
import random
import shutil
import threading
import uuid
import zipfile
from datetime import datetime
from functools import wraps
from io import BytesIO

import cv2
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
from PIL import Image, ImageDraw, ImageFont
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
FRAMES_DIR = os.path.join(BASE_DIR, "frames")
ANNOT_DIR = os.path.join(BASE_DIR, "annotations")
DATA_DIR = os.path.join(BASE_DIR, "data")

VIDEOS_JSON = os.path.join(DATA_DIR, "videos.json")
LABELS_JSON = os.path.join(DATA_DIR, "labels.json")
TEAMS_JSON = os.path.join(DATA_DIR, "teams.json")

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
    ensure_dirs()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_videos():
    return load_json(VIDEOS_JSON, {})


def save_videos(videos):
    save_json(VIDEOS_JSON, videos)


def load_teams():
    return load_json(TEAMS_JSON, {})


def save_teams(teams):
    save_json(TEAMS_JSON, teams)


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


def probe_duration(path):
    """用 OpenCV 返回视频时长(秒), 失败返回 None。"""
    try:
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            return None
        fps = cap.get(cv2.CAP_PROP_FPS) or 0
        n = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        cap.release()
        if fps > 0 and n > 0:
            return round(n / fps, 2)
        return None
    except cv2.error:
        return None


def frames_for(video_id):
    """返回某视频的帧文件名列表 (已排序)。"""
    d = os.path.join(FRAMES_DIR, video_id)
    if not os.path.isdir(d):
        return []
    return sorted(f for f in os.listdir(d) if f.lower().endswith((".jpg", ".jpeg", ".png")))


def team_video(videos, team_id):
    """返回某队伍当前的视频信息 (没有则 None)。"""
    for info in videos.values():
        if info.get("team_id") == team_id:
            return info
    return None


def purge_video(videos, video_id):
    """删除视频文件、帧、标注及元数据条目。"""
    info = videos.get(video_id)
    if not info:
        return
    old_path = os.path.join(UPLOAD_DIR, info["stored_name"])
    if os.path.exists(old_path):
        os.remove(old_path)
    shutil.rmtree(os.path.join(FRAMES_DIR, video_id), ignore_errors=True)
    shutil.rmtree(os.path.join(ANNOT_DIR, video_id), ignore_errors=True)
    videos.pop(video_id, None)


# ----------------------------------------------------------------------------
# 验证码 (字母数字, 每次登录校验)
# ----------------------------------------------------------------------------
CAPTCHA_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 去除易混淆字符 0O1IL
CAPTCHA_LEN = 4


def _load_captcha_font(size):
    for path in (
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _render_captcha(code):
    """生成验证码 PNG 图像 (字母数字 + 干扰线/噪点)。"""
    width, height = 130, 48
    img = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    font = _load_captcha_font(30)
    for _ in range(6):
        x1, y1 = random.randint(0, width), random.randint(0, height)
        x2, y2 = random.randint(0, width), random.randint(0, height)
        draw.line([(x1, y1), (x2, y2)], fill=(random.randint(160, 220),) * 3, width=1)
    for _ in range(120):
        draw.point(
            (random.randint(0, width), random.randint(0, height)),
            fill=(random.randint(150, 210),) * 3,
        )
    step = width // (CAPTCHA_LEN + 1)
    for i, ch in enumerate(code):
        color = (random.randint(0, 90), random.randint(0, 90), random.randint(90, 180))
        y = random.randint(2, 10)
        draw.text((8 + i * step, y), ch, font=font, fill=color)
    buf = BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return buf


def gen_captcha_uri():
    """生成验证码, 写入 session, 并以内嵌 data URI 形式返回 PNG。

    验证码图像与页面渲染绑定 (内嵌而非独立 URL), 避免图片被浏览器/代理重复请求
    导致 session 中的验证码与页面显示不一致。
    """
    code = "".join(random.choice(CAPTCHA_CHARS) for _ in range(CAPTCHA_LEN))
    session["captcha"] = code.upper()
    b64 = base64.b64encode(_render_captcha(code).getvalue()).decode("ascii")
    return "data:image/png;base64," + b64


@app.route("/captcha")
def captcha():
    """按需刷新验证码 (仅在用户点击刷新时调用), 返回新的 data URI。"""
    return jsonify({"img": gen_captcha_uri()})


def check_captcha(value):
    """校验用户输入的验证码 (不区分大小写), 校验后立即失效。"""
    expected = session.pop("captcha", None)
    if not expected:
        return False
    return (value or "").strip().upper() == expected


def render_auth(**kwargs):
    """渲染队伍登录/注册页, 始终内嵌一个新的验证码。"""
    kwargs.setdefault("captcha_img", gen_captcha_uri())
    return render_template("auth.html", **kwargs)


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


def team_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("team_id"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return wrapper


# ----------------------------------------------------------------------------
# 队伍注册 / 登录 (用户端)
# ----------------------------------------------------------------------------
@app.route("/register", methods=["POST"])
def register():
    team_name = (request.form.get("team_name") or "").strip()
    school = (request.form.get("school") or "").strip()
    if not team_name or not school:
        return render_auth(reg_error="队伍名称和学校名称不能为空",
                            team_name=team_name, school=school, tab="register")
    teams = load_teams()
    for t in teams.values():
        if t["team_name"] == team_name and t["school"] == school:
            return render_auth(reg_error="该队伍 (队名+学校) 已注册, 请直接登录",
                                team_name=team_name, school=school, tab="register")
    team_id = uuid.uuid4().hex[:12]
    teams[team_id] = {
        "id": team_id,
        "team_name": team_name,
        "school": school,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_teams(teams)
    session["team_id"] = team_id
    session["team_name"] = team_name
    return redirect(url_for("index"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        team_name = (request.form.get("team_name") or "").strip()
        school = (request.form.get("school") or "").strip()
        if not check_captcha(request.form.get("captcha")):
            return render_auth(login_error="验证码错误, 请重新输入",
                                team_name=team_name, school=school, tab="login")
        teams = load_teams()
        match = None
        for t in teams.values():
            if t["team_name"] == team_name and t["school"] == school:
                match = t
                break
        if not match:
            return render_auth(login_error="队伍不存在, 请先注册或检查队名/学校",
                                team_name=team_name, school=school, tab="login")
        session["team_id"] = match["id"]
        session["team_name"] = match["team_name"]
        return redirect(url_for("index"))
    if session.get("team_id"):
        return redirect(url_for("index"))
    return render_auth(tab="login")


@app.route("/logout")
def logout():
    session.pop("team_id", None)
    session.pop("team_name", None)
    return redirect(url_for("login"))


# ----------------------------------------------------------------------------
# 用户端
# ----------------------------------------------------------------------------
@app.route("/")
@team_required
def index():
    teams = load_teams()
    team = teams.get(session["team_id"])
    if not team:
        session.clear()
        return redirect(url_for("login"))
    videos = load_videos()
    current = team_video(videos, team["id"])
    if current:
        current = dict(current)
        current["size_h"] = human_size(current["size"])
        current["frame_count"] = len(frames_for(current["id"]))
    return render_template("index.html", current=current, team=team)


@app.route("/upload", methods=["POST"])
@team_required
def upload():
    file = request.files.get("video")
    if not file or file.filename == "":
        return jsonify({"error": "未选择文件"}), 400

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_EXT:
        return jsonify({"error": f"不支持的格式 {ext}, 请上传视频文件"}), 400

    ensure_dirs()
    team_id = session["team_id"]
    teams = load_teams()
    team = teams.get(team_id)
    if not team:
        return jsonify({"error": "队伍信息失效, 请重新登录"}), 401

    videos = load_videos()
    # 每队最多一个视频: 替换 -> 删除该队旧视频及其帧、标注
    existing = team_video(videos, team_id)
    if existing:
        purge_video(videos, existing["id"])

    video_id = uuid.uuid4().hex[:12]
    safe = secure_filename(file.filename) or "video"
    stored_name = f"{video_id}_{safe}"
    save_path = os.path.join(UPLOAD_DIR, stored_name)
    file.save(save_path)

    size = os.path.getsize(save_path)
    duration = probe_duration(save_path)
    videos[video_id] = {
        "id": video_id,
        "team_id": team_id,
        "team_name": team["team_name"],
        "school": team["school"],
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
    if not (session.get("is_admin") or session.get("team_id")):
        abort(403)
    videos = load_videos()
    info = videos.get(video_id)
    if not info:
        abort(404)
    # 队伍仅能访问自己的视频
    if not session.get("is_admin") and info.get("team_id") != session.get("team_id"):
        abort(403)
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
        if not check_captcha(request.form.get("captcha")):
            error = "验证码错误, 请重新输入"
        elif username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session["is_admin"] = True
            nxt = request.args.get("next") or url_for("admin_dashboard")
            return redirect(nxt)
        else:
            error = "用户名或密码错误"
    return render_template("admin_login.html", error=error, captcha_img=gen_captcha_uri())


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
    teams = load_teams()
    return render_template(
        "admin_dashboard.html", videos=items, total_frames=total_frames, team_count=len(teams)
    )


def _split_video(video_id, info, interval):
    """用 OpenCV 按间隔(秒)将视频切分为帧。"""
    out_dir = os.path.join(FRAMES_DIR, video_id)
    shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir, exist_ok=True)
    src = os.path.join(UPLOAD_DIR, info["stored_name"])
    cap = cv2.VideoCapture(src)
    if not cap.isOpened():
        return 0
    fps = cap.get(cv2.CAP_PROP_FPS) or 0
    if fps <= 0:
        fps = 30.0
    step = max(1, int(round(fps * interval)))
    idx = 0
    saved = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if idx % step == 0:
            saved += 1
            ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
            if ok:
                with open(os.path.join(out_dir, f"frame_{saved:05d}.jpg"), "wb") as fh:
                    fh.write(buf.tobytes())
        idx += 1
    cap.release()
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


def safe_name(s):
    """清理文件/路径不安全字符, 但保留中文等 unicode 字符。"""
    s = (s or "").strip()
    for ch in '\\/:*?"<>|':
        s = s.replace(ch, "_")
    s = s.replace("..", "_")
    return s or "x"


def _export_base(info, vid):
    parts = [info.get("team_name"), info.get("school"),
             os.path.splitext(info["original_name"])[0]]
    base = "__".join(safe_name(p) for p in parts if p)
    # 以 video_id 前缀保证跨队伍唯一, 避免同名覆盖
    return f"{vid[:6]}__{base}"


@app.route("/admin/export/images")
@admin_required
def admin_export_images():
    """导出整个图像数据集为 zip (一个 images 文件夹, 包含所有图像)。"""
    videos = load_videos()
    mem = BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as zf:
        for vid, info in videos.items():
            base = _export_base(info, vid)
            for frame in frames_for(vid):
                src = os.path.join(FRAMES_DIR, vid, frame)
                zf.write(src, os.path.join("images", f"{base}__{frame}"))
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
            base = _export_base(info, vid)
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
    """返回所有可标注的帧 (跨所有队伍/视频)。"""
    videos = load_videos()
    result = []
    for v in sorted(videos.values(), key=lambda x: x["uploaded_at"]):
        vid = v["id"]
        label = f"{v.get('team_name', '')} · {v['original_name']}".strip(" ·")
        for frame in frames_for(vid):
            stem = os.path.splitext(frame)[0]
            ann_path = os.path.join(ANNOT_DIR, vid, stem + ".json")
            n_shapes = 0
            if os.path.exists(ann_path):
                data = load_json(ann_path, {})
                n_shapes = len(data.get("shapes", []))
            result.append(
                {
                    "video_id": vid,
                    "video_name": label,
                    "frame": frame,
                    "url": url_for("admin_frame", video_id=vid, frame=frame),
                    "annotated": n_shapes > 0,
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
