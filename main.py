"""
PPS – Pre Production Service
Backend API · Version 1.3.8
FastAPI + PyMuPDF · Developed for DCP
"""

from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
import fitz  # PyMuPDF
import io
import struct
import zlib
import math
from typing import Optional

app = FastAPI(title="PPS API", version="1.3.6")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

PT_TO_MM = 25.4 / 72


# ─────────────────────────────────────────────
#  ANALYSE ENDPOINT
# ─────────────────────────────────────────────
@app.post("/analyze")
async def analyze(
    file: UploadFile = File(...),
    print_width_mm: float = Form(...),
    print_height_mm: float = Form(...),
    scale: int = Form(10),
    job_name: Optional[str] = Form(""),
):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Nur PDF-Dateien erlaubt.")

    data = await file.read()
    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(413, "Datei zu groß (max. 50 MB).")

    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        raise HTTPException(422, "PDF konnte nicht geöffnet werden.")

    result = run_analysis(doc, data, print_width_mm, print_height_mm, scale, job_name, file.filename)
    doc.close()
    return result


# ─────────────────────────────────────────────
#  FIX ENDPOINT
# ─────────────────────────────────────────────
@app.post("/fix")
async def fix_pdf(
    file: UploadFile = File(...),
    print_width_mm: float = Form(...),
    print_height_mm: float = Form(...),
    scale: int = Form(10),
    fix_cropmarks: bool = Form(True),
    fix_bleed: bool = Form(True),
    fix_colorspace: bool = Form(False),
):
    data = await file.read()
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        raise HTTPException(422, "PDF konnte nicht geöffnet werden.")

    fixed_pdf, fixes_applied = apply_fixes(
        doc, data, print_width_mm, print_height_mm, scale,
        fix_cropmarks, fix_bleed, fix_colorspace
    )
    doc.close()

    filename = file.filename.replace(".pdf", "") + "_PPS_fixed.pdf"
    return Response(
        content=fixed_pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"',
                 "X-Fixes-Applied": ", ".join(fixes_applied)}
    )


# ─────────────────────────────────────────────
#  CORE ANALYSIS LOGIC
# ─────────────────────────────────────────────
def run_analysis(doc, raw_bytes, print_w, print_h, scale, job_name, filename):
    page = doc[0]
    page_count = len(doc)

    # Page dimensions
    rect = page.rect
    pdf_w_mm = rect.width * PT_TO_MM
    pdf_h_mm = rect.height * PT_TO_MM

    # ── Box-Analyse ──
    mediabox = page.mediabox
    trimbox  = page.trimbox if page.trimbox != page.mediabox else None

    media_w_mm = (mediabox.x1 - mediabox.x0) * PT_TO_MM
    media_h_mm = (mediabox.y1 - mediabox.y0) * PT_TO_MM
    trim_w_mm  = (trimbox.x1 - trimbox.x0) * PT_TO_MM if trimbox else media_w_mm
    trim_h_mm  = (trimbox.y1 - trimbox.y0) * PT_TO_MM if trimbox else media_h_mm

    expected_w_mm    = print_w / scale
    expected_h_mm    = print_h / scale
    expected_bleed_mm = 20.0 / scale

    # ── Beschnittzeichen zuerst erkennen ──
    # Wichtig: BEVOR wir Beschnitt berechnen, damit wir nicht
    # Beschnittzeichen-Fläche als echten Beschnitt werten
    has_cropmarks = detect_cropmarks(page, media_w_mm, media_h_mm, trim_w_mm, trim_h_mm)

    # ── Echter Beschnitt berechnen ──
    # Wenn Beschnittzeichen vorhanden: MediaBox-Überschuss = Beschnittzeichen-Bereich
    # → kein echter Beschnitt vorhanden
    # Wenn keine Beschnittzeichen: MediaBox-Überschuss = echter Beschnitt
    if has_cropmarks and trimbox:
        # TrimBox definiert den Druckbereich — Beschnitt ist 0 weil nur Zeichen außen
        bleed_mm = 0.0
    elif trimbox:
        # BleedBox = MediaBox, TrimBox kleiner → echter Beschnitt
        bleed_mm = ((media_w_mm - trim_w_mm) / 2 + (media_h_mm - trim_h_mm) / 2) / 2
    else:
        # Keine TrimBox → kein Beschnitt erkennbar
        bleed_mm = 0.0

    # ── Ratio check (immer auf TrimBox-Basis) ──
    ratio_diff_w = abs(trim_w_mm - expected_w_mm)
    ratio_diff_h = abs(trim_h_mm - expected_h_mm)
    ratio_ok = ratio_diff_w < 3 and ratio_diff_h < 3

    # ── Bleed Status ──
    bleed_diff = abs(bleed_mm - expected_bleed_mm)
    if bleed_mm < 0.5:
        bleed_status = "error"
    elif bleed_diff > 1.5:
        bleed_status = "warn"
    else:
        bleed_status = "ok"

    # Image resolution analysis (the key feature)
    images = analyze_images(page, doc, print_w, print_h, scale)

    # Font analysis
    font_info = analyze_fonts(doc)

    # Color space & ICC
    color_info = analyze_colorspace(page, doc, raw_bytes)

    # File size
    file_size_mb = len(raw_bytes) / 1024 / 1024

    # Build checks
    checks = []

    # 1. Page count
    checks.append({
        "id": "pages",
        "label": "Seitenanzahl",
        "status": "ok" if page_count == 1 else "error",
        "value": f"{page_count} Seite{'n' if page_count != 1 else ''}",
        "note": "Einzelseiten-PDF — korrekt." if page_count == 1
                else f"PDF enthält {page_count} Seiten. Bitte nur Einzelseiten liefern.",
        "fixable": False,
        "details": {}
    })

    # 2. Aspect ratio
    checks.append({
        "id": "ratio",
        "label": "Seitenverhältnis",
        "status": "ok" if ratio_ok else "error",
        "value": f"PDF (Netto): {trim_w_mm:.1f} × {trim_h_mm:.1f} mm · Erwartet: {expected_w_mm:.1f} × {expected_h_mm:.1f} mm",
        "note": f"Verhältnis stimmt überein (1:{scale})." if ratio_ok
                else f"Abweichung: Breite {ratio_diff_w:+.1f} mm, Höhe {ratio_diff_h:+.1f} mm. Bitte Datei prüfen.",
        "fixable": False,
        "details": {"trim_w": round(trim_w_mm, 1), "trim_h": round(trim_h_mm, 1),
                    "expected_w": round(expected_w_mm, 1), "expected_h": round(expected_h_mm, 1)}
    })

    # 3. Bleed — PRIORITÄT: wichtiger als Beschnittzeichen
    bleed_note = {
        "ok": f"Beschnittzugabe korrekt ({bleed_mm:.1f} mm = {bleed_mm*scale:.0f} mm bei 1:1).",
        "warn": f"Zugabe {bleed_mm:.1f} mm weicht vom Sollwert {expected_bleed_mm:.1f} mm ab.",
        "error": f"Keine Beschnittzugabe erkannt! Benötigt: {expected_bleed_mm:.1f} mm (= 20 mm bei 1:1). Fix It erstellt Beschnitt durch Randspiegelung."
    }[bleed_status]
    if has_cropmarks and bleed_status == "error":
        bleed_note = f"Beschnittzeichen vorhanden, aber KEINE echte Beschnittzugabe! Nach dem Entfernen der Zeichen fehlen {expected_bleed_mm:.1f} mm Beschnitt. Fix It löst beides."
    checks.append({
        "id": "bleed",
        "label": "Beschnittzugabe",
        "status": bleed_status,
        "value": f"{bleed_mm:.1f} mm erkannt · Erwartet: {expected_bleed_mm:.1f} mm (= 20 mm bei 1:1)",
        "note": bleed_note,
        "fixable": bleed_status != "ok",
        "details": {"bleed_mm": round(bleed_mm, 2), "expected_mm": round(expected_bleed_mm, 2),
                    "has_cropmarks": has_cropmarks}
    })

    # 4. Crop marks — als Warnung, nicht als Fehler
    checks.append({
        "id": "cropmarks",
        "label": "Beschnittzeichen",
        "status": "warn" if has_cropmarks else "ok",
        "value": "Vorhanden — werden beim Fix entfernt" if has_cropmarks else "Keine vorhanden",
        "note": "Beschnittzeichen erkannt. Diese werden automatisch entfernt wenn du 'Fix It' für die Beschnittzugabe verwendest." if has_cropmarks
                else "Keine Beschnittzeichen erkannt — korrekt.",
        "fixable": False,
        "details": {"detected": has_cropmarks}
    })

    # 5. Image resolution
    min_dpi_doc = 50.0 / scale
    critical_dpi = 25.0 / scale
    img_status, img_value, img_note = evaluate_images(images, min_dpi_doc, critical_dpi, scale)
    checks.append({
        "id": "resolution",
        "label": "Bildauflösung",
        "status": img_status,
        "value": img_value,
        "note": img_note,
        "fixable": img_status in ("warn",),
        "details": {"images": images, "min_dpi": round(min_dpi_doc, 1), "critical_dpi": round(critical_dpi, 1)}
    })

    # 6. File size
    expected_min = (print_w / 1000) * (print_h / 1000) * 0.4
    fs_status = "ok"
    if file_size_mb < expected_min and len(images) > 0:
        fs_status = "warn"
    elif file_size_mb > 150:
        fs_status = "warn"
    checks.append({
        "id": "filesize",
        "label": "Dateigröße",
        "status": fs_status,
        "value": f"{file_size_mb:.2f} MB",
        "note": f"Plausibel für {print_w/1000:.1f}×{print_h/1000:.1f} m Druck." if fs_status == "ok"
                else f"{file_size_mb:.1f} MB ist {'gering' if file_size_mb < expected_min else 'sehr groß'} für dieses Format.",
        "fixable": False,
        "details": {"size_mb": round(file_size_mb, 2)}
    })

    # 7. Fonts
    font_status = "error" if font_info["has_unembedded"] else ("warn" if font_info["has_text"] else "ok")
    checks.append({
        "id": "fonts",
        "label": "Schriften",
        "status": font_status,
        "value": font_info["summary"],
        "note": {
            "ok": "Keine Textelemente gefunden — Schriften sind korrekt in Kurven umgewandelt.",
            "warn": "Schriften sind eingebettet, aber nicht in Kurven umgewandelt. Bitte in der Quell-App in Pfade konvertieren.",
            "error": "Nicht eingebettete Schriften gefunden! Schriftsubstitution beim Druck möglich."
        }[font_status],
        "fixable": False,
        "details": font_info
    })

    # 8. Color space
    cs_status = "ok" if color_info["is_cmyk"] else ("warn" if color_info["is_mixed"] else "error")
    checks.append({
        "id": "colorspace",
        "label": "Farbraum",
        "status": cs_status,
        "value": color_info["colorspace_summary"],
        "note": {
            "ok": "CMYK-Farbraum erkannt — korrekt für Textildruck.",
            "warn": "Gemischte Farbräume (CMYK + RGB). Bitte alles in CMYK konvertieren.",
            "error": "RGB-Farbraum erkannt. Für Textildruck ist CMYK mit ISO Coated v2 erforderlich."
        }[cs_status],
        "fixable": not color_info["is_cmyk"],
        "details": color_info
    })

    # 9. ICC Profile
    icc_status = "ok" if color_info["icc_ok"] else ("warn" if color_info["icc_name"] else "error")
    checks.append({
        "id": "icc",
        "label": "ICC-Profil",
        "status": icc_status,
        "value": color_info["icc_name"] or "Kein ICC-Profil eingebettet",
        "note": {
            "ok": f"Korrektes ICC-Profil gefunden: {color_info['icc_name']}",
            "warn": f"ICC-Profil '{color_info['icc_name']}' ist nicht das empfohlene ISO Coated v2 (ECI).",
            "error": "Kein ICC-Profil eingebettet. Bitte ISO Coated v2 (ECI) verwenden."
        }[icc_status],
        "fixable": not color_info["icc_ok"],
        "details": color_info
    })

    # Overall status
    statuses = [c["status"] for c in checks]
    if "error" in statuses:
        overall = "error"
    elif "warn" in statuses:
        overall = "warn"
    else:
        overall = "ok"

    error_count = statuses.count("error")
    warn_count  = statuses.count("warn")
    ok_count    = statuses.count("ok")

    summary = ""
    if overall == "ok":
        summary = f"Alle {ok_count} Checks bestanden. Die Datei ist druckfertig."
    elif overall == "error":
        summary = f"{error_count} Fehler und {warn_count} Warnungen gefunden. Bitte vor dem Druck korrigieren."
    else:
        summary = f"{warn_count} Hinweise. Bitte prüfen — der Druck könnte beeinträchtigt sein."

    return {
        "filename": filename,
        "job_name": job_name,
        "overall_status": overall,
        "summary": summary,
        "checks": checks,
        "meta": {
            "pdf_w_mm": round(pdf_w_mm, 1),
            "pdf_h_mm": round(pdf_h_mm, 1),
            "trim_w_mm": round(trim_w_mm, 1),
            "trim_h_mm": round(trim_h_mm, 1),
            "bleed_mm": round(bleed_mm, 2),
            "file_size_mb": round(file_size_mb, 2),
            "page_count": page_count,
            "scale": scale,
            "print_w_mm": print_w,
            "print_h_mm": print_h,
        }
    }


# ─────────────────────────────────────────────
#  IMAGE ANALYSIS  (the heart of PPS)
# ─────────────────────────────────────────────
def analyze_images(page, doc, print_w_mm, print_h_mm, scale):
    images = []
    image_list = page.get_images(full=True)

    for img_info in image_list:
        xref = img_info[0]
        try:
            base_image = doc.extract_image(xref)
            img_w_px = base_image["width"]
            img_h_px = base_image["height"]
            colorspace = base_image.get("colorspace", 0)
            cs_name = {1: "Graustufen", 3: "RGB", 4: "CMYK"}.get(colorspace, f"CS{colorspace}")

            # Get placement rectangle on page
            img_rects = page.get_image_rects(xref)
            if img_rects:
                r = img_rects[0]
                placed_w_mm = (r.x1 - r.x0) * PT_TO_MM
                placed_h_mm = (r.y1 - r.y0) * PT_TO_MM

                if placed_w_mm > 0 and placed_h_mm > 0:
                    dpi_x = img_w_px / (placed_w_mm / 25.4)
                    dpi_y = img_h_px / (placed_h_mm / 25.4)
                    dpi_effective = min(dpi_x, dpi_y)
                    # Bei 1:10: 300 DPI im Dokument = 30 DPI im Druck (1:1)
                    dpi_at_1to1 = dpi_effective / scale
                else:
                    dpi_effective = 0
                    dpi_at_1to1 = 0
            else:
                # Fallback: estimate from page coverage
                page_w_mm = page.rect.width * PT_TO_MM
                page_h_mm = page.rect.height * PT_TO_MM
                dpi_effective = min(img_w_px / (page_w_mm / 25.4), img_h_px / (page_h_mm / 25.4))
                dpi_at_1to1 = dpi_effective / scale

            images.append({
                "xref": xref,
                "width_px": img_w_px,
                "height_px": img_h_px,
                "dpi_in_doc": round(dpi_effective, 1),
                "dpi_at_1to1": round(dpi_at_1to1, 1),
                "colorspace": cs_name,
                "size_kb": round(len(base_image.get("image", b"")) / 1024, 1),
            })
        except Exception as e:
            images.append({"xref": xref, "error": str(e), "dpi_in_doc": 0, "dpi_at_1to1": 0})

    return images


def evaluate_images(images, min_dpi, critical_dpi, scale):
    if not images:
        return ("ok",
                "Keine eingebetteten Pixel-Bilder — reine Vektordatei.",
                "Ideal für Großformatdruck. Vektorgrafiken skalieren verlustfrei.")

    valid = [i for i in images if "dpi_in_doc" in i and i["dpi_in_doc"] > 0]
    if not valid:
        return ("warn", f"{len(images)} Bild(er) — DPI nicht messbar", "Auflösung konnte nicht bestimmt werden.")

    # dpi_at_1to1 ist die echte Druckauflösung (dpi_in_doc / scale)
    min_at_1to1 = min(i["dpi_at_1to1"] for i in valid)
    max_at_1to1 = max(i["dpi_at_1to1"] for i in valid)
    min_in_doc  = min(i["dpi_in_doc"]  for i in valid)
    max_in_doc  = max(i["dpi_in_doc"]  for i in valid)

    min_print = 50.0   # Minimum DPI bei 1:1
    crit_print = 25.0  # Kritisch — zu niedrig für Upscaling

    below_min  = [i for i in valid if i["dpi_at_1to1"] < min_print]
    below_crit = [i for i in valid if i["dpi_at_1to1"] < crit_print]

    value = (f"{len(images)} Bild(er) · "
             f"{min_in_doc:.0f}–{max_in_doc:.0f} DPI im Dokument "
             f"(= {min_at_1to1:.0f}–{max_at_1to1:.0f} DPI bei 1:1)")

    if below_crit:
        return ("error", value,
                f"{len(below_crit)} Bild(er) unter {crit_print:.0f} DPI (1:1). "
                f"Zu niedrig für Upscaling — bitte Originaldatei in höherer Auflösung liefern.")
    elif below_min:
        return ("warn", value,
                f"{len(below_min)} Bild(er) unter {min_print:.0f} DPI (1:1). "
                f"Upscaling (2×) über 'Fix It' möglich.")
    else:
        return ("ok", value,
                f"Alle Bilder erreichen mindestens {min_print:.0f} DPI bei 1:1. Auflösung ausreichend.")


# ─────────────────────────────────────────────
#  FONT ANALYSIS
# ─────────────────────────────────────────────
def analyze_fonts(doc):
    all_fonts = []
    has_unembedded = False
    has_text = False

    for page_num in range(len(doc)):
        page = doc[page_num]
        font_list = page.get_fonts(full=True)
        text = page.get_text("text").strip()
        if text:
            has_text = True

        for f in font_list:
            name  = f[3] or f[4] or "Unbekannt"
            embedded = f[2] != ""
            if not embedded:
                has_unembedded = True
            all_fonts.append({"name": name, "embedded": embedded})

    unique = {f["name"]: f for f in all_fonts}
    if not has_text and not unique:
        summary = "Kein Text — Schriften korrekt in Kurven umgewandelt"
    elif has_unembedded:
        missing = [n for n, f in unique.items() if not f["embedded"]]
        summary = f"Nicht eingebettet: {', '.join(missing[:3])}"
    elif has_text:
        summary = f"{len(unique)} Schrift(en) eingebettet (aber noch nicht in Kurven)"
    else:
        summary = "Kein eingebetteter Text gefunden"

    return {
        "has_text": has_text,
        "has_unembedded": has_unembedded,
        "font_count": len(unique),
        "fonts": list(unique.values())[:10],
        "summary": summary
    }


# ─────────────────────────────────────────────
#  COLOR SPACE & ICC ANALYSIS
# ─────────────────────────────────────────────
def analyze_colorspace(page, doc, raw_bytes):
    is_cmyk = False
    is_rgb  = False
    is_mixed = False
    icc_name = ""
    icc_ok   = False

    # Check images colorspaces
    image_list = page.get_images(full=True)
    cs_set = set()
    for img_info in image_list:
        xref = img_info[0]
        try:
            base_image = doc.extract_image(xref)
            cs = base_image.get("colorspace", 0)
            cs_set.add(cs)
        except Exception:
            pass

    if 4 in cs_set and 3 not in cs_set:
        is_cmyk = True
    elif 3 in cs_set and 4 not in cs_set:
        is_rgb = True
    elif 4 in cs_set and 3 in cs_set:
        is_mixed = True
        is_cmyk = False

    # ICC profile extraction from raw PDF bytes
    icc_markers = [b"/ICCBased", b"ICCProfile", b"icc", b"ICC"]
    raw_lower = raw_bytes.lower()

    known_profiles = {
        b"iso coated v2": "ISO Coated v2 (ECI)",
        b"isocoated_v2": "ISO Coated v2 (ECI)",
        b"fogra39": "ISO Coated v2 / Fogra39",
        b"fogra51": "ISO Uncoated v2 / Fogra51",
        b"srgb": "sRGB IEC61966",
        b"adobe rgb": "Adobe RGB (1998)",
        b"p3": "Display P3",
    }
    for marker, name in known_profiles.items():
        if marker in raw_lower:
            icc_name = name
            icc_ok = "iso coated v2" in name.lower() or "fogra39" in name.lower()
            break

    if not icc_name:
        # Try to find any ICC keyword
        if b"/iccbased" in raw_lower or b"iccprofile" in raw_lower:
            icc_name = "ICC-Profil gefunden (Name nicht lesbar)"
            icc_ok = False

    cs_parts = []
    if is_cmyk: cs_parts.append("CMYK")
    if is_rgb:  cs_parts.append("RGB")
    if is_mixed: cs_parts.append("CMYK + RGB (gemischt)")
    if not cs_parts:
        cs_parts.append("Nicht erkennbar (evtl. reine Vektordatei)")
        is_cmyk = True  # Vector-only = safe assumption for display

    return {
        "is_cmyk": is_cmyk,
        "is_rgb": is_rgb,
        "is_mixed": is_mixed,
        "icc_name": icc_name,
        "icc_ok": icc_ok,
        "colorspace_summary": " · ".join(cs_parts),
    }


# ─────────────────────────────────────────────
#  CROP MARK DETECTION
# ─────────────────────────────────────────────
def detect_cropmarks(page, media_w, media_h, trim_w, trim_h):
    """
    Beschnittzeichen erkennen — nur wenn Elemente AUSSERHALB der TrimBox liegen.
    Vektorgrafiken innerhalb der TrimBox werden nicht als Beschnittzeichen gewertet.
    """
    # Nur prüfen wenn MediaBox größer als TrimBox ist (Beschnitt vorhanden)
    margin_w = (media_w - trim_w) / 2
    margin_h = (media_h - trim_h) / 2

    if margin_w < 1.0 and margin_h < 1.0:
        # Keine Marge — keine Beschnittzeichen möglich
        return False

    # TrimBox in Punkten berechnen
    mediabox = page.mediabox
    trim_x0 = mediabox.x0 + (margin_w / PT_TO_MM)
    trim_y0 = mediabox.y0 + (margin_h / PT_TO_MM)
    trim_x1 = mediabox.x1 - (margin_w / PT_TO_MM)
    trim_y1 = mediabox.y1 - (margin_h / PT_TO_MM)

    paths = page.get_drawings()
    cropmark_count = 0

    for p in paths:
        r = p.get("rect")
        if r is None:
            continue
        stroke_w = float(p.get("width") or 1.0)  # None-safe
        rect_w_pt = abs(r.x1 - r.x0)
        rect_h_pt = abs(r.y1 - r.y0)
        rect_w_mm = rect_w_pt * PT_TO_MM
        rect_h_mm = rect_h_pt * PT_TO_MM
        is_outside_trim = (
            r.x1 < trim_x0 or r.x0 > trim_x1 or
            r.y1 < trim_y0 or r.y0 > trim_y1
        )
        is_thin = stroke_w < 0.8
        is_short = (rect_w_mm < 25 or rect_h_mm < 25)
        is_line  = (rect_w_mm < 1.0 or rect_h_mm < 1.0)  # fast eindimensional

        if is_outside_trim and is_thin and (is_short or is_line):
            cropmark_count += 1

    # Mindestens 4 Beschnittzeichen-Kandidaten = wahrscheinlich vorhanden
    return cropmark_count >= 4


# ─────────────────────────────────────────────
#  FIX LOGIC
# ─────────────────────────────────────────────
def apply_fixes(doc, raw_bytes, print_w, print_h, scale, fix_cropmarks, fix_bleed, fix_colorspace):
    """
    Fix-Reihenfolge:
    1. Beschnittzeichen entfernen (Clip auf TrimBox)
    2. Beschnittzugabe durch Randspiegelung hinzufügen
    """
    fixes_applied = []
    page = doc[0]

    # ── Schritt 1: Beschnittzeichen entfernen ──
    # Immer ausführen wenn Beschnitt gefixt wird (Zeichen stören sonst)
    trimbox = page.trimbox
    mediabox = page.mediabox
    has_trimbox = trimbox and trimbox != mediabox

    if (fix_cropmarks or fix_bleed) and has_trimbox:
        # Auf TrimBox zuschneiden → Beschnittzeichen außen fallen weg
        page.set_cropbox(trimbox)
        page.set_mediabox(trimbox)
        fixes_applied.append("Beschnittzeichen entfernt")

    # ── Schritt 2: Beschnittzugabe durch Randspiegelung ──
    if fix_bleed:
        expected_bleed_mm = 20.0 / scale
        expected_bleed_pt = expected_bleed_mm / PT_TO_MM

        # Aktuellen Druckbereich holen (nach Crop-Schritt)
        page = doc[0]
        rect = page.rect
        page_w_pt = rect.width
        page_h_pt = rect.height

        # Neues Dokument mit erweiterter Seite
        new_doc = fitz.open()
        new_w = page_w_pt + 2 * expected_bleed_pt
        new_h = page_h_pt + 2 * expected_bleed_pt
        new_page = new_doc.new_page(width=new_w, height=new_h)

        # Originalen Inhalt in die Mitte einbetten
        src_rect = fitz.Rect(0, 0, page_w_pt, page_h_pt)
        dst_rect = fitz.Rect(expected_bleed_pt, expected_bleed_pt,
                             expected_bleed_pt + page_w_pt,
                             expected_bleed_pt + page_h_pt)
        new_page.show_pdf_page(dst_rect, doc, 0)

        # Randspiegelung — vier Seiten
        # Links (gespiegelt horizontal)
        left_src = fitz.Rect(0, 0, expected_bleed_pt, page_h_pt)
        left_dst = fitz.Rect(0, expected_bleed_pt, expected_bleed_pt,
                             expected_bleed_pt + page_h_pt)
        new_page.show_pdf_page(left_dst, doc, 0,
                               clip=fitz.Rect(0, 0, expected_bleed_pt, page_h_pt),
                               rotate=0)

        # Rechts (gespiegelt horizontal)
        right_src_x = page_w_pt - expected_bleed_pt
        right_dst = fitz.Rect(expected_bleed_pt + page_w_pt, expected_bleed_pt,
                              new_w, expected_bleed_pt + page_h_pt)
        new_page.show_pdf_page(right_dst, doc, 0,
                               clip=fitz.Rect(right_src_x, 0, page_w_pt, page_h_pt))

        # Oben (gespiegelt vertikal)
        top_dst = fitz.Rect(expected_bleed_pt, 0,
                            expected_bleed_pt + page_w_pt, expected_bleed_pt)
        new_page.show_pdf_page(top_dst, doc, 0,
                               clip=fitz.Rect(0, 0, page_w_pt, expected_bleed_pt))

        # Unten (gespiegelt vertikal)
        bot_src_y = page_h_pt - expected_bleed_pt
        bot_dst = fitz.Rect(expected_bleed_pt, expected_bleed_pt + page_h_pt,
                            expected_bleed_pt + page_w_pt, new_h)
        new_page.show_pdf_page(bot_dst, doc, 0,
                               clip=fitz.Rect(0, bot_src_y, page_w_pt, page_h_pt))

        # Ecken (Kombination)
        # Oben-links
        new_page.show_pdf_page(fitz.Rect(0, 0, expected_bleed_pt, expected_bleed_pt),
                               doc, 0, clip=fitz.Rect(0, 0, expected_bleed_pt, expected_bleed_pt))
        # Oben-rechts
        new_page.show_pdf_page(fitz.Rect(expected_bleed_pt+page_w_pt, 0, new_w, expected_bleed_pt),
                               doc, 0, clip=fitz.Rect(right_src_x, 0, page_w_pt, expected_bleed_pt))
        # Unten-links
        new_page.show_pdf_page(fitz.Rect(0, expected_bleed_pt+page_h_pt, expected_bleed_pt, new_h),
                               doc, 0, clip=fitz.Rect(0, bot_src_y, expected_bleed_pt, page_h_pt))
        # Unten-rechts
        new_page.show_pdf_page(fitz.Rect(expected_bleed_pt+page_w_pt, expected_bleed_pt+page_h_pt, new_w, new_h),
                               doc, 0, clip=fitz.Rect(right_src_x, bot_src_y, page_w_pt, page_h_pt))

        pdf_bytes = new_doc.tobytes(garbage=4, deflate=True)
        new_doc.close()
        fixes_applied.append(f"Beschnittzugabe {expected_bleed_mm:.1f} mm durch Randspiegelung hinzugefügt")
        if not fixes_applied or fixes_applied == ["Beschnittzeichen entfernt"]:
            pass
        return pdf_bytes, fixes_applied

    pdf_bytes = doc.tobytes(garbage=4, deflate=True)

    if not fixes_applied:
        fixes_applied.append("Keine Korrekturen notwendig")

    return pdf_bytes, fixes_applied


# ─────────────────────────────────────────────
#  USER STORE — In-Memory + KV-Store via kvdb.io
#  Kostenlos, kein Setup, persistent
# ─────────────────────────────────────────────
import json
import os
import urllib.request
import urllib.error

ADMIN_EMAIL    = os.environ.get("ADMIN_EMAIL", "dm@dcp-online.de")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "supersize")
KV_BUCKET      = os.environ.get("KV_BUCKET", "")   # kvdb.io bucket ID
KV_URL         = f"https://kvdb.io/{KV_BUCKET}/pps_users"

# In-Memory für laufende Session
_users: dict = {}
_loaded: bool = False

def load_users() -> dict:
    global _users, _loaded
    if _loaded:
        return _users
    if not KV_BUCKET:
        _loaded = True
        return _users
    try:
        req = urllib.request.Request(KV_URL)
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode()
            if raw:
                _users = json.loads(raw)
    except Exception:
        _users = {}
    _loaded = True
    return _users

def save_users(users: dict):
    global _users, _loaded
    _users = users
    _loaded = True
    if not KV_BUCKET:
        return
    try:
        payload = json.dumps(users).encode()
        req = urllib.request.Request(
            KV_URL,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise HTTPException(500, f"KV Fehler {e.code}: {body}")
    except Exception as e:
        raise HTTPException(500, f"Speichern fehlgeschlagen: {str(e)}")

# ─────────────────────────────────────────────
#  AUTH ENDPOINTS
# ─────────────────────────────────────────────
from pydantic import BaseModel

class UserRequest(BaseModel):
    name: str
    email: str
    password: str

@app.post("/login")
def login(
    email: str = Form(...),
    password: str = Form(...),
):
    email = email.strip().lower()
    password = password.strip()
    if email == ADMIN_EMAIL.lower() and password == ADMIN_PASSWORD:
        return {"success": True, "role": "admin", "name": "Admin"}
    users = load_users()
    if email in users and users[email]["password"] == password:
        return {"success": True, "role": "customer", "name": users[email]["name"]}
    raise HTTPException(401, "E-Mail oder Passwort ungültig.")

@app.get("/login")
def login_get(email: str, password: str):
    email = email.strip().lower()
    password = password.strip()
    if email == ADMIN_EMAIL.lower() and password == ADMIN_PASSWORD:
        return {"success": True, "role": "admin", "name": "Admin"}
    users = load_users()
    if email in users and users[email]["password"] == password:
        return {"success": True, "role": "customer", "name": users[email]["name"]}
    raise HTTPException(401, "E-Mail oder Passwort ungültig.")

@app.get("/admin/users")
def get_users(admin_email: str, admin_password: str):
    if admin_email.lower() != ADMIN_EMAIL.lower() or admin_password != ADMIN_PASSWORD:
        raise HTTPException(403, "Nicht autorisiert.")
    users = load_users()
    return {"users": [{"email": e, "name": d["name"]} for e, d in users.items()]}

@app.post("/admin/users")
def create_user(req: UserRequest, admin_email: str, admin_password: str):
    if admin_email.lower() != ADMIN_EMAIL.lower() or admin_password != ADMIN_PASSWORD:
        raise HTTPException(403, "Nicht autorisiert.")
    users = load_users()
    email = req.email.strip().lower()
    users[email] = {"name": req.name.strip(), "password": req.password.strip()}
    save_users(users)
    return {"success": True, "message": f"Benutzer {email} angelegt."}

@app.delete("/admin/users/{email}")
def delete_user(email: str, admin_email: str, admin_password: str):
    if admin_email.lower() != ADMIN_EMAIL.lower() or admin_password != ADMIN_PASSWORD:
        raise HTTPException(403, "Nicht autorisiert.")
    users = load_users()
    email = email.strip().lower()
    if email not in users:
        raise HTTPException(404, "Benutzer nicht gefunden.")
    del users[email]
    save_users(users)
    return {"success": True}

# ─────────────────────────────────────────────
#  HEALTH CHECK
# ─────────────────────────────────────────────
@app.get("/debug/kv")
def debug_kv():
    bucket = os.environ.get("KV_BUCKET", "")
    if not bucket:
        return {"error": "KV_BUCKET nicht gesetzt"}
    url = f"https://kvdb.io/{bucket}/pps_users"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode()
            return {"status": "ok", "data": raw}
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}", "body": e.read().decode()}
    except Exception as e:
        return {"error": str(e)}

@app.get("/health")
def health():
    return {"status": "ok", "service": "PPS API", "version": "1.3.8"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)

