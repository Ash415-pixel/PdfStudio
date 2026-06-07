"""
PDF Crop & Layout Studio — Flask Backend
========================================
Handles PDF upload, page rendering, crop operations, and multi-instance layout export.
"""

import os
import io
import re
import uuid
import json
import base64
import logging
import sqlite3
import tempfile
import threading
from datetime import datetime, timezone
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
DB_PATH    = BASE_DIR / "transactions.db"

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

# pdfium is NOT thread-safe for concurrent renders on the same file.
# All pdfium calls must hold this lock.
_pdfium_lock = threading.Lock()

# Crop-preview image cache  {(sid, page, x, y, w, h) -> png_bytes}
# Avoids re-rendering the same region for every grid cell in the layout preview.
_crop_cache: dict[tuple, bytes] = {}


# ─── DATABASE ──────────────────────────────────────────────────────────────────

def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp        TEXT NOT NULL,
            filename         TEXT,
            page             INTEGER,
            product_details  TEXT,
            customer_address TEXT,
            price_details    TEXT,
            grid_cols        INTEGER,
            grid_rows        INTEGER,
            copies           INTEGER,
            output_size      TEXT,
            orientation      TEXT
        )
    """)
    conn.commit()
    conn.close()


def save_transaction(filename: str, page: int, product: str, address: str, price: str,
                     cols: int, rows: int, copies: int, output_size: str, orientation: str) -> int:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        """INSERT INTO transactions
           (timestamp, filename, page, product_details, customer_address, price_details,
            grid_cols, grid_rows, copies, output_size, orientation)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (ts, filename, page, product, address, price, cols, rows, copies, output_size, orientation),
    )
    row_id = cur.lastrowid
    conn.commit()
    conn.close()
    return row_id


# ─── HELPERS ───────────────────────────────────────────────────────────────────

def new_session_id() -> str:
    return str(uuid.uuid4())


def get_session(sid: str) -> Optional[dict]:
    return sessions.get(sid)


init_db()


# ── Invoice extraction helpers ──────────────────────────────────────────────────
# Marks right-column seller content that can appear on the same line as the
# customer address in two-column invoice layouts — strip everything from here.
_INV_RIGHT_COL = re.compile(
    r'(?:Sold\s+by\s*[:\-]|Enrolment\s+No|Original\s+For\s+Recipient'
    r'|Invoice\s+No\.?|Order\s+Date|Invoice\s+Date)',
    re.IGNORECASE,
)
_CURRENCY_RE = re.compile(r'Rs\.|₹|\$|€|£', re.IGNORECASE)


def _inv_product(lines: list, text: str) -> str:
    """Product Details block (SKU/Size/Qty/Color/Order No.) + Description table items."""
    parts = []

    # 1. "Product Details" section: SKU header row + values row
    in_section = False
    sku_found = False
    for i, line in enumerate(lines):
        if re.search(r'\bProduct\s+Details\b', line, re.IGNORECASE):
            in_section = True
            continue
        if not in_section:
            continue
        if re.search(r'\bSKU\b', line, re.IGNORECASE):
            parts.append(line)          # "SKU  Size  Qty  Color  Order No."
            sku_found = True
            continue
        if sku_found:
            parts.append(line)          # "TGL007  Free Size  1  White  29260..."
            break
        if re.search(r'BILL\s+OF\s+SUPPLY|COMMERCIAL\s+INVOICE', line, re.IGNORECASE):
            break

    # 2. Item rows from the Description table (before Other Charges / Total)
    desc_idx = next(
        (i for i, l in enumerate(lines) if re.match(r'^\s*Description\b', l, re.IGNORECASE)), -1
    )
    if desc_idx >= 0:
        for line in lines[desc_idx + 1:]:
            if re.match(r'^\s*(Other\s+Charges|Total)\b', line, re.IGNORECASE):
                break
            if line and not re.match(r'^\s*(Qty|Gross\s+Amount|Discount)\b', line, re.IGNORECASE):
                parts.append(f"Item: {line}")

    return "\n".join(parts) if parts else "\n".join(lines[:3])


def _strip_seller_column(line: str) -> str:
    """
    Remove right-column seller content from a merged two-column line.
    Two strategies applied in order:
      1. Split on 2+ spaces (PDF column gap preserved by pypdf)
      2. Detect CamelCase company names (e.g. TheGiftingLamp) that appear
         after recognisable residential-address keywords.
    """
    # Strategy 1: 2+ spaces = column separator
    parts = re.split(r'\s{2,}', line)
    line = parts[0].strip() if len(parts) > 1 else line

    # Strategy 2: CamelCase brand/company name after a residential address keyword
    for m in re.finditer(r'\b[A-Z][a-z]+(?:[A-Z][a-z]*)+\b', line):
        if m.start() > 10 and re.search(
            r'\b(?:building|wing|flat|floor|house|road|street|colony|nagar|society|village|sector)\b',
            line[:m.start()], re.IGNORECASE,
        ):
            line = line[:m.start()].rstrip(' ,')
            break

    return line.strip()


def _inv_address(lines: list) -> str:
    """
    Customer name + address from the BILL TO / SHIP TO section.
    Strips right-column seller content from every line.
    Stops after the line containing the 6-digit customer pincode.
    """
    bill_idx = next(
        (i for i, l in enumerate(lines) if re.search(r'BILL\s+TO', l, re.IGNORECASE)), -1
    )
    if bill_idx < 0:
        return _fallback_address(lines)

    addr = []
    for line in lines[bill_idx:]:
        if re.match(r'^\s*(?:Description\s|Order\s+No\.\s+Invoice)', line, re.IGNORECASE):
            break

        # Strip right-column seller content via known markers
        m = _INV_RIGHT_COL.search(line)
        cleaned = line[:m.start()].strip() if m else line.strip()

        # Further strip seller content from merged two-column lines
        cleaned = _strip_seller_column(cleaned)

        # Remove section header label
        cleaned = re.sub(
            r'BILL\s+TO\s*/?\s*SHIP\s+TO\s*|BILL\s+TO\s*',
            '', cleaned, flags=re.IGNORECASE,
        ).strip()

        if cleaned:
            addr.append(cleaned)

        # Stop after the customer's 6-digit pincode
        if addr and re.search(r'\b\d{6}\b', cleaned):
            break

    return "\n".join(addr) if addr else _fallback_address(lines)


def _fallback_address(lines: list) -> str:
    _addr_re = re.compile(
        r'(\d+\s+\w[\w\s]+(?:street|st|avenue|ave|road|rd|lane|ln|drive|dr|blvd|way|nagar|colony)\b'
        r'|\b(?:pin|zip|postal)\s*[:\-]?\s*\d{4,6}'
        r'|\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b|\b\d{6}\b)',
        re.IGNORECASE,
    )
    return "\n".join(ln for ln in lines if _addr_re.search(ln))


def _inv_price(lines: list, text: str) -> str:
    """Description table rows with currency values + grand total."""
    parts = []

    desc_idx = next(
        (i for i, l in enumerate(lines) if re.match(r'^\s*Description\b', l, re.IGNORECASE)), -1
    )
    if desc_idx >= 0:
        for line in lines[desc_idx + 1:]:
            if re.match(r'^\s*(Qty|Gross\s+Amount|Discount)\b', line, re.IGNORECASE):
                continue
            if _CURRENCY_RE.search(line) or re.match(r'^\s*Total\b', line, re.IGNORECASE):
                parts.append(line)

    # Grand total (last "Total Rs.X" in text)
    totals = re.findall(
        r'\bTotal\b[:\s]*(Rs\.[\d,.]+|\$[\d,.]+|€[\d,.]+|£[\d,.]+|₹[\d,.]+)',
        text, re.IGNORECASE,
    )
    if totals and not any("Grand Total" in p for p in parts):
        parts.append(f"Grand Total: {totals[-1]}")

    # Fallback for non-invoice PDFs
    if not parts:
        _price_re = re.compile(
            r'(\$[\d,]+\.?\d*|€[\d,]+\.?\d*|£[\d,]+\.?\d*|₹[\d,]+\.?\d*'
            r'|Rs\.[\d,]+\.?\d*|[\d,]+\.\d{2}\s*(?:USD|EUR|GBP|INR)'
            r'|\b(?:total|price|amount|subtotal|tax|gst|vat)\s*[:\-]?\s*[\$€£₹]?[\d,]+\.?\d*)',
            re.IGNORECASE,
        )
        parts = [ln for ln in lines if _price_re.search(ln)]

    return "\n".join(parts)


def extract_fields_from_pdf(pdf_path: str, page_index: int) -> dict:
    """
    Extract product details, billing address, and price summary from a PDF page.
    Handles structured e-commerce/commercial invoice formats (Product Details block,
    Bill To/Ship To section, Description table). Falls back to heuristic matching
    for other PDF types.
    """
    try:
        reader = PdfReader(pdf_path)
        text = reader.pages[page_index].extract_text() or ""
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

        return {
            "raw_text":        text,
            "product_details": _inv_product(lines, text),
            "customer_address": _inv_address(lines),
            "price_details":   _inv_price(lines, text),
        }
    except Exception as exc:
        log.warning(f"Text extraction failed: {exc}")
        return {"raw_text": "", "product_details": "", "customer_address": "", "price_details": ""}


def render_page_to_png(pdf_path: str, page_index: int, dpi: int = PREVIEW_DPI) -> bytes:
    """Render a single PDF page to PNG bytes using pypdfium2."""
    with _pdfium_lock:
        doc = pdfium.PdfDocument(pdf_path)
        try:
            page = doc[page_index]
            scale = dpi / 72.0
            bitmap = page.render(scale=scale, rotation=0)
            pil_img = bitmap.to_pil()
        finally:
            doc.close()
    buf = io.BytesIO()
    pil_img.save(buf, format="PNG", optimize=True)
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
    log.info(f"[BUILD] start — src={source_pdf_path} page={page_index} crop={crop_pt} "
             f"grid={cols}×{rows} copies={copies} size={output_size_mm} orient={orientation}")

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
    log.info(f"[BUILD] page={page_w_pt:.1f}×{page_h_pt:.1f}pt  cell={cell_w_pt:.1f}×{cell_h_pt:.1f}pt")

    crop_w_pt = crop_pt["w"]
    crop_h_pt = crop_pt["h"]
    crop_ar = crop_w_pt / crop_h_pt if crop_h_pt > 0 else 1.0

    # ── Render crop region to a high-res image for embedding ──────────────────
    EXPORT_DPI = 150  # 300 DPI → ~35 MB bitmap per A4 page; 150 DPI is sufficient and stable
    scale = EXPORT_DPI / 72.0
    log.info(f"[BUILD] rendering page at {EXPORT_DPI} DPI (scale={scale:.3f})")

    with _pdfium_lock:
        doc = pdfium.PdfDocument(source_pdf_path)
        try:
            page = doc[page_index]
            pg_width_pt = page.get_width()
            pg_height_pt = page.get_height()
            log.info(f"[BUILD] source page size: {pg_width_pt:.1f}×{pg_height_pt:.1f}pt")

            # Convert crop from PDF-pt coordinates to render pixels
            crop_x_px = int(crop_pt["x"] * scale)
            crop_y_px = int((pg_height_pt - crop_pt["y"] - crop_h_pt) * scale)  # flip Y
            crop_w_px = int(crop_w_pt * scale)
            crop_h_px = int(crop_h_pt * scale)

            # Render the full page then crop
            bitmap   = page.render(scale=scale, rotation=0)
            pil_full = bitmap.to_pil()
        finally:
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
    result = out_buf.getvalue()
    log.info(f"[BUILD] done — output PDF is {len(result)} bytes")
    return result


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
            "filename": f.filename,
            "page_count": page_count,
            "pages_info": [],
        }

        # Pre-cache page dimensions
        with _pdfium_lock:
            doc = pdfium.PdfDocument(save_path)
            try:
                for i in range(page_count):
                    pg = doc[i]
                    sessions[sid]["pages_info"].append({
                        "width_pt": pg.get_width(),
                        "height_pt": pg.get_height(),
                        "width_mm": pg.get_width() / MM_TO_PT,
                        "height_mm": pg.get_height() / MM_TO_PT,
                    })
            finally:
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

        with _pdfium_lock:
            doc = pdfium.PdfDocument(sess["path"])
            try:
                pg = doc[page - 1]
                pg_h_pt = pg.get_height()
                bitmap = pg.render(scale=scale, rotation=0)
                pil_full = bitmap.to_pil()
            finally:
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
        maintain_ar, center_items, cut_lines,
        transaction: {product_details, customer_address, price_details}  (optional)
    }
    """
    data = request.get_json()
    sid = data.get("session_id")
    log.info(f"[EXPORT] ── START  sid={sid}")

    sess = get_session(sid)
    if not sess:
        log.warning(f"[EXPORT] Session not found: {sid}")
        return jsonify({"error": "Session not found"}), 404

    # ── Step 1: parse request ────────────────────────────────────────────────
    try:
        page        = int(data.get("page", 1))
        crop_mm     = data["crop_mm"]
        layout      = data.get("layout", {"cols": 2, "rows": 2})
        output_size = data.get("output_size", "A4")
        orientation = data.get("orientation", "portrait")
        margin_mm   = float(data.get("margin_mm", 5))
        gutter_mm   = float(data.get("gutter_mm", 3))
        maintain_ar  = bool(data.get("maintain_ar", True))
        center_items = bool(data.get("center_items", True))
        cut_lines    = bool(data.get("cut_lines", False))
        cols   = int(layout["cols"])
        rows   = int(layout["rows"])
        copies = max(1, min(cols * rows, int(data.get("copies", cols * rows))))
        log.info(
            f"[EXPORT] Params parsed — page={page} crop={crop_mm} "
            f"grid={cols}×{rows} copies={copies} size={output_size} orient={orientation}"
        )
    except Exception as e:
        log.error(f"[EXPORT] Bad request params: {e}", exc_info=True)
        return jsonify({"error": f"Invalid request: {e}"}), 400

    # ── Step 2: build the PDF ────────────────────────────────────────────────
    try:
        pg_info  = sess["pages_info"][page - 1]
        pg_h_mm  = pg_info["height_mm"]
        crop_pt  = {
            "x": mm_to_pt(crop_mm["x"]),
            "y": mm_to_pt(pg_h_mm - crop_mm["y"] - crop_mm["h"]),
            "w": mm_to_pt(crop_mm["w"]),
            "h": mm_to_pt(crop_mm["h"]),
        }
        output_size_mm = PAGE_SIZES_MM.get(output_size, PAGE_SIZES_MM["A4"])
        log.info(f"[EXPORT] Calling build_layout_pdf — crop_pt={crop_pt}")

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
        log.info(f"[EXPORT] PDF built — {len(pdf_bytes)} bytes")
    except Exception as e:
        log.error(f"[EXPORT] PDF build FAILED: {e}", exc_info=True)
        return jsonify({"error": f"PDF generation failed: {e}"}), 500

    # ── Step 3: log transaction (never blocks the download) ─────────────────
    try:
        txn = data.get("transaction", {})
        log.info(f"[EXPORT] Saving transaction — product={txn.get('product_details','')!r:.40}")
        save_transaction(
            filename=sess.get("filename", ""),
            page=page,
            product=txn.get("product_details", ""),
            address=txn.get("customer_address", ""),
            price=txn.get("price_details", ""),
            cols=cols,
            rows=rows,
            copies=copies,
            output_size=output_size,
            orientation=orientation,
        )
        log.info(f"[EXPORT] Transaction saved OK")
    except Exception as db_err:
        log.error(f"[EXPORT] Transaction save FAILED (download still proceeding): {db_err}", exc_info=True)

    # ── Step 4: send the file ────────────────────────────────────────────────
    log.info(f"[EXPORT] ── DONE  sid={sid}  {cols}×{rows}  {copies} copies  {output_size} {orientation}")
    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name="cropped-layout.pdf",
    )


@app.route("/api/crop-preview-img/<sid>/<int:page>")
def api_crop_preview_img(sid: str, page: int):
    """
    Serve the cropped region as PNG for layout preview tiles.
    Results are cached per (sid, page, x, y, w, h) so a 2×2 grid
    only triggers one pdfium render instead of four concurrent ones.
    """
    sess = get_session(sid)
    if not sess:
        log.warning(f"[CROP-IMG] session not found: {sid}")
        return jsonify({"error": "Session not found"}), 404

    try:
        x_mm = round(float(request.args.get("x", 0)), 3)
        y_mm = round(float(request.args.get("y", 0)), 3)
        w_mm = round(float(request.args.get("w", 0)), 3)
        h_mm = round(float(request.args.get("h", 0)), 3)

        if w_mm <= 0 or h_mm <= 0:
            log.warning(f"[CROP-IMG] invalid dimensions w={w_mm} h={h_mm}")
            return jsonify({"error": "Invalid crop dimensions"}), 400

        cache_key = (sid, page, x_mm, y_mm, w_mm, h_mm)

        # Return cached PNG if available (avoids concurrent pdfium renders)
        if cache_key in _crop_cache:
            log.debug(f"[CROP-IMG] cache hit {cache_key}")
            return send_file(io.BytesIO(_crop_cache[cache_key]), mimetype="image/png",
                             max_age=0, headers={"Cache-Control": "no-store"})

        log.info(f"[CROP-IMG] render sid={sid} page={page} x={x_mm} y={y_mm} w={w_mm} h={h_mm}")

        pg_info = sess["pages_info"][page - 1]
        pg_h_mm = pg_info["height_mm"]

        CROP_DPI = 150
        scale    = CROP_DPI / 72.0

        with _pdfium_lock:
            doc = pdfium.PdfDocument(sess["path"])
            try:
                pg      = doc[page - 1]
                pg_h_pt = pg.get_height()
                bitmap  = pg.render(scale=scale, rotation=0)
                pil_full = bitmap.to_pil()
            finally:
                doc.close()

        # mm → pixels (Y-axis flip: PDF origin is bottom-left)
        x_pt        = mm_to_pt(x_mm)
        h_pt        = mm_to_pt(h_mm)
        y_pt_bottom = mm_to_pt(pg_h_mm - y_mm - h_mm)

        left  = max(0, int(x_pt * scale))
        upper = max(0, int((pg_h_pt - y_pt_bottom - h_pt) * scale))
        right = min(pil_full.width,  left  + int(mm_to_pt(w_mm) * scale))
        lower = min(pil_full.height, upper + int(h_pt * scale))

        log.info(f"[CROP-IMG] pixel crop: left={left} upper={upper} right={right} lower={lower} "
                 f"(image={pil_full.width}×{pil_full.height})")

        if right <= left or lower <= upper:
            log.error(f"[CROP-IMG] zero-size crop box after coordinate conversion")
            return jsonify({"error": "Crop region is zero-size"}), 400

        pil_crop = pil_full.crop((left, upper, right, lower))
        buf = io.BytesIO()
        pil_crop.save(buf, "PNG")
        png_bytes = buf.getvalue()

        # Cache the result
        _crop_cache[cache_key] = png_bytes
        log.info(f"[CROP-IMG] done — {len(png_bytes)} bytes, cached")

        return send_file(io.BytesIO(png_bytes), mimetype="image/png",
                         max_age=0, headers={"Cache-Control": "no-store"})

    except Exception as e:
        log.error(f"[CROP-IMG] FAILED: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/session/<sid>", methods=["DELETE"])
def api_delete_session(sid: str):
    """Clean up a session and its uploaded file."""
    sess = sessions.pop(sid, None)
    if sess and os.path.exists(sess["path"]):
        os.unlink(sess["path"])
    # Evict this session's entries from the crop preview cache
    stale = [k for k in _crop_cache if k[0] == sid]
    for k in stale:
        del _crop_cache[k]
    return jsonify({"ok": True})


@app.route("/api/extract-fields", methods=["POST"])
def api_extract_fields():
    """Extract product/address/price text from a PDF page using heuristics."""
    data = request.get_json()
    sid  = data.get("session_id")
    page = int(data.get("page", 1))
    sess = get_session(sid)
    if not sess:
        return jsonify({"error": "Session not found"}), 404
    result = extract_fields_from_pdf(sess["path"], page - 1)
    return jsonify(result)


@app.route("/api/transactions", methods=["GET"])
def api_get_transactions():
    """Return all stored transactions, newest first."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM transactions ORDER BY id DESC LIMIT 200"
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/transactions", methods=["POST"])
def api_save_transaction_manual():
    """Save a transaction log entry manually (without exporting a PDF)."""
    data = request.get_json() or {}
    try:
        row_id = save_transaction(
            filename=data.get("filename", ""),
            page=int(data.get("page", 1)),
            product=data.get("product_details", ""),
            address=data.get("customer_address", ""),
            price=data.get("price_details", ""),
            cols=int(data.get("grid_cols", 1)),
            rows=int(data.get("grid_rows", 1)),
            copies=int(data.get("copies", 1)),
            output_size=data.get("output_size", ""),
            orientation=data.get("orientation", ""),
        )
        return jsonify({"id": row_id})
    except Exception as e:
        log.error(f"Manual transaction save error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/transactions/<int:txn_id>", methods=["DELETE"])
def api_delete_transaction(txn_id: int):
    """Delete a single transaction by ID."""
    try:
        conn = sqlite3.connect(DB_PATH)
        deleted = conn.execute(
            "DELETE FROM transactions WHERE id = ?", (txn_id,)
        ).rowcount
        conn.commit()
        conn.close()
        if deleted == 0:
            return jsonify({"error": "Transaction not found"}), 404
        log.info(f"Deleted transaction id={txn_id}")
        return jsonify({"ok": True})
    except Exception as e:
        log.error(f"Delete transaction error: {e}")
        return jsonify({"error": str(e)}), 500


@app.errorhandler(413)
def too_large(e):
    return jsonify({"error": f"File too large. Maximum size is {MAX_UPLOAD_MB}MB."}), 413


# ─── ENTRY POINT ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n" + "═" * 56)
    print("  PDF Crop & Layout Studio")
    print("  Running at http://localhost:5000")
    print("═" * 56 + "\n")
    # use_reloader=False: keeps a single process so _pdfium_lock works correctly.
    # The Werkzeug reloader spawns a child process per request cycle which gives
    # each child its own copy of the lock, defeating the thread-safety guarantee.
    app.run(debug=True, host="0.0.0.0", port=5000, use_reloader=False, threaded=True)
