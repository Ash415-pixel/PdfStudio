/**
 * PDF Crop & Layout Studio — Frontend JS
 * =======================================
 * Three modules: UI (navigation), Crop (interactive editor), Layout (preview + export)
 */

"use strict";

// ─── CONSTANTS ────────────────────────────────────────────────────────────────

const PRESETS = [
  { label: "A4",     w: 210,    h: 297   },
  { label: "A5",     w: 148,    h: 210   },
  { label: "Biz Card", w: 85,   h: 55    },
  { label: "Label",  w: 100,    h: 50    },
  { label: "Square", w: 100,    h: 100   },
  { label: "Half A4", w: 105,   h: 148   },
];

const QUICK_LAYOUTS = [
  { cols: 1, rows: 1 },
  { cols: 2, rows: 1 },
  { cols: 1, rows: 2 },
  { cols: 2, rows: 2 },
  { cols: 3, rows: 1 },
  { cols: 1, rows: 3 },
  { cols: 3, rows: 2 },
  { cols: 2, rows: 3 },
];

const PAGE_SIZES_MM = {
  A4:     { w: 210, h: 297 },
  A3:     { w: 297, h: 420 },
  Letter: { w: 215.9, h: 279.4 },
  A5:     { w: 148, h: 210 },
};

// ─── STATE ────────────────────────────────────────────────────────────────────

const state = {
  sessionId:   null,
  filename:    null,
  pageCount:   0,
  pagesInfo:   [],       // [{width_mm, height_mm, width_pt, height_pt}]
  currentPage: 1,
  renderDPI:   150,      // DPI the backend renders at

  // Crop in image pixels (matching the rendered preview image dimensions)
  crop:      null,       // {x, y, w, h}
  cropMm:    null,       // {x, y, w, h} in mm (ground truth)
  unit:      "mm",

  imgW:      0,          // rendered image width in px
  imgH:      0,          // rendered image height in px

  // Interaction
  isDragging:    false,
  dragStart:     null,
  activeHandle:  null,
  handleOrigin:  null,
  isMoving:      false,
  moveOrigin:    null,

  orientation:   "portrait",
};

// ─── HELPERS ──────────────────────────────────────────────────────────────────

function toast(msg, type = "", duration = 3000) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = "toast show" + (type ? " " + type : "");
  clearTimeout(el._timer);
  el._timer = setTimeout(() => { el.className = "toast"; }, duration);
}

function setProgress(pct, label = "") {
  document.getElementById("prog-fill").style.width = pct + "%";
  if (label) document.getElementById("prog-label").textContent = label;
}

function showProgress(label) {
  document.getElementById("upload-progress").style.display = "block";
  setProgress(0, label);
}

function hideProgress() {
  setTimeout(() => {
    document.getElementById("upload-progress").style.display = "none";
  }, 600);
}

function px2mm(px, axis) {
  // canvas pixel → mm using known DPI and current page dimensions
  const pg = state.pagesInfo[state.currentPage - 1];
  if (!pg) return 0;
  if (axis === "x") return (px / state.imgW) * pg.width_mm;
  return (px / state.imgH) * pg.height_mm;
}

function mm2px(mm, axis) {
  const pg = state.pagesInfo[state.currentPage - 1];
  if (!pg) return 0;
  if (axis === "x") return (mm / pg.width_mm) * state.imgW;
  return (mm / pg.height_mm) * state.imgH;
}

function cropPxToMm(crop) {
  return {
    x: px2mm(crop.x, "x"),
    y: px2mm(crop.y, "y"),
    w: px2mm(crop.w, "x"),
    h: px2mm(crop.h, "y"),
  };
}

function cropMmToPx(cmm) {
  return {
    x: mm2px(cmm.x, "x"),
    y: mm2px(cmm.y, "y"),
    w: mm2px(cmm.w, "x"),
    h: mm2px(cmm.h, "y"),
  };
}

function mmToUnit(mm) {
  if (state.unit === "mm") return +mm.toFixed(1);
  if (state.unit === "in") return +(mm / 25.4).toFixed(3);
  if (state.unit === "px") return +(mm * (state.renderDPI / 25.4)).toFixed(0);
}
function unitToMm(val) {
  if (state.unit === "mm") return +val;
  if (state.unit === "in") return +val * 25.4;
  if (state.unit === "px") return +val / (state.renderDPI / 25.4);
}

function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

// ─── UI MODULE ────────────────────────────────────────────────────────────────

const UI = {
  goStep(n) {
    if (n === 2 && !state.sessionId) { toast("Upload a PDF first", "error"); return; }
    if (n === 3 && !state.cropMm)    { toast("Select a crop region first", "error"); return; }

    [1, 2, 3].forEach(i => {
      document.getElementById(`panel-${i}`).classList.toggle("active", i === n);
      const item = document.getElementById(`step-item-${i}`);
      item.classList.toggle("active", i === n);
      item.classList.toggle("done", i < n);
    });

    if (n === 3) Layout.update();
  },
};

// ─── CROP MODULE ──────────────────────────────────────────────────────────────

const Crop = {
  init() {
    this._buildPresets();
    this._setupDrop();
    this._setupInteraction();
  },

  _buildPresets() {
    const wrap = document.getElementById("preset-wrap");
    PRESETS.forEach(p => {
      const chip = document.createElement("button");
      chip.className = "preset-chip";
      chip.textContent = p.label;
      chip.title = `${p.w} × ${p.h} mm`;
      chip.onclick = () => this.applyPreset(p, chip);
      wrap.appendChild(chip);
    });
  },

  _setupDrop() {
    const zone = document.getElementById("drop-zone");
    const input = document.getElementById("file-input");

    zone.addEventListener("dragover",  e => { e.preventDefault(); zone.classList.add("over"); });
    zone.addEventListener("dragleave", () => zone.classList.remove("over"));
    zone.addEventListener("drop", e => {
      e.preventDefault();
      zone.classList.remove("over");
      const f = e.dataTransfer.files[0];
      if (f?.name.toLowerCase().endsWith(".pdf")) this.upload(f);
      else toast("Please drop a PDF file", "error");
    });
    input.addEventListener("change", () => {
      if (input.files[0]) this.upload(input.files[0]);
    });
  },

  async upload(file) {
    showProgress("Uploading PDF…");
    setProgress(10);

    const fd = new FormData();
    fd.append("file", file);

    try {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/upload");
      xhr.upload.onprogress = e => {
        if (e.lengthComputable) setProgress(10 + 60 * (e.loaded / e.total), "Uploading…");
      };

      const data = await new Promise((res, rej) => {
        xhr.onload = () => res(JSON.parse(xhr.responseText));
        xhr.onerror = () => rej(new Error("Network error"));
        xhr.send(fd);
      });

      if (data.error) throw new Error(data.error);

      setProgress(80, "Rendering preview…");
      state.sessionId = data.session_id;
      state.filename  = data.filename;
      state.pageCount = data.page_count;
      state.pagesInfo = data.pages_info;
      state.currentPage = 1;
      state.crop    = null;
      state.cropMm  = null;

      document.getElementById("file-info").textContent = file.name;

      // Build page selector
      const sel = document.getElementById("page-select");
      sel.innerHTML = "";
      for (let i = 1; i <= data.page_count; i++) {
        const o = document.createElement("option");
        o.value = i; o.textContent = `Page ${i} of ${data.page_count}`;
        sel.appendChild(o);
      }

      await this.renderPage(1);
      setProgress(100, "Done!");
      hideProgress();
      UI.goStep(2);
      toast("PDF loaded — " + data.page_count + " page(s)", "success");

    } catch (e) {
      hideProgress();
      toast("Upload failed: " + e.message, "error");
    }
  },

  async renderPage(n) {
    state.currentPage = n;
    const img = document.getElementById("pdf-preview");
    img.src = "";

    document.getElementById("stage-wrap").style.display = "none";
    document.getElementById("empty-canvas").style.display = "flex";

    return new Promise((res, rej) => {
      img.onload = () => {
        state.imgW = img.naturalWidth;
        state.imgH = img.naturalHeight;
        document.getElementById("stage-wrap").style.display = "block";
        document.getElementById("empty-canvas").style.display = "none";
        this._updateToolbarInfo();
        this._clearOverlay();
        res();
      };
      img.onerror = rej;
      img.src = `/api/render/${state.sessionId}/${n}?dpi=${state.renderDPI}&t=${Date.now()}`;
    });
  },

  async changePage(n) {
    state.crop   = null;
    state.cropMm = null;
    this._resetStats();
    document.getElementById("btn-next-layout").disabled = true;
    await this.renderPage(n);
  },

  setUnit(u) {
    state.unit = u;
    document.querySelectorAll(".seg-btn[data-unit]").forEach(b => {
      b.classList.toggle("active", b.dataset.unit === u);
    });
    this._syncInputs();
  },

  applyPreset(p, chipEl) {
    document.querySelectorAll(".preset-chip").forEach(c => c.classList.remove("active"));
    chipEl?.classList.add("active");

    const pg = state.pagesInfo[state.currentPage - 1];
    if (!pg) return;

    const cx = Math.max(0, (pg.width_mm - p.w) / 2);
    const cy = Math.max(0, (pg.height_mm - p.h) / 2);
    state.cropMm = {
      x: cx,
      y: cy,
      w: Math.min(p.w, pg.width_mm),
      h: Math.min(p.h, pg.height_mm),
    };
    state.crop = cropMmToPx(state.cropMm);
    this._updateOverlay();
    this._syncInputs();
    this._updateStats();
    document.getElementById("btn-next-layout").disabled = false;
  },

  applyManual() {
    const x = unitToMm(+document.getElementById("inp-x").value || 0);
    const y = unitToMm(+document.getElementById("inp-y").value || 0);
    const w = unitToMm(+document.getElementById("inp-w").value || 0);
    const h = unitToMm(+document.getElementById("inp-h").value || 0);
    if (w <= 0 || h <= 0) return;

    const pg = state.pagesInfo[state.currentPage - 1];
    state.cropMm = {
      x: clamp(x, 0, pg.width_mm - 1),
      y: clamp(y, 0, pg.height_mm - 1),
      w: Math.min(w, pg.width_mm - x),
      h: Math.min(h, pg.height_mm - y),
    };
    state.crop = cropMmToPx(state.cropMm);
    this._updateOverlay();
    this._updateStats();
    document.getElementById("btn-next-layout").disabled = false;
  },

  reset() {
    state.crop   = null;
    state.cropMm = null;
    this._clearOverlay();
    this._resetStats();
    this._resetInputs();
    document.getElementById("btn-next-layout").disabled = true;
    document.querySelectorAll(".preset-chip").forEach(c => c.classList.remove("active"));
  },

  selectAll() {
    const pg = state.pagesInfo[state.currentPage - 1];
    if (!pg) return;
    state.cropMm = { x: 0, y: 0, w: pg.width_mm, h: pg.height_mm };
    state.crop   = cropMmToPx(state.cropMm);
    this._updateOverlay();
    this._syncInputs();
    this._updateStats();
    document.getElementById("btn-next-layout").disabled = false;
  },

  setZoom(val) {
    document.getElementById("zoom-label").textContent = val + "%";
    const img = document.getElementById("pdf-preview");
    const scale = val / 100;
    img.style.width  = (state.imgW * scale) + "px";
    img.style.height = (state.imgH * scale) + "px";
    // scale = rendered_px / display_px
    // We track crop in rendered px, overlay uses CSS dimensions
    document.getElementById("crop-stage").style.width  = (state.imgW * scale) + "px";
    document.getElementById("crop-stage").style.height = (state.imgH * scale) + "px";
    this._updateOverlay();
  },

  fitView() {
    document.getElementById("zoom-range").value = 100;
    this.setZoom(100);
  },

  zoom100() { this.fitView(); },

  _getDisplayScale() {
    const img = document.getElementById("pdf-preview");
    if (!img.naturalWidth) return 1;
    return img.offsetWidth / img.naturalWidth;
  },

  _setupInteraction() {
    const stage = document.getElementById("crop-stage");

    stage.addEventListener("mousedown",  e => this._onMouseDown(e));
    document.addEventListener("mousemove", e => this._onMouseMove(e));
    document.addEventListener("mouseup",   e => this._onMouseUp(e));

    // Touch support
    stage.addEventListener("touchstart",  e => { e.preventDefault(); this._onMouseDown(this._toMouse(e)); }, { passive: false });
    document.addEventListener("touchmove",  e => { e.preventDefault(); this._onMouseMove(this._toMouse(e)); }, { passive: false });
    document.addEventListener("touchend",   e => this._onMouseUp(e));
  },

  _toMouse(e) {
    const t = e.touches[0] || e.changedTouches[0];
    return { clientX: t.clientX, clientY: t.clientY, target: e.target, button: 0 };
  },

  _stagePos(e) {
    const rect = document.getElementById("crop-stage").getBoundingClientRect();
    const ds   = this._getDisplayScale();
    return {
      x: clamp(e.clientX - rect.left, 0, rect.width)  / ds,
      y: clamp(e.clientY - rect.top,  0, rect.height) / ds,
    };
  },

  _onMouseDown(e) {
    if (e.button !== 0) return;
    const target = e.target;

    // If clicking on a handle
    if (target.classList.contains("crop-handle")) {
      e.stopPropagation();
      state.activeHandle = target.dataset.handle;
      state.handleOrigin = { mx: e.clientX, my: e.clientY, crop: { ...state.crop } };
      return;
    }

    // If clicking on the crop-box itself → move
    if (target.classList.contains("crop-box")) {
      e.stopPropagation();
      state.isMoving   = true;
      state.moveOrigin = { mx: e.clientX, my: e.clientY, crop: { ...state.crop } };
      return;
    }

    // New drag on stage
    const pos = this._stagePos(e);
    state.isDragging = true;
    state.dragStart  = pos;
    state.crop = { x: pos.x, y: pos.y, w: 0, h: 0 };
  },

  _onMouseMove(e) {
    if (state.activeHandle) {
      this._resizeHandle(e);
      return;
    }
    if (state.isMoving) {
      this._moveCrop(e);
      return;
    }
    if (!state.isDragging) return;

    const pos = this._stagePos(e);
    state.crop = {
      x: Math.min(pos.x, state.dragStart.x),
      y: Math.min(pos.y, state.dragStart.y),
      w: Math.abs(pos.x - state.dragStart.x),
      h: Math.abs(pos.y - state.dragStart.y),
    };
    this._clampCrop();
    this._updateOverlay();
  },

  _onMouseUp(e) {
    if (state.isDragging) {
      state.isDragging = false;
      if (state.crop && state.crop.w > 4 && state.crop.h > 4) {
        this._finalize();
      } else {
        this.reset();
      }
    }
    if (state.activeHandle) { state.activeHandle = null; this._finalize(); }
    if (state.isMoving)     { state.isMoving = false;    this._finalize(); }
  },

  _resizeHandle(e) {
    const dx = (e.clientX - state.handleOrigin.mx) / this._getDisplayScale();
    const dy = (e.clientY - state.handleOrigin.my) / this._getDisplayScale();
    let { x, y, w, h } = state.handleOrigin.crop;
    const hid = state.activeHandle;

    if (hid.includes("e")) w = Math.max(5, w + dx);
    if (hid.includes("s")) h = Math.max(5, h + dy);
    if (hid.includes("w")) { x += dx; w = Math.max(5, w - dx); }
    if (hid.includes("n")) { y += dy; h = Math.max(5, h - dy); }

    state.crop = { x, y, w, h };
    this._clampCrop();
    this._updateOverlay();
  },

  _moveCrop(e) {
    const dx = (e.clientX - state.moveOrigin.mx) / this._getDisplayScale();
    const dy = (e.clientY - state.moveOrigin.my) / this._getDisplayScale();
    state.crop = {
      x: state.moveOrigin.crop.x + dx,
      y: state.moveOrigin.crop.y + dy,
      w: state.moveOrigin.crop.w,
      h: state.moveOrigin.crop.h,
    };
    this._clampCrop();
    this._updateOverlay();
  },

  _clampCrop() {
    if (!state.crop) return;
    const { x, y, w, h } = state.crop;
    state.crop.x = clamp(x, 0, state.imgW - (w || 1));
    state.crop.y = clamp(y, 0, state.imgH - (h || 1));
    state.crop.w = Math.min(w, state.imgW - state.crop.x);
    state.crop.h = Math.min(h, state.imgH - state.crop.y);
  },

  _finalize() {
    state.cropMm = cropPxToMm(state.crop);
    this._syncInputs();
    this._updateStats();
    this._updateOverlay();
    document.getElementById("btn-next-layout").disabled = false;
  },

  _updateOverlay() {
    const overlay = document.getElementById("crop-overlay");
    overlay.innerHTML = "";

    if (!state.crop || state.crop.w < 1) return;

    const ds = this._getDisplayScale();
    const { x, y, w, h } = state.crop;
    const X = x * ds, Y = y * ds, W = w * ds, H = h * ds;
    const IW = state.imgW * ds, IH = state.imgH * ds;

    // Masks
    const masks = [
      { top: 0,   left: 0,   width: IW,  height: Y   },
      { top: Y+H, left: 0,   width: IW,  height: IH-Y-H },
      { top: Y,   left: 0,   width: X,   height: H   },
      { top: Y,   left: X+W, width: IW-X-W, height: H },
    ];
    masks.forEach(m => {
      const el = document.createElement("div");
      el.className = "crop-mask";
      Object.assign(el.style, {
        top: m.top + "px", left: m.left + "px",
        width: m.width + "px", height: m.height + "px",
      });
      overlay.appendChild(el);
    });

    // Crop box
    const box = document.createElement("div");
    box.className = "crop-box";
    Object.assign(box.style, {
      left: X + "px", top: Y + "px",
      width: W + "px", height: H + "px",
    });

    // Dimension badge
    const badge = document.createElement("div");
    badge.className = "crop-dim-badge";
    const cmm = state.cropMm || cropPxToMm(state.crop);
    badge.textContent = `${cmm.w.toFixed(1)} × ${cmm.h.toFixed(1)} mm`;
    box.appendChild(badge);

    // Rule-of-thirds lines
    ["33.33%", "66.66%"].forEach(pct => {
      [true, false].forEach(horiz => {
        const line = document.createElement("div");
        line.className = "crop-grid-line";
        Object.assign(line.style, horiz
          ? { top: pct, left: 0, right: 0, height: "1px" }
          : { left: pct, top: 0, bottom: 0, width: "1px" });
        box.appendChild(line);
      });
    });

    // Handles
    ["nw","n","ne","w","e","sw","s","se"].forEach(hid => {
      const h = document.createElement("div");
      h.className = `crop-handle ${hid}`;
      h.dataset.handle = hid;
      h.addEventListener("mousedown", ev => {
        ev.stopPropagation();
        state.activeHandle = hid;
        state.handleOrigin = { mx: ev.clientX, my: ev.clientY, crop: { ...state.crop } };
      });
      box.appendChild(h);
    });

    // Move cursor on box (not handles)
    box.addEventListener("mousedown", ev => {
      if (ev.target.classList.contains("crop-handle")) return;
      ev.stopPropagation();
      state.isMoving   = true;
      state.moveOrigin = { mx: ev.clientX, my: ev.clientY, crop: { ...state.crop } };
    });

    overlay.appendChild(box);
  },

  _clearOverlay() {
    document.getElementById("crop-overlay").innerHTML = "";
  },

  _syncInputs() {
    if (!state.cropMm) return;
    const fields = ["x","y","w","h"];
    const vals   = [state.cropMm.x, state.cropMm.y, state.cropMm.w, state.cropMm.h];
    fields.forEach((f, i) => {
      document.getElementById("inp-" + f).value = mmToUnit(vals[i]);
    });
  },

  _resetInputs() {
    ["x","y","w","h"].forEach(f => { document.getElementById("inp-" + f).value = ""; });
  },

  _updateStats() {
    if (!state.cropMm) { this._resetStats(); return; }
    const c = state.cropMm;
    const ar = (c.w / c.h).toFixed(2);
    document.getElementById("stat-w").textContent = c.w.toFixed(1) + " mm";
    document.getElementById("stat-h").textContent = c.h.toFixed(1) + " mm";
    document.getElementById("stat-x").textContent = c.x.toFixed(1) + " mm";
    document.getElementById("stat-y").textContent = c.y.toFixed(1) + " mm";
    document.getElementById("stat-ar").textContent = ar + " : 1";
  },

  _resetStats() {
    ["w","h","x","y","ar"].forEach(k => {
      document.getElementById("stat-" + k).textContent = "—";
    });
  },

  _updateToolbarInfo() {
    const pg = state.pagesInfo[state.currentPage - 1];
    if (!pg) return;
    const txt = `Page ${state.currentPage} of ${state.pageCount}  ·  ${pg.width_mm.toFixed(1)} × ${pg.height_mm.toFixed(1)} mm`;
    document.getElementById("ctb-info").textContent = txt;
  },
};

// ─── LAYOUT MODULE ────────────────────────────────────────────────────────────

const Layout = {
  init() {
    this._buildLayoutChips();
  },

  _buildLayoutChips() {
    const grid = document.getElementById("layout-preset-grid");
    QUICK_LAYOUTS.forEach(({ cols, rows }) => {
      const chip = document.createElement("div");
      chip.className = "lp-chip";
      chip.dataset.cols = cols;
      chip.dataset.rows = rows;

      let rowsHtml = "";
      for (let r = 0; r < Math.min(rows, 3); r++) {
        let cells = "";
        for (let c = 0; c < Math.min(cols, 3); c++) cells += `<div class="lp-cell-box"></div>`;
        rowsHtml += `<div class="lp-row">${cells}</div>`;
      }
      chip.innerHTML = `<div class="lp-grid">${rowsHtml}</div><div class="lp-label">${cols}×${rows}</div>`;
      chip.onclick = () => this._applyLayoutChip(chip, cols, rows);
      grid.appendChild(chip);
    });
  },

  _applyLayoutChip(chip, cols, rows) {
    document.querySelectorAll(".lp-chip").forEach(c => c.classList.remove("active"));
    chip.classList.add("active");
    document.getElementById("lay-cols").value = cols;
    document.getElementById("lay-rows").value = rows;
    this.update();
  },

  setOrient(o) {
    state.orientation = o;
    document.querySelectorAll(".seg-btn[data-orient]").forEach(b => {
      b.classList.toggle("active", b.dataset.orient === o);
    });
    this.update();
  },

  _getConfig() {
    const cols     = Math.max(1, Math.min(8, parseInt(document.getElementById("lay-cols").value) || 2));
    const rows     = Math.max(1, Math.min(8, parseInt(document.getElementById("lay-rows").value) || 2));
    const maxCopies = cols * rows;
    const copiesEl = document.getElementById("lay-copies");
    copiesEl.max = maxCopies;
    if (parseInt(copiesEl.value) > maxCopies) copiesEl.value = maxCopies;
    document.getElementById("copies-hint").textContent = `(1–${maxCopies})`;
    const copies   = Math.max(1, Math.min(maxCopies, parseInt(copiesEl.value) || maxCopies));
    const outKey   = document.getElementById("out-size").value;
    const marginMm = parseFloat(document.getElementById("lay-margin").value) || 5;
    const gutterMm = parseFloat(document.getElementById("lay-gutter").value) || 3;
    const maintainAr  = document.getElementById("opt-ar").checked;
    const centerItems = document.getElementById("opt-center").checked;
    const cutLines    = document.getElementById("opt-cutlines").checked;

    let { w: pgW, h: pgH } = PAGE_SIZES_MM[outKey] || PAGE_SIZES_MM.A4;
    if (state.orientation === "landscape") [pgW, pgH] = [pgH, pgW];

    return { cols, rows, copies, pgW, pgH, marginMm, gutterMm, maintainAr, centerItems, cutLines };
  },

  update() {
    if (!state.cropMm) return;
    const cfg = this._getConfig();
    this._renderPreview(cfg);
    this._updateSummary(cfg);
  },

  _renderPreview(cfg) {
    const { cols, rows, copies, pgW, pgH, marginMm, gutterMm, maintainAr, centerItems, cutLines } = cfg;

    // Scale sheet to fit viewport (~500px tall)
    const PREVIEW_H = 500;
    const scl = PREVIEW_H / pgH;
    const PREVIEW_W = pgW * scl;

    const sheet = document.getElementById("a4-sheet");
    sheet.style.width  = PREVIEW_W + "px";
    sheet.style.height = PREVIEW_H + "px";
    sheet.innerHTML    = "";

    document.getElementById("sheet-meta").textContent =
      `${document.getElementById("out-size").value} · ${pgW.toFixed(0)} × ${pgH.toFixed(0)} mm · ${state.orientation}`;

    document.getElementById("copy-badge").textContent = `${copies} cop${copies === 1 ? "y" : "ies"}`;

    const totalGW  = gutterMm * (cols - 1);
    const totalGH  = gutterMm * (rows - 1);
    const cellWmm  = (pgW - 2 * marginMm - totalGW) / cols;
    const cellHmm  = (pgH - 2 * marginMm - totalGH) / rows;
    const cropAR   = state.cropMm.w / state.cropMm.h;

    for (let r = 0; r < rows; r++) {
      for (let c = 0; c < cols; c++) {
        const cellIndex = r * cols + c;
        const cellX = (marginMm + c * (cellWmm + gutterMm)) * scl;
        const cellY = (marginMm + r * (cellHmm + gutterMm)) * scl;
        const cellW = cellWmm * scl;
        const cellH = cellHmm * scl;

        const cell = document.createElement("div");
        cell.className = "layout-cell";
        Object.assign(cell.style, {
          left: cellX + "px", top: cellY + "px",
          width: cellW + "px", height: cellH + "px",
          background: cellIndex < copies ? "#f0f0f0" : "#ffffff",
          border: cutLines ? "1px dashed rgba(100,100,100,0.4)" : "1px solid rgba(0,0,0,0.06)",
        });

        if (cellIndex < copies) {
          // Draw crop preview image in cell
          let imgX = 0, imgY = 0, imgW = cellW, imgH = cellH;
          if (maintainAr) {
            const cAR = cellW / cellH;
            if (cropAR > cAR) { imgW = cellW; imgH = cellW / cropAR; }
            else { imgH = cellH; imgW = cellH * cropAR; }
            if (centerItems) { imgX = (cellW - imgW) / 2; imgY = (cellH - imgH) / 2; }
          }

          const img = document.createElement("img");
          const cmm = state.cropMm;
          img.src = `/api/crop-preview-img/${state.sessionId}/${state.currentPage}?x=${cmm.x}&y=${cmm.y}&w=${cmm.w}&h=${cmm.h}&t=${Date.now()}`;
          Object.assign(img.style, {
            position: "absolute",
            left: imgX + "px", top: imgY + "px",
            width: imgW + "px", height: imgH + "px",
            objectFit: "fill",
          });
          cell.appendChild(img);
        }

        sheet.appendChild(cell);
      }
    }
  },

  _updateSummary(cfg) {
    const { cols, rows, copies, pgW, pgH, marginMm, gutterMm } = cfg;
    const totalGW = gutterMm * (cols - 1);
    const totalGH = gutterMm * (rows - 1);
    const cellW   = ((pgW - 2 * marginMm - totalGW) / cols).toFixed(1);
    const cellH   = ((pgH - 2 * marginMm - totalGH) / rows).toFixed(1);

    document.getElementById("layout-summary").innerHTML = `
      Copies: <strong>${copies}</strong> of <strong>${cols * rows}</strong><br>
      Grid: <strong>${cols} col × ${rows} row</strong><br>
      Cell size: <strong>${cellW} × ${cellH} mm</strong><br>
      Page: <strong>${pgW.toFixed(0)} × ${pgH.toFixed(0)} mm</strong><br>
      Margin: <strong>${marginMm} mm</strong>  ·  Gutter: <strong>${gutterMm} mm</strong>
    `;

    document.getElementById("layout-ctb-info").textContent =
      `${cols} × ${rows} grid · ${copies} of ${cols * rows} cop${copies === 1 ? "y" : "ies"} · ${pgW.toFixed(0)} × ${pgH.toFixed(0)} mm`;
  },

  async export() {
    if (!state.sessionId || !state.cropMm) {
      toast("No crop region selected", "error"); return;
    }

    const btn = document.getElementById("btn-export");
    btn.disabled = true;
    btn.innerHTML = `<span class="spinner"></span> Generating…`;

    const cfg = this._getConfig();

    const payload = {
      session_id:  state.sessionId,
      page:        state.currentPage,
      crop_mm:     state.cropMm,
      layout:      { cols: cfg.cols, rows: cfg.rows },
      copies:      cfg.copies,
      output_size: document.getElementById("out-size").value,
      orientation: state.orientation,
      margin_mm:   cfg.marginMm,
      gutter_mm:   cfg.gutterMm,
      maintain_ar: cfg.maintainAr,
      center_items: cfg.centerItems,
      cut_lines:   cfg.cutLines,
    };

    try {
      const resp = await fetch("/api/export", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify(payload),
      });

      if (!resp.ok) {
        const err = await resp.json();
        throw new Error(err.error || resp.statusText);
      }

      const blob = await resp.blob();
      const url  = URL.createObjectURL(blob);
      const a    = document.createElement("a");
      a.href     = url;
      a.download = "cropped-layout.pdf";
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 5000);
      toast("PDF exported successfully!", "success");

    } catch (e) {
      toast("Export failed: " + e.message, "error");
    } finally {
      btn.disabled = false;
      btn.innerHTML = `<span class="btn-export-icon">⬇</span> Export PDF`;
    }
  },
};

// ─── CROP PREVIEW IMAGE ROUTE HELPER ─────────────────────────────────────────
// We need a route to serve just the cropped region as an image for the preview
// We POST crop_mm lazily on layout render; here we set it globally each time crop changes.

// Patch: store cropMm in a way the preview img src can be parametrized
const _origFinalize = Crop._finalize.bind(Crop);
Crop._finalize = function() {
  _origFinalize();
  // Update layout preview if we're on step 3
  if (document.getElementById("panel-3").classList.contains("active")) {
    Layout.update();
  }
};

// ─── KEYBOARD SHORTCUTS ───────────────────────────────────────────────────────

document.addEventListener("keydown", e => {
  const tag = document.activeElement.tagName;
  if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;

  if (e.key === "Escape")               Crop.reset();
  if (e.ctrlKey && e.key === "a")       { e.preventDefault(); Crop.selectAll(); }
  if (e.key === "1")                    UI.goStep(1);
  if (e.key === "2" && state.sessionId) UI.goStep(2);
  if (e.key === "3" && state.cropMm)    UI.goStep(3);
});

// ─── SESSION CLEANUP ON UNLOAD ────────────────────────────────────────────────

window.addEventListener("beforeunload", () => {
  if (state.sessionId) {
    navigator.sendBeacon(`/api/session/${state.sessionId}`, JSON.stringify({ method: "DELETE" }));
  }
});

// ─── INIT ─────────────────────────────────────────────────────────────────────

Crop.init();
Layout.init();
