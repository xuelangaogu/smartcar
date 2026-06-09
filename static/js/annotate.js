/* 智能车竞赛数据标注平台 - 类 LabelMe 标注引擎 */
(function () {
  "use strict";
  var CFG = window.SMARTCAR;

  // ---- 状态 ----
  var state = {
    frames: [],
    filtered: [],
    labels: [],
    curIndex: -1,
    cur: null, // {video_id, frame, url}
    img: null,
    shapes: [],
    tool: "select",
    activeLabel: null,
    selectedShape: -1,
    selectedVertex: -1,
    scale: 1,
    offsetX: 0,
    offsetY: 0,
    drawing: null, // 正在绘制中的形状
    dragging: null, // {type:'shape'|'vertex'|'pan', startX, startY, ...}
    dirty: false,
  };

  var canvas = document.getElementById("annotCanvas");
  var ctx = canvas.getContext("2d");
  var wrap = document.getElementById("canvasWrap");

  // ---- 工具函数 ----
  function api(url, opts) {
    return fetch(url, opts).then(function (r) {
      return r.json().then(function (d) {
        if (!r.ok) throw new Error(d.error || "请求失败");
        return d;
      });
    });
  }
  function labelColor(name) {
    var l = state.labels.find(function (x) { return x.name === name; });
    return l ? l.color : "#e6194b";
  }
  function toImg(sx, sy) {
    return { x: (sx - state.offsetX) / state.scale, y: (sy - state.offsetY) / state.scale };
  }
  function toScreen(ix, iy) {
    return { x: ix * state.scale + state.offsetX, y: iy * state.scale + state.offsetY };
  }

  // ---- 画布尺寸 ----
  function resizeCanvas() {
    canvas.width = wrap.clientWidth;
    canvas.height = wrap.clientHeight;
    render();
  }
  window.addEventListener("resize", resizeCanvas);

  function fitImage() {
    if (!state.img) return;
    var pad = 40;
    var sw = canvas.width - pad, sh = canvas.height - pad;
    state.scale = Math.min(sw / state.img.width, sh / state.img.height);
    state.offsetX = (canvas.width - state.img.width * state.scale) / 2;
    state.offsetY = (canvas.height - state.img.height * state.scale) / 2;
    updateZoomLabel();
    render();
  }
  function updateZoomLabel() {
    document.getElementById("zoomLabel").textContent = Math.round(state.scale * 100) + "%";
  }

  // ---- 渲染 ----
  function render() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (!state.img) return;
    ctx.save();
    ctx.translate(state.offsetX, state.offsetY);
    ctx.scale(state.scale, state.scale);
    ctx.drawImage(state.img, 0, 0);
    ctx.restore();

    state.shapes.forEach(function (sh, i) {
      drawShape(sh, i === state.selectedShape);
    });
    if (state.drawing) drawShape(state.drawing, true, true);
  }

  function drawShape(sh, selected, preview) {
    var color = sh.color || labelColor(sh.label);
    var pts = sh.points.map(function (p) { return toScreen(p[0], p[1]); });
    ctx.lineWidth = selected ? 2.5 : 2;
    ctx.strokeStyle = color;
    ctx.fillStyle = hexA(color, selected ? 0.25 : 0.15);

    if (sh.shape_type === "rectangle" && pts.length >= 2) {
      var x = Math.min(pts[0].x, pts[1].x), y = Math.min(pts[0].y, pts[1].y);
      var w = Math.abs(pts[1].x - pts[0].x), h = Math.abs(pts[1].y - pts[0].y);
      ctx.fillRect(x, y, w, h);
      ctx.strokeRect(x, y, w, h);
    } else if (sh.shape_type === "polygon") {
      ctx.beginPath();
      pts.forEach(function (p, i) { i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y); });
      if (!preview) ctx.closePath();
      ctx.fill(); ctx.stroke();
    } else if (sh.shape_type === "line" && pts.length >= 2) {
      ctx.beginPath(); ctx.moveTo(pts[0].x, pts[0].y); ctx.lineTo(pts[1].x, pts[1].y); ctx.stroke();
    } else if (sh.shape_type === "point") {
      pts.forEach(function (p) {
        ctx.beginPath(); ctx.arc(p.x, p.y, 5, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
      });
    }

    // 顶点
    if (selected && sh.shape_type !== "point") {
      pts.forEach(function (p) {
        ctx.beginPath(); ctx.arc(p.x, p.y, 4, 0, Math.PI * 2);
        ctx.fillStyle = "#fff"; ctx.fill(); ctx.strokeStyle = color; ctx.stroke();
      });
    }
    // 标签文字
    if (sh.label && pts.length) {
      var lx = pts[0].x, ly = pts[0].y - 6;
      ctx.font = "12px sans-serif";
      var tw = ctx.measureText(sh.label).width;
      ctx.fillStyle = color; ctx.fillRect(lx, ly - 14, tw + 10, 16);
      ctx.fillStyle = "#fff"; ctx.fillText(sh.label, lx + 5, ly - 2);
    }
  }

  function hexA(hex, a) {
    var h = hex.replace("#", "");
    if (h.length === 3) h = h.split("").map(function (c) { return c + c; }).join("");
    var r = parseInt(h.substr(0, 2), 16), g = parseInt(h.substr(2, 2), 16), b = parseInt(h.substr(4, 2), 16);
    return "rgba(" + r + "," + g + "," + b + "," + a + ")";
  }

  // ---- 命中测试 ----
  function hitVertex(ix, iy) {
    var tol = 6 / state.scale;
    for (var i = state.shapes.length - 1; i >= 0; i--) {
      var pts = state.shapes[i].points;
      for (var j = 0; j < pts.length; j++) {
        if (Math.hypot(pts[j][0] - ix, pts[j][1] - iy) < tol) return { shape: i, vertex: j };
      }
    }
    return null;
  }
  function hitShape(ix, iy) {
    for (var i = state.shapes.length - 1; i >= 0; i--) {
      var sh = state.shapes[i];
      if (sh.shape_type === "rectangle") {
        var p = sh.points;
        var x0 = Math.min(p[0][0], p[1][0]), x1 = Math.max(p[0][0], p[1][0]);
        var y0 = Math.min(p[0][1], p[1][1]), y1 = Math.max(p[0][1], p[1][1]);
        if (ix >= x0 && ix <= x1 && iy >= y0 && iy <= y1) return i;
      } else if (sh.shape_type === "polygon") {
        if (pointInPoly(ix, iy, sh.points)) return i;
      } else if (sh.shape_type === "point") {
        if (Math.hypot(sh.points[0][0] - ix, sh.points[0][1] - iy) < 8 / state.scale) return i;
      } else if (sh.shape_type === "line") {
        if (distToSeg(ix, iy, sh.points[0], sh.points[1]) < 6 / state.scale) return i;
      }
    }
    return -1;
  }
  function pointInPoly(x, y, pts) {
    var inside = false;
    for (var i = 0, j = pts.length - 1; i < pts.length; j = i++) {
      var xi = pts[i][0], yi = pts[i][1], xj = pts[j][0], yj = pts[j][1];
      if (((yi > y) !== (yj > y)) && (x < (xj - xi) * (y - yi) / (yj - yi) + xi)) inside = !inside;
    }
    return inside;
  }
  function distToSeg(px, py, a, b) {
    var dx = b[0] - a[0], dy = b[1] - a[1];
    var t = ((px - a[0]) * dx + (py - a[1]) * dy) / (dx * dx + dy * dy || 1);
    t = Math.max(0, Math.min(1, t));
    return Math.hypot(px - (a[0] + t * dx), py - (a[1] + t * dy));
  }

  // ---- 鼠标事件 ----
  canvas.addEventListener("contextmenu", function (e) { e.preventDefault(); });

  canvas.addEventListener("mousedown", function (e) {
    if (!state.img) return;
    var rect = canvas.getBoundingClientRect();
    var sx = e.clientX - rect.left, sy = e.clientY - rect.top;
    var ip = toImg(sx, sy);

    if (e.button === 2 || e.button === 1) { // 右/中键平移
      state.dragging = { type: "pan", startX: sx, startY: sy, ox: state.offsetX, oy: state.offsetY };
      return;
    }

    if (state.tool === "select") {
      var v = hitVertex(ip.x, ip.y);
      if (v) {
        state.selectedShape = v.shape; state.selectedVertex = v.vertex;
        state.dragging = { type: "vertex", shape: v.shape, vertex: v.vertex };
        renderAll(); return;
      }
      var s = hitShape(ip.x, ip.y);
      state.selectedShape = s;
      if (s >= 0) {
        state.dragging = { type: "shape", shape: s, startX: ip.x, startY: ip.y, orig: JSON.parse(JSON.stringify(state.shapes[s].points)) };
      }
      renderAll(); return;
    }

    if (!state.activeLabel) { showToast("请先在右侧选择或添加一个标签", true); return; }

    if (state.tool === "rectangle") {
      state.drawing = { shape_type: "rectangle", label: state.activeLabel, color: labelColor(state.activeLabel), points: [[ip.x, ip.y], [ip.x, ip.y]] };
    } else if (state.tool === "line") {
      state.drawing = { shape_type: "line", label: state.activeLabel, color: labelColor(state.activeLabel), points: [[ip.x, ip.y], [ip.x, ip.y]] };
    } else if (state.tool === "point") {
      addShape({ shape_type: "point", label: state.activeLabel, color: labelColor(state.activeLabel), points: [[ip.x, ip.y]] });
    } else if (state.tool === "polygon") {
      if (!state.drawing) {
        state.drawing = { shape_type: "polygon", label: state.activeLabel, color: labelColor(state.activeLabel), points: [[ip.x, ip.y]] };
      } else {
        // 点击起点附近 -> 闭合
        var first = state.drawing.points[0];
        if (state.drawing.points.length >= 3 && Math.hypot(first[0] - ip.x, first[1] - ip.y) < 8 / state.scale) {
          finishPolygon();
        } else {
          state.drawing.points.push([ip.x, ip.y]);
        }
      }
    }
    render();
  });

  canvas.addEventListener("mousemove", function (e) {
    if (!state.img) return;
    var rect = canvas.getBoundingClientRect();
    var sx = e.clientX - rect.left, sy = e.clientY - rect.top;
    var ip = toImg(sx, sy);

    if (state.dragging) {
      if (state.dragging.type === "pan") {
        state.offsetX = state.dragging.ox + (sx - state.dragging.startX);
        state.offsetY = state.dragging.oy + (sy - state.dragging.startY);
      } else if (state.dragging.type === "vertex") {
        state.shapes[state.dragging.shape].points[state.dragging.vertex] = [ip.x, ip.y];
        state.dirty = true;
      } else if (state.dragging.type === "shape") {
        var dx = ip.x - state.dragging.startX, dy = ip.y - state.dragging.startY;
        state.shapes[state.dragging.shape].points = state.dragging.orig.map(function (p) { return [p[0] + dx, p[1] + dy]; });
        state.dirty = true;
      }
      render(); return;
    }

    if (state.drawing && (state.tool === "rectangle" || state.tool === "line")) {
      state.drawing.points[1] = [ip.x, ip.y];
      render();
    } else if (state.drawing && state.tool === "polygon") {
      render();
      // 预览到鼠标的线
      var last = toScreen(state.drawing.points[state.drawing.points.length - 1][0], state.drawing.points[state.drawing.points.length - 1][1]);
      ctx.strokeStyle = state.drawing.color; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.moveTo(last.x, last.y); ctx.lineTo(sx, sy); ctx.stroke();
    }
  });

  window.addEventListener("mouseup", function (e) {
    if (state.dragging) { state.dragging = null; return; }
    if (state.drawing && (state.tool === "rectangle" || state.tool === "line")) {
      var p = state.drawing.points;
      if (Math.hypot(p[1][0] - p[0][0], p[1][1] - p[0][1]) > 3 / state.scale) {
        addShape(state.drawing);
      }
      state.drawing = null;
      render();
    }
  });

  canvas.addEventListener("dblclick", function () {
    if (state.drawing && state.tool === "polygon" && state.drawing.points.length >= 3) finishPolygon();
  });

  canvas.addEventListener("wheel", function (e) {
    e.preventDefault();
    var rect = canvas.getBoundingClientRect();
    var sx = e.clientX - rect.left, sy = e.clientY - rect.top;
    var before = toImg(sx, sy);
    var factor = e.deltaY < 0 ? 1.1 : 1 / 1.1;
    state.scale = Math.max(0.05, Math.min(20, state.scale * factor));
    state.offsetX = sx - before.x * state.scale;
    state.offsetY = sy - before.y * state.scale;
    updateZoomLabel(); render();
  }, { passive: false });

  function finishPolygon() {
    if (state.drawing && state.drawing.points.length >= 3) addShape(state.drawing);
    state.drawing = null;
    render();
  }

  function addShape(sh) {
    state.shapes.push(sh);
    state.selectedShape = state.shapes.length - 1;
    state.dirty = true;
    renderAll();
  }

  // ---- 标签 UI ----
  function renderLabels() {
    var box = document.getElementById("labelList");
    box.innerHTML = "";
    state.labels.forEach(function (l) {
      var row = document.createElement("div");
      row.className = "label-row" + (l.name === state.activeLabel ? " sel" : "");
      row.innerHTML =
        '<span class="sw" style="background:' + l.color + '"></span>' +
        '<span class="nm">' + escapeHtml(l.name) + "</span>" +
        '<span class="x" title="删除">&times;</span>';
      row.querySelector(".nm").addEventListener("click", function () { state.activeLabel = l.name; renderLabels(); });
      row.querySelector(".sw").addEventListener("click", function () { state.activeLabel = l.name; renderLabels(); });
      row.querySelector(".x").addEventListener("click", function (e) {
        e.stopPropagation();
        if (!confirm("删除标签 " + l.name + " ?")) return;
        api(CFG.labelsUrl, { method: "DELETE", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: l.name }) })
          .then(function (d) { state.labels = d; if (state.activeLabel === l.name) state.activeLabel = d[0] ? d[0].name : null; renderLabels(); });
      });
      box.appendChild(row);
    });
  }

  document.getElementById("addLabelBtn").addEventListener("click", function () {
    var name = document.getElementById("newLabelName").value.trim();
    var color = document.getElementById("newLabelColor").value;
    if (!name) { showToast("请输入标签名", true); return; }
    api(CFG.labelsUrl, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: name, color: color }) })
      .then(function (d) { state.labels = d; state.activeLabel = name; document.getElementById("newLabelName").value = ""; renderLabels(); })
      .catch(function (err) { showToast(err.message, true); });
  });

  // ---- 形状列表 ----
  function renderShapes() {
    var box = document.getElementById("shapeList");
    box.innerHTML = "";
    document.getElementById("shapeCount").textContent = state.shapes.length;
    state.shapes.forEach(function (sh, i) {
      var row = document.createElement("div");
      row.className = "shape-row" + (i === state.selectedShape ? " sel" : "");
      if (i === state.selectedShape) row.style.background = "#eef4ff";
      var typeName = { rectangle: "矩形", polygon: "多边形", point: "点", line: "线" }[sh.shape_type] || sh.shape_type;
      row.innerHTML =
        '<span class="sw" style="background:' + (sh.color || labelColor(sh.label)) + '"></span>' +
        '<span class="nm">' + escapeHtml(sh.label) + " · " + typeName + "</span>" +
        '<span class="x" title="删除">🗑️</span>';
      row.querySelector(".nm").addEventListener("click", function () { state.selectedShape = i; renderAll(); });
      row.querySelector(".x").addEventListener("click", function () { state.shapes.splice(i, 1); state.selectedShape = -1; state.dirty = true; renderAll(); });
      box.appendChild(row);
    });
  }

  function renderAll() { renderShapes(); renderLabels(); render(); }

  // ---- 帧列表 ----
  function renderFrameList() {
    var f = document.getElementById("filterSel").value;
    state.filtered = state.frames.filter(function (fr) {
      if (f === "todo") return !fr.annotated;
      if (f === "done") return fr.annotated;
      return true;
    });
    var box = document.getElementById("frameList");
    box.innerHTML = "";
    document.getElementById("frameCount").textContent = state.frames.length;
    state.filtered.forEach(function (fr) {
      var row = document.createElement("div");
      var isCur = state.cur && state.cur.video_id === fr.video_id && state.cur.frame === fr.frame;
      row.className = "frame-item" + (fr.annotated ? " done" : "") + (isCur ? " active" : "");
      row.innerHTML =
        '<span class="dot"></span>' +
        '<div style="overflow:hidden;"><div class="nm">' + escapeHtml(fr.frame) + "</div>" +
        '<div class="vn">' + escapeHtml(fr.video_name) + (fr.shapes ? " · " + fr.shapes : "") + "</div></div>";
      row.addEventListener("click", function () { loadFrame(fr); });
      box.appendChild(row);
    });
  }
  document.getElementById("filterSel").addEventListener("change", renderFrameList);

  // ---- 加载/保存标注 ----
  function loadFrame(fr) {
    if (state.dirty && !confirm("当前图像有未保存的标注，确定切换？")) return;
    state.cur = fr;
    state.curIndex = state.filtered.indexOf(fr);
    state.selectedShape = -1; state.drawing = null;
    document.getElementById("curName").textContent = fr.frame;
    var img = new Image();
    img.onload = function () {
      state.img = img;
      fitImage();
      api(CFG.annUrlBase + "/" + fr.video_id + "/" + encodeURIComponent(fr.frame))
        .then(function (d) {
          state.shapes = (d.shapes || []).map(function (s) { s.color = s.color || labelColor(s.label); return s; });
          state.dirty = false;
          renderAll();
        });
    };
    img.src = fr.url;
    renderFrameList();
  }

  function save() {
    if (!state.cur) { showToast("请先选择图像", true); return; }
    var payload = {
      imageWidth: state.img ? state.img.width : null,
      imageHeight: state.img ? state.img.height : null,
      shapes: state.shapes,
    };
    api(CFG.annUrlBase + "/" + state.cur.video_id + "/" + encodeURIComponent(state.cur.frame),
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) })
      .then(function () {
        state.dirty = false;
        showToast("已保存 " + state.shapes.length + " 个标注");
        var fr = state.frames.find(function (x) { return x.video_id === state.cur.video_id && x.frame === state.cur.frame; });
        if (fr) { fr.annotated = state.shapes.length > 0; fr.shapes = state.shapes.length; }
        renderFrameList();
      })
      .catch(function (err) { showToast(err.message, true); });
  }
  document.getElementById("saveBtn").addEventListener("click", save);

  function nav(delta) {
    if (!state.filtered.length) return;
    var i = state.curIndex + delta;
    if (i < 0 || i >= state.filtered.length) return;
    loadFrame(state.filtered[i]);
  }
  document.getElementById("prevBtn").addEventListener("click", function () { nav(-1); });
  document.getElementById("nextBtn").addEventListener("click", function () { nav(1); });

  // ---- 缩放按钮 ----
  document.getElementById("zoomIn").addEventListener("click", function () { state.scale *= 1.2; updateZoomLabel(); render(); });
  document.getElementById("zoomOut").addEventListener("click", function () { state.scale /= 1.2; updateZoomLabel(); render(); });
  document.getElementById("zoomFit").addEventListener("click", fitImage);

  // ---- 工具切换 ----
  document.querySelectorAll(".tool-btn").forEach(function (b) {
    b.addEventListener("click", function () { setTool(b.dataset.tool); });
  });
  function setTool(t) {
    state.tool = t;
    if (t !== "polygon") state.drawing = null;
    document.querySelectorAll(".tool-btn").forEach(function (b) { b.classList.toggle("active", b.dataset.tool === t); });
    canvas.style.cursor = t === "select" ? "default" : "crosshair";
    render();
  }

  // ---- 键盘 ----
  document.addEventListener("keydown", function (e) {
    if (/input|textarea|select/i.test(e.target.tagName)) return;
    if (e.ctrlKey && e.key.toLowerCase() === "s") { e.preventDefault(); save(); return; }
    if (e.key === "v" || e.key === "V") setTool("select");
    else if (e.key === "r" || e.key === "R") setTool("rectangle");
    else if (e.key === "p" || e.key === "P") setTool("polygon");
    else if (e.key === "o" || e.key === "O") setTool("point");
    else if (e.key === "l" || e.key === "L") setTool("line");
    else if (e.key === "Enter") finishPolygon();
    else if (e.key === "Escape") { state.drawing = null; render(); }
    else if (e.key === "Delete" || e.key === "Backspace") {
      if (state.selectedShape >= 0) { state.shapes.splice(state.selectedShape, 1); state.selectedShape = -1; state.dirty = true; renderAll(); }
    } else if (e.key === "ArrowLeft") nav(-1);
    else if (e.key === "ArrowRight") nav(1);
  });

  window.addEventListener("beforeunload", function (e) {
    if (state.dirty) { e.preventDefault(); e.returnValue = ""; }
  });

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // ---- 初始化 ----
  function init() {
    resizeCanvas();
    Promise.all([api(CFG.labelsUrl), api(CFG.framesUrl)]).then(function (res) {
      state.labels = res[0];
      state.activeLabel = state.labels[0] ? state.labels[0].name : null;
      state.frames = res[1].frames;
      renderLabels();
      renderFrameList();
      if (state.frames.length) loadFrame(state.frames[0]);
      else {
        ctx.fillStyle = "#aaa"; ctx.font = "16px sans-serif"; ctx.textAlign = "center";
        ctx.fillText("暂无图像，请先在管理后台切分视频生成图像", canvas.width / 2, canvas.height / 2);
      }
    }).catch(function (err) { showToast(err.message, true); });
  }
  init();
})();
