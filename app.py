"""
PDF Crop & Layout Studio — Flask Backend
========================================
Handles PDF upload, page rendering, crop operations, and multi-instance layout export.
"""

import os
import io
import uuid
import json
import base64
import logging
import tempfile
from pathlib import Path
from typing import Optional

from flask import (
    Flask, request, jsonify, send_file,
    render_template, send_from_directory
)
import pypdfium2 as pdfium
from pypdf import PdfReader, PdfWriter
from PIL import Image
import reportlab
from reportlab.lib.pagesizes import A4, A3, letter, A5, landscape
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.lib.units import mm

# ─── CONFIGURATION ─────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"

UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

MAX_UPLOAD_MB = 50
PREVIEW_DPI = 150       # DPI for interactive preview renders
RENDER_DPI = 150        # Base DPI for rendering
MM_TO_PT = 72 / 25.4   # 1mm = 2.8346pt

PAGE_SIZES_MM = {
    "A4":     (210.0, 297.0),
    "A3":     (297.0, 420.0),
    "Letter": (215.9, 279.4),
    "A5":     (148.0, 210.0),
}

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("pdf_studio")

# ─── APP SETUP ─────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
app.secret_key = os.urandom(24)

# In-memory session store  {session_id: {path, pages, ...}}
sessions: dict[str, dict] = {}


# ─── HELPERS ───────────────────────────────────────────────────────────────────

def new_session_id() -> str:
    return str(uuid.uuid4())


def get_session(sid: str) -> Optional[dict]:
    return sessions.get(sid)


def render_page_to_png(pdf_path: str, page_index: int, dpi: int = PREVIEW_DPI) -> bytes:
    """Render a single PDF page to PNG bytes using pypdfium2."""
    doc = pdfium.PdfDocument(pdf_path)
    page = doc[page_index]
    scale = dpi / 72.0
    bitmap = page.render(scale=scale, rotation=0)
    pil_img = bitmap.to_pil()
    buf = io.BytesIO()
    pil_img.save(buf, format="PNG", optimize=True)
    doc.close()
    return buf.getvalue()


def px_to_pt(px: float, dpi: int) -> float:
    """Convert rendered pixel coordinate to PDF points."""
    return px * 72.0 / dpi


def mm_to_pt(mm_val: float) -> float:
    return mm_val * MM_TO_PT


def build_layout_pdf(
    source_pdf_path: str,
    page_index: int,
    crop_pt: dict,          # {x, y, w, h} in PDF points (y from bottom)
    output_size_mm: tuple,  # (width_mm, height_mm)
    cols: int,
    rows: int,
    copies: int,
    margin_mm: float,
    gutter_mm: float,
    maintain_ar: bool,
    center_items: bool,
    cut_lines: bool,
    orientation: str,
) -> bytes:
    """
    Build a new PDF with `copies` copies of the cropped region arranged in a
    `cols × rows` grid on a single output page. Cells beyond `copies` are left blank.
    Returns the PDF as bytes.
    """
    page_w_mm, page_h_mm = output_size_mm
    if orientation == "landscape":
        page_w_mm, page_h_mm = page_h_mm, page_w_mm

    page_w_pt = mm_to_pt(page_w_mm)
    page_h_pt = mm_to_pt(page_h_mm)
    margin_pt = mm_to_pt(margin_mm)
    gutter_pt = mm_to_pt(gutter_mm)

    total_gutter_w = gutter_pt * (cols - 1)
    total_gutter_h = gutter_pt * (rows - 1)
    cell_w_pt = (page_w_pt - 2 * margin_pt - total_gutter_w) / cols
    cell_h_pt = (page_h_pt - 2 * margin_pt - total_gutter_h) / rows

    crop_w_pt = crop_pt["w"]
    crop_h_pt = crop_pt["h"]
    crop_ar = crop_w_pt / crop_h_pt if crop_h_pt > 0 else 1.0

    # ── Render crop region to a high-res image for embedding ──────────────────
    # We render at a higher DPI to maintain quality in the output PDF
    EXPORT_DPI = 300
    scale = EXPORT_DPI / 72.0

    doc = pdfium.PdfDocument(source_pdf_path)
    page = doc[page_index]
    pg_width_pt = page.get_width()
    pg_height_pt = page.get_height()

    # Convert crop from PDF-pt coordinates to render pixels
    crop_x_px = int(crop_pt["x"] * scale)
    crop_y_px = int((pg_height_pt - crop_pt["y"] - crop_h_pt) * scale)  # flip Y
    crop_w_px = int(crop_w_pt * scale)
    crop_h_px = int(crop_h_pt * scale)

    # Render the full page then crop
    bitmap = page.render(scale=scale, rotation=0)
    pil_full = bitmap.to_pil()
    doc.close()

    # Crop the rendered image
    left = max(0, crop_x_px)
    upper = max(0, crop_y_px)
    right = min(pil_full.width, left + crop_w_px)
    lower = min(pil_full.height, upper + crop_h_px)
    pil_crop = pil_full.crop((left, upper, right, lower))

    # Save cropped image to temp file
    tmp_img = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    pil_crop.save(tmp_img.name, "PNG")
    tmp_img.close()

    # ── Build output PDF with ReportLab ───────────────────────────────────────
    out_buf = io.BytesIO()
    c = rl_canvas.Canvas(out_buf, pagesize=(page_w_pt, page_h_pt))

    for row in range(rows):
        for col in range(cols):
            cell_index = row * cols + col
            cell_x = margin_pt + col * (cell_w_pt + gutter_pt)
            # ReportLab Y origin is bottom-left
            cell_y = page_h_pt - margin_pt - (row + 1) * cell_h_pt - row * gutter_pt

            if cell_index < copies:
                draw_x, draw_y = cell_x, cell_y
                draw_w, draw_h = cell_w_pt, cell_h_pt

                if maintain_ar:
                    cell_ar = cell_w_pt / cell_h_pt if cell_h_pt > 0 else 1.0
                    if crop_ar > cell_ar:
                        draw_w = cell_w_pt
                        draw_h = cell_w_pt / crop_ar
                    else:
                        draw_h = cell_h_pt
                        draw_w = cell_h_pt * crop_ar
                    if center_items:
                        draw_x = cell_x + (cell_w_pt - draw_w) / 2
                        draw_y = cell_y + (cell_h_pt - draw_h) / 2

                # Draw the cropped image
                c.drawImage(
                    tmp_img.name,
                    draw_x, draw_y,
                    width=draw_w, height=draw_h,
                    preserveAspectRatio=False,
                    mask="auto"
                )

            # Cut lines (drawn for all cells regardless of copies)
            if cut_lines:
                c.setStrokeColorRGB(0.5, 0.5, 0.5)
                c.setLineWidth(0.4)
                c.setDash(4, 4)
                c.rect(cell_x, cell_y, cell_w_pt, cell_h_pt, stroke=1, fill=0)
                c.setDash()

    c.save()
    os.unlink(tmp_img.name)
    return out_buf.getvalue()


# ─── ROUTES ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/upload", methods=["POST"])
def api_upload():
    """Upload a PDF file, return session ID + page count + first page preview."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    f = request.files["file"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Only PDF files are supported"}), 400

    sid = new_session_id()
    save_path = str(UPLOAD_DIR / f"{sid}.pdf")
    f.save(save_path)

    try:
        reader = PdfReader(save_path)
        page_count = len(reader.pages)
        first_page = reader.pages[0]
        # Get page dimensions in mm
        w_pt = float(first_page.mediabox.width)
        h_pt = float(first_page.mediabox.height)
        w_mm = w_pt / MM_TO_PT
        h_mm = h_pt / MM_TO_PT

        sessions[sid] = {
            "path": save_path,
            "page_count": page_count,
            "pages_info": [],
        }

        # Pre-cache page dimensions
        doc = pdfium.PdfDocument(save_path)
        for i in range(page_count):
            pg = doc[i]
            sessions[sid]["pages_info"].append({
                "width_pt": pg.get_width(),
                "height_pt": pg.get_height(),
                "width_mm": pg.get_width() / MM_TO_PT,
                "height_mm": pg.get_height() / MM_TO_PT,
            })
        doc.close()

        log.info(f"Uploaded PDF [{sid}]: {page_count} pages, {w_mm:.1f}×{h_mm:.1f}mm")
        return jsonify({
            "session_id": sid,
            "page_count": page_count,
            "pages_info": sessions[sid]["pages_info"],
            "filename": f.filename,
        })

    except Exception as e:
        log.error(f"Upload error: {e}")
        os.unlink(save_path)
        return jsonify({"error": str(e)}), 500


@app.route("/api/render/<sid>/<int:page>")
def api_render(sid: str, page: int):
    """Render a PDF page at given DPI and return as PNG."""
    sess = get_session(sid)
    if not sess:
        return jsonify({"error": "Session not found"}), 404

    dpi = int(request.args.get("dpi", PREVIEW_DPI))
    dpi = max(72, min(dpi, 300))

    try:
        png_bytes = render_page_to_png(sess["path"], page - 1, dpi=dpi)
        return send_file(
            io.BytesIO(png_bytes),
            mimetype="image/png",
            as_attachment=False,
        )
    except Exception as e:
        log.error(f"Render error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/crop-preview", methods=["POST"])
def api_crop_preview():
    """
    Render the cropped region at high resolution for preview.
    Body: {session_id, page, crop_mm: {x, y, w, h}}
    Returns: PNG image of cropped region.
    """
    data = request.get_json()
    sid = data.get("session_id")
    page = int(data.get("page", 1))
    crop_mm = data.get("crop_mm", {})

    sess = get_session(sid)
    if not sess:
        return jsonify({"error": "Session not found"}), 404

    try:
        pg_info = sess["pages_info"][page - 1]
        pg_h_mm = pg_info["height_mm"]

        # Convert mm to pt
        x_pt = mm_to_pt(crop_mm["x"])
        # PDF Y is from bottom; input Y is from top
        y_pt_from_bottom = mm_to_pt(pg_h_mm - crop_mm["y"] - crop_mm["h"])
        w_pt = mm_to_pt(crop_mm["w"])
        h_pt = mm_to_pt(crop_mm["h"])

        CROP_PREV_DPI = 200
        scale = CROP_PREV_DPI / 72.0

        doc = pdfium.PdfDocument(sess["path"])
        pg = doc[page - 1]
        pg_h_pt = pg.get_height()
        bitmap = pg.render(scale=scale, rotation=0)
        pil_full = bitmap.to_pil()
        doc.close()

        # Crop
        left = int(x_pt * scale)
        upper = int((pg_h_pt - y_pt_from_bottom - h_pt) * scale)
        right = min(pil_full.width, left + int(w_pt * scale))
        lower = min(pil_full.height, upper + int(h_pt * scale))
        pil_crop = pil_full.crop((max(0, left), max(0, upper), right, lower))

        buf = io.BytesIO()
        pil_crop.save(buf, "PNG")
        buf.seek(0)
        return send_file(buf, mimetype="image/png")

    except Exception as e:
        log.error(f"Crop preview error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/export", methods=["POST"])
def api_export():
    """
    Generate and download the final layout PDF.
    Body JSON:
    {
        session_id, page,
        crop_mm: {x, y, w, h},
        layout: {cols, rows},
        copies: int (optional, defaults to cols*rows),
        output_size: "A4"|"A3"|"Letter"|"A5",
        orientation: "portrait"|"landscape",
        margin_mm, gutter_mm,
        maintain_ar, center_items, cut_lines
    }
    """
    data = request.get_json()
    sid = data.get("session_id")
    sess = get_session(sid)
    if not sess:
        return jsonify({"error": "Session not found"}), 404

    try:
        page = int(data.get("page", 1))
        crop_mm = data["crop_mm"]
        layout = data.get("layout", {"cols": 2, "rows": 2})
        output_size = data.get("output_size", "A4")
        orientation = data.get("orientation", "portrait")
        margin_mm = float(data.get("margin_mm", 5))
        gutter_mm = float(data.get("gutter_mm", 3))
        maintain_ar = bool(data.get("maintain_ar", True))
        center_items = bool(data.get("center_items", True))
        cut_lines = bool(data.get("cut_lines", False))

        cols = int(layout["cols"])
        rows = int(layout["rows"])
        copies = max(1, min(cols * rows, int(data.get("copies", cols * rows))))

        pg_info = sess["pages_info"][page - 1]
        pg_h_mm = pg_info["height_mm"]

        # Convert crop mm → PDF points (y from bottom)
        crop_pt = {
            "x": mm_to_pt(crop_mm["x"]),
            "y": mm_to_pt(pg_h_mm - crop_mm["y"] - crop_mm["h"]),  # flip Y
            "w": mm_to_pt(crop_mm["w"]),
            "h": mm_to_pt(crop_mm["h"]),
        }

        output_size_mm = PAGE_SIZES_MM.get(output_size, PAGE_SIZES_MM["A4"])

        pdf_bytes = build_layout_pdf(
            source_pdf_path=sess["path"],
            page_index=page - 1,
            crop_pt=crop_pt,
            output_size_mm=output_size_mm,
            cols=cols,
            rows=rows,
            copies=copies,
            margin_mm=margin_mm,
            gutter_mm=gutter_mm,
            maintain_ar=maintain_ar,
            center_items=center_items,
            cut_lines=cut_lines,
            orientation=orientation,
        )

        log.info(f"Exported PDF [{sid}]: {copies} of {cols}×{rows} on {output_size} {orientation}")
        return send_file(
            io.BytesIO(pdf_bytes),
            mimetype="application/pdf",
            as_attachment=True,
            download_name="cropped-layout.pdf",
        )

    except Exception as e:
        log.error(f"Export error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/crop-preview-img/<sid>/<int:page>")
def api_crop_preview_img(sid: str, page: int):
    """
    Serve the current crop region as a PNG image (for layout preview tiles).
    Crop coordinates are passed as query params: x,y,w,h in mm.
    """
    sess = get_session(sid)
    if not sess:
        return jsonify({"error": "Session not found"}), 404

    try:
        x_mm = float(request.args.get("x", 0))
        y_mm = float(request.args.get("y", 0))
        w_mm = float(request.args.get("w", 0))
        h_mm = float(request.args.get("h", 0))

        if w_mm <= 0 or h_mm <= 0:
            return jsonify({"error": "Invalid crop dimensions"}), 400

        pg_info = sess["pages_info"][page - 1]
        pg_h_mm = pg_info["height_mm"]

        CROP_DPI = 180
        scale    = CROP_DPI / 72.0

        doc = pdfium.PdfDocument(sess["path"])
        pg  = doc[page - 1]
        pg_h_pt = pg.get_height()

        bitmap   = pg.render(scale=scale, rotation=0)
        pil_full = bitmap.to_pil()
        doc.close()

        # Convert mm to crop pixels
        x_pt  = mm_to_pt(x_mm)
        h_pt  = mm_to_pt(h_mm)
        y_pt_bottom = mm_to_pt(pg_h_mm - y_mm - h_mm)

        left  = max(0, int(x_pt * scale))
        upper = max(0, int((pg_h_pt - y_pt_bottom - h_pt) * scale))
        right = min(pil_full.width,  left  + int(mm_to_pt(w_mm) * scale))
        lower = min(pil_full.height, upper + int(h_pt * scale))

        pil_crop = pil_full.crop((left, upper, right, lower))
        buf = io.BytesIO()
        pil_crop.save(buf, "PNG")
        buf.seek(0)
        return send_file(buf, mimetype="image/png",
                         max_age=0,
                         headers={"Cache-Control": "no-store"})

    except Exception as e:
        log.error(f"Crop img error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/session/<sid>", methods=["DELETE"])
def api_delete_session(sid: str):
    """Clean up a session and its uploaded file."""
    sess = sessions.pop(sid, None)
    if sess and os.path.exists(sess["path"]):
        os.unlink(sess["path"])
    return jsonify({"ok": True})


@app.errorhandler(413)
def too_large(e):
    return jsonify({"error": f"File too large. Maximum size is {MAX_UPLOAD_MB}MB."}), 413


# ─── ENTRY POINT ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n" + "═" * 56)
    print("  PDF Crop & Layout Studio")
    print("  Running at http://localhost:5000")
    print("═" * 56 + "\n")
    app.run(debug=True, host="0.0.0.0", port=5000)
