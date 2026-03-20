"""
PPS – Pre Production Service
Backend API · Version 2.2
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

app = FastAPI(title="PPS API", version="2.1.1")

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
def _check_and_increment_trial(email: str):
    """Checks trial limit for user. Raises 402 if exceeded."""
    users = load_users()
    if email not in users:
        return  # admin or unknown — skip
    user = users[email]
    if user.get("role") != "trial":
        return  # full user — no limit
    used  = int(user.get("trial_analyses", 0))
    limit = int(user.get("trial_limit", TRIAL_LIMIT))
    if used >= limit:
        raise HTTPException(
            402,
            f"Ihr Test-Kontingent ({limit} Analysen) ist aufgebraucht. "
            "Bitte kontaktieren Sie uns fuer einen vollstaendigen Zugang: hello@studiomarx.com"
        )
    user["trial_analyses"] = used + 1
    users[email] = user
    save_users(users)


@app.post("/analyze")
async def analyze(
    file: UploadFile = File(...),
    print_width_mm: float = Form(...),
    print_height_mm: float = Form(...),
    scale: int = Form(10),
    job_name: Optional[str] = Form(""),
    user_email: Optional[str] = Form(""),
):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Nur PDF-Dateien erlaubt.")

    # Trial limit check
    if user_email:
        _check_and_increment_trial(user_email.strip().lower())

    data = await file.read()
    if len(data) > 100 * 1024 * 1024:
        raise HTTPException(413, "Datei zu groß (max. 100 MB).")

    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        raise HTTPException(422, "PDF konnte nicht geöffnet werden.")

    import gc
    result = run_analysis(doc, data, print_width_mm, print_height_mm, scale, job_name, file.filename)
    doc.close()
    del data
    gc.collect()
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
    fix_resolution: bool = Form(False),
    job_name: Optional[str] = Form(""),
):
    import gc
    data = await file.read()
    if len(data) > 100 * 1024 * 1024:
        raise HTTPException(413, "Datei zu groß (max. 100 MB).")

    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        raise HTTPException(422, "PDF konnte nicht geöffnet werden.")

    upscaled_count = 0

    # Schritt 1: Analyse NUR für Report — danach Speicher freigeben
    analysis_result = None
    try:
        analysis_result = run_analysis(doc, data, print_width_mm, print_height_mm,
                                       scale, "", file.filename)
    except Exception:
        pass

    # Schritt 2: Preview-Bild JETZT generieren (solange doc noch offen) — klein halten
    preview_bytes = None
    try:
        prev_page = doc[0]
        prev_pix = prev_page.get_pixmap(matrix=fitz.Matrix(0.3, 0.3), alpha=False)
        import io as _prev_io
        from PIL import Image as _PrevImg
        prev_buf = _prev_io.BytesIO()
        _PrevImg.frombytes("RGB", (prev_pix.width, prev_pix.height),
                           prev_pix.samples).save(prev_buf, format="JPEG", quality=70)
        preview_bytes = prev_buf.getvalue()
        del prev_pix, prev_buf  # sofort freigeben
        gc.collect()
    except Exception:
        preview_bytes = None

    # Schritt 3: Upscaling auf Original-PDF
    if fix_resolution:
        upscaled_bytes, upscaled_count = upscale_images_in_pdf(doc, scale)
        if upscaled_bytes:
            doc.close()
            doc = fitz.open(stream=upscaled_bytes, filetype="pdf")
            del upscaled_bytes
            gc.collect()

    # Schritt 4: Bleed + Cropmarks fixen — data danach freigeben
    fixed_pdf, fixes_applied = apply_fixes(
        doc, data, print_width_mm, print_height_mm, scale,
        fix_cropmarks, fix_bleed, fix_colorspace
    )
    doc.close()
    del data  # Original-Bytes nicht mehr nötig
    gc.collect()

    if upscaled_count > 0:
        fixes_applied.append(f"{upscaled_count} Pixel-Bild(er) hochgerechnet auf 100 DPI")

    # Warnung für nicht-fixbare Bilder
    if analysis_result:
        for check in analysis_result.get("checks", []):
            if check.get("id") == "resolution":
                imgs = check.get("details", {}).get("images", [])
                bad = [i for i in imgs if i.get("dpi_at_1to1", 0) < 25]
                if bad:
                    fixes_applied.append(
                        f"Hinweis: {len(bad)} Bild(er) unter 25 DPI wurden hochgerechnet "
                        f"— Druckqualitaet koennte beeintraechtigt sein"
                    )

    import zipfile
    import io as _zip_io
    from datetime import datetime

    base_name = file.filename.replace(".pdf", "")
    fixed_name = base_name + "_PPS_fixed.pdf"

    # Schritt 5: Report generieren — preview_bytes bereits fertig
    report_bytes = None
    try:
        report_bytes = _generate_report_bytes(
            result_data=analysis_result,
            filename=file.filename,
            job_name=job_name or "",
            print_w=print_width_mm,
            print_h=print_height_mm,
            scale=scale,
            fixes_applied=fixes_applied,
            scale_val=scale,
            preview_bytes=preview_bytes
        )
        del preview_bytes
        gc.collect()
    except Exception as _rep_err:
        import sys, traceback
        print(f"[PPS] report error: {_rep_err}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        report_bytes = None

    if report_bytes:
        # ZIP mit PDF + Report
        zip_buf = _zip_io.BytesIO()
        with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(fixed_name, fixed_pdf)
            zf.writestr(base_name + "_PPS_Report.pdf", report_bytes)
        zip_buf.seek(0)
        return Response(
            content=zip_buf.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{base_name}_PPS.zip"',
                     "X-Fixes-Applied": ", ".join(fixes_applied).encode("ascii","ignore").decode()}
        )
    else:
        return Response(
            content=fixed_pdf,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{fixed_name}"',
                     "X-Fixes-Applied": ", ".join(fixes_applied).encode("ascii","ignore").decode()}
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

    # ── Beschnittzeichen erkennen ──
    has_cropmarks = detect_cropmarks(page, media_w_mm, media_h_mm, trim_w_mm, trim_h_mm)

    # ── Echter Beschnitt berechnen ──
    # Regel: Wenn TrimBox vorhanden, ist die Differenz MediaBox-TrimBox = echter Beschnitt
    # Beschnittzeichen liegen AUSSERHALB der TrimBox → ändern nichts an der Beschnitt-Berechnung
    # Nur wenn KEINE TrimBox: können wir keinen Beschnitt erkennen
    if trimbox:
        bleed_mm = ((media_w_mm - trim_w_mm) / 2 + (media_h_mm - trim_h_mm) / 2) / 2
        # Wenn Beschnittzeichen vorhanden aber kein echter Beschnitt (TrimBox = MediaBox)
        # dann ist bleed_mm = 0, was korrekt ist
    else:
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
        "fixable": img_status in ("warn", "error"),  # error kann teilweise fixbar sein
        "details": {"images": images, "min_dpi": round(min_dpi_doc, 1), "critical_dpi": round(critical_dpi, 1)}
    })

    # 6. File size — nur anzeigen, nicht bewerten (Punkt 5 Feedback)
    checks.append({
        "id": "filesize",
        "label": "Dateigröße",
        "status": "ok",
        "value": f"{file_size_mb:.2f} MB",
        "note": None,
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

    # 8. Color space — neutral anzeigen, Empfehlung aussprechen, nicht als Fehler werten
    spot_colors = color_info.get("spot_colors", [])
    cutcontur   = color_info.get("cutcontur", [])

    # Farbraum-Wert aufbauen
    cs_value = color_info["colorspace_summary"]
    if spot_colors:
        spot_label = ", ".join(spot_colors[:5]) + (" ..." if len(spot_colors) > 5 else "")
        cs_value += f" · Spotfarbe(n): {spot_label}"

    # Hinweistext
    if color_info["is_cmyk"]:
        cs_note = "CMYK-Farbraum erkannt \u2014 optimal f\u00fcr Textildruck."
    elif color_info["is_rgb"]:
        cs_note = "RGB-Farbraum erkannt. Empfehlung: CMYK mit ISO Coated v2 f\u00fcr optimale Druckergebnisse."
    elif color_info["is_mixed"]:
        cs_note = "Gemischte Farbr\u00e4ume (CMYK + RGB). Empfehlung: alle Objekte in CMYK konvertieren."
    else:
        cs_note = "Farbraum nicht eindeutig erkennbar (evtl. reine Vektordatei)."

    if cutcontur:
        cc_label = ", ".join(cutcontur)
        cs_note += f" \u2713 CutContur erkannt: {cc_label}"
    elif spot_colors and not cutcontur:
        cs_note += f" Spotfarbe(n) vorhanden \u2014 keine CutContur erkannt."

    checks.append({
        "id": "colorspace",
        "label": "Farbraum",
        "status": "ok",   # immer neutral — nur informieren
        "value": cs_value,
        "note": cs_note,
        "fixable": False,
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
                    dpi_at_1to1 = dpi_effective / scale
                else:
                    dpi_effective = 0
                    dpi_at_1to1 = 0
                bbox = [round(r.x0, 2), round(r.y0, 2), round(r.x1, 2), round(r.y1, 2)]
            else:
                page_w_mm = page.rect.width * PT_TO_MM
                page_h_mm = page.rect.height * PT_TO_MM
                dpi_effective = min(img_w_px / (page_w_mm / 25.4), img_h_px / (page_h_mm / 25.4))
                dpi_at_1to1 = dpi_effective / scale
                bbox = None

            images.append({
                "xref": xref,
                "width_px": img_w_px,
                "height_px": img_h_px,
                "dpi_in_doc": round(dpi_effective, 1),
                "dpi_at_1to1": round(dpi_at_1to1, 1),
                "colorspace": cs_name,
                "size_kb": round(len(base_image.get("image", b"")) / 1024, 1),
                "bbox": bbox,
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

    # ── Spotcolor / CutContur Erkennung ──
    spot_names = []
    try:
        # PyMuPDF: alle Colorspace-Objekte durchsuchen
        for xref in range(1, doc.xref_length()):
            try:
                if doc.xref_is_stream(xref):
                    continue
                obj_str = doc.xref_object(xref, compressed=False)
                if "/Separation" in obj_str or "/DeviceN" in obj_str:
                    # Spotfarbe gefunden — Namen extrahieren
                    import re
                    # /Separation (Name) ...
                    sep_match = re.search(r'/Separation\s*\(([^)]+)\)', obj_str)
                    if sep_match:
                        name = sep_match.group(1).strip()
                        if name not in spot_names:
                            spot_names.append(name)
                    # /Separation /Name ...
                    sep_match2 = re.search(r'/Separation\s*/([A-Za-z0-9_.-]+)', obj_str)
                    if sep_match2:
                        name = sep_match2.group(1).strip()
                        if name not in spot_names:
                            spot_names.append(name)
                    # /DeviceN [(Name1)(Name2)...]
                    dn_match = re.search(r'/DeviceN\s*\[([^\]]+)\]', obj_str)
                    if dn_match:
                        for n in re.findall(r'\(([^)]+)\)|/([A-Za-z0-9_.-]+)', dn_match.group(1)):
                            name = (n[0] or n[1]).strip()
                            if name and name not in spot_names:
                                spot_names.append(name)
            except Exception:
                pass
    except Exception:
        pass

    # Erkennen ob eine Spotfarbe eine CutContur ist
    cutcontur_names = []
    for name in spot_names:
        nl = name.lower().replace(" ", "").replace("-", "").replace("_", "")
        if any(kw in nl for kw in ["cut", "kontur", "contour", "kontur", "die", "thru",
                                    "cutter", "cutline", "dieline", "stanze", "crease"]):
            cutcontur_names.append(name)

    return {
        "is_cmyk": is_cmyk,
        "is_rgb": is_rgb,
        "is_mixed": is_mixed,
        "icc_name": icc_name,
        "icc_ok": icc_ok,
        "colorspace_summary": " · ".join(cs_parts),
        "spot_colors": spot_names,
        "cutcontur": cutcontur_names,
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
#  IMAGE UPSCALING
# ─────────────────────────────────────────────
def upscale_images_in_pdf(doc, scale, min_dpi_1to1=50.0):
    """
    Hochrechnen via neues Dokument:
    1. Originaldokument als Hintergrund rendern
    2. Bilder die upscaling brauchen einzeln hochrechnen
    3. Hochgerechnete Bilder über den Hintergrund legen
    Gibt (new_pdf_bytes, count) zurück statt modifiziertem doc.
    """
    from PIL import Image
    import io as _io
    import sys

    page = doc[0]
    image_list = page.get_images(full=True)

    # Sammle alle Bilder die upscaling brauchen
    to_upscale = []
    for img_info in image_list:
        xref = img_info[0]
        try:
            base_image = doc.extract_image(xref)
            img_w_px = base_image["width"]
            img_h_px = base_image["height"]
            img_bytes = base_image["image"]
            colorspace = base_image.get("colorspace", 3)

            img_rects = page.get_image_rects(xref)
            if not img_rects:
                continue
            r = img_rects[0]
            placed_w_mm = (r.x1 - r.x0) * PT_TO_MM
            placed_h_mm = (r.y1 - r.y0) * PT_TO_MM
            if placed_w_mm <= 0 or placed_h_mm <= 0:
                continue

            dpi_doc = min(
                img_w_px / (placed_w_mm / 25.4),
                img_h_px / (placed_h_mm / 25.4)
            )
            dpi_1to1 = dpi_doc / scale

            if dpi_1to1 >= min_dpi_1to1:
                continue  # bereits gut genug — kein Upscaling nötig

            # Alle unter 50 DPI werden hochgerechnet (auch unter 25 DPI)
            # Report-Warnung macht auf kritisch schlechte Auflösung aufmerksam
            target_dpi = 100.0
            factor = min(target_dpi / dpi_1to1, 4.0)  # max 4x upscale
            to_upscale.append({
                "xref": xref,
                "rect": r,
                "bytes": img_bytes,
                "w": img_w_px,
                "h": img_h_px,
                "factor": factor,
                "dpi_1to1": dpi_1to1,
                "colorspace": colorspace,
            })
        except Exception as e:
            print(f"[PPS] upscale scan error xref={xref}: {e}", file=sys.stderr)

    if not to_upscale:
        return None, 0

    import pikepdf
    import io as _io2
    import sys

    upscaled_count = 0
    pdf_bytes_orig = doc.tobytes()
    pdf_pk = pikepdf.open(_io2.BytesIO(pdf_bytes_orig))
    page_pk = pdf_pk.pages[0]

    # Hole alle XObjects der Seite
    resources = page_pk.Resources
    xobjects = resources.get("/XObject", {})

    for item in to_upscale:
        try:
            img = Image.open(_io.BytesIO(item["bytes"]))
            original_mode = img.mode

            new_w = int(item["w"] * item["factor"])
            new_h = int(item["h"] * item["factor"])

            # Modus beibehalten für JPEG-Kompatibilität
            if img.mode == 'L':
                # Graustufen → als L hochrechnen, dann als JPEG speichern
                img_up = img.resize((new_w, new_h), Image.LANCZOS)
                # JPEG unterstützt kein L direkt → RGB konvertieren
                img_up = img_up.convert('RGB')
                cs_name = "/DeviceRGB"
            elif img.mode == 'CMYK':
                img_up = img.resize((new_w, new_h), Image.LANCZOS)
                cs_name = "/DeviceCMYK"
            elif img.mode == 'RGB':
                img_up = img.resize((new_w, new_h), Image.LANCZOS)
                cs_name = "/DeviceRGB"
            else:
                img_up = img.convert('RGB').resize((new_w, new_h), Image.LANCZOS)
                cs_name = "/DeviceRGB"

            out_buf = _io.BytesIO()
            img_up.save(out_buf, format="JPEG", quality=95)
            new_jpeg = out_buf.getvalue()

            # Alle XObjects loggen zum Debuggen
            print(f"[PPS] looking for image {item['w']}x{item['h']} in {len(list(xobjects.keys()))} xobjects", file=sys.stderr)
            for name in list(xobjects.keys()):
                try:
                    obj = xobjects[name]
                    obj_w = int(str(obj.get("/Width", "0")))
                    obj_h = int(str(obj.get("/Height", "0")))
                    print(f"[PPS]   xobj '{name}': {obj_w}x{obj_h}", file=sys.stderr)
                    if obj_w == item["w"] and obj_h == item["h"]:
                        cs = obj.get("/ColorSpace", pikepdf.Name("/DeviceRGB"))
                        # Neuen Stream mit make_stream erstellen — korrekte pikepdf Methode
                        # Nutze cs_name wenn Modus konvertiert wurde
                        final_cs = pikepdf.Name(cs_name) if 'cs_name' in dir() else cs
                        new_stream = pdf_pk.make_stream(
                            new_jpeg,
                            Type=pikepdf.Name("/XObject"),
                            Subtype=pikepdf.Name("/Image"),
                            Width=new_w,
                            Height=new_h,
                            ColorSpace=final_cs,
                            BitsPerComponent=8,
                            Filter=pikepdf.Name("/DCTDecode"),
                        )
                        xobjects[name] = pdf_pk.make_indirect(new_stream)
                        upscaled_count += 1
                        new_dpi = item["dpi_1to1"] * item["factor"]
                        print(f"[PPS] REPLACED '{name}': {item['w']}x{item['h']}→{new_w}x{new_h} "
                              f"| {item['dpi_1to1']:.0f}→{new_dpi:.0f} DPI@1:1", file=sys.stderr)
                        break
                except Exception as xe:
                    print(f"[PPS] xobj error {name}: {xe}", file=sys.stderr)
                    continue

        except Exception as e:
            print(f"[PPS] upscale item error: {e}", file=sys.stderr)

    out = _io2.BytesIO()
    pdf_pk.save(out)
    pdf_pk.close()
    return out.getvalue(), upscaled_count


# ─────────────────────────────────────────────
#  FIX LOGIC
# ─────────────────────────────────────────────
def _estimate_trim_area(page, mediabox):
    """
    Bestimme TrimBox aus Beschnittzeichen-Koordinaten.
    Beschnittzeichen sind kurze dünne Linien an den 4 Ecken.
    Die inneren Enden der Linien definieren die TrimBox-Ecken.
    """
    import sys
    paths = page.get_drawings()
    mw = mediabox.width
    mh = mediabox.height

    # Suche kurze dünne Linien (Beschnittzeichen)
    # Typisch: Länge 3-8mm, Stärke < 1pt, Position nahe Ecken
    candidates = []
    for p in paths:
        r = p.get("rect")
        if r is None:
            continue
        stroke_w = float(p.get("width") or 1.0)
        if stroke_w > 1.5:
            continue
        rw = abs(r.x1 - r.x0)
        rh = abs(r.y1 - r.y0)
        # Muss eine Linie sein (sehr dünn in einer Dimension)
        if not (rw < 3 or rh < 3):
            continue
        # Muss kurz sein (3-30mm)
        length_mm = max(rw, rh) * PT_TO_MM
        if length_mm < 2 or length_mm > 35:
            continue
        candidates.append(r)

    if len(candidates) < 4:
        return mediabox

    # Trenne in Bereiche: nahe welcher Ecke liegt die Linie?
    cx = (mediabox.x0 + mediabox.x1) / 2
    cy = (mediabox.y0 + mediabox.y1) / 2

    # Innere Grenzen: die Punkte wo Beschnittzeichen aufhören
    # = wo der Druckbereich beginnt
    inner_x0_candidates = []  # linke Beschnittzeichen: ihr rechtes Ende
    inner_x1_candidates = []  # rechte Beschnittzeichen: ihr linkes Ende
    inner_y0_candidates = []  # obere Beschnittzeichen: ihr unteres Ende
    inner_y1_candidates = []  # untere Beschnittzeichen: ihr oberes Ende

    for r in candidates:
        rect_cx = (r.x0 + r.x1) / 2
        rect_cy = (r.y0 + r.y1) / 2
        rw = abs(r.x1 - r.x0)
        rh = abs(r.y1 - r.y0)

        if rw > rh:  # horizontale Linie
            if rect_cy < cy:  # oberer Bereich
                inner_y0_candidates.append(r.y1)
            else:              # unterer Bereich
                inner_y1_candidates.append(r.y0)
        else:          # vertikale Linie
            if rect_cx < cx:  # linker Bereich
                inner_x0_candidates.append(r.x1)
            else:              # rechter Bereich
                inner_x1_candidates.append(r.x0)

    x0 = max(inner_x0_candidates) if inner_x0_candidates else mediabox.x0
    x1 = min(inner_x1_candidates) if inner_x1_candidates else mediabox.x1
    y0 = max(inner_y0_candidates) if inner_y0_candidates else mediabox.y0
    y1 = min(inner_y1_candidates) if inner_y1_candidates else mediabox.y1

    print(f"[PPS] trim from cropmarks: {(x1-x0)*PT_TO_MM:.1f}x{(y1-y0)*PT_TO_MM:.1f}mm "
          f"(candidates: {len(candidates)})", file=sys.stderr)

    # Validierung
    if x0 >= x1 or y0 >= y1 or (x1-x0) < mw*0.5 or (y1-y0) < mh*0.5:
        print(f"[PPS] trim invalid, using mediabox", file=sys.stderr)
        return mediabox

    return fitz.Rect(x0, y0, x1, y1)


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
    # Vektortreu: Original bleibt als Vektor, nur Randstreifen werden als
    # Pixmap-Elemente angefügt
    if fix_bleed:
        expected_bleed_mm = 20.0 / scale
        expected_bleed_pt = expected_bleed_mm / PT_TO_MM

        page = doc[0]
        trimbox = page.trimbox
        mediabox = page.mediabox

        if trimbox and trimbox != mediabox:
            clip_rect = trimbox
        else:
            clip_rect = _estimate_trim_area(page, mediabox)

        W = clip_rect.width
        H = clip_rect.height
        B = expected_bleed_pt

        # Koordinaten normalisieren: clip_rect kann bei x0>0,y0>0 beginnen
        # Für Randstreifen brauchen wir absolute Koordinaten auf der Originalseite
        cx0 = clip_rect.x0
        cy0 = clip_rect.y0
        cx1 = clip_rect.x1
        cy1 = clip_rect.y1

        from PIL import Image
        import io as _io
        import gc as _gc

        # Randstreifen als Pixmaps rendern — 100 DPI statt 150 spart ~55% RAM
        dpi = 100
        sf = dpi / 72.0

        def render_strip(x0, y0, x1, y1):
            mat = fitz.Matrix(sf, sf)
            pix = page.get_pixmap(matrix=mat, alpha=False,
                                  clip=fitz.Rect(x0, y0, x1, y1))
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            pix = None  # Pixmap sofort freigeben
            return img

        def img_to_bytes(img):
            buf = _io.BytesIO()
            img.save(buf, format="JPEG", quality=88)
            return buf.getvalue()

        # Streifen rendern und sofort transformieren
        strip_l = render_strip(cx0, cy0, cx0 + B, cy1).transpose(Image.FLIP_LEFT_RIGHT)
        strip_r = render_strip(cx1 - B, cy0, cx1, cy1).transpose(Image.FLIP_LEFT_RIGHT)
        strip_t = render_strip(cx0, cy0, cx1, cy0 + B).transpose(Image.FLIP_TOP_BOTTOM)
        strip_b = render_strip(cx0, cy1 - B, cx1, cy1).transpose(Image.FLIP_TOP_BOTTOM)
        corner_tl = render_strip(cx0, cy0, cx0+B, cy0+B).transpose(Image.ROTATE_180)
        corner_tr = render_strip(cx1-B, cy0, cx1, cy0+B).transpose(Image.ROTATE_180)
        corner_bl = render_strip(cx0, cy1-B, cx0+B, cy1).transpose(Image.ROTATE_180)
        corner_br = render_strip(cx1-B, cy1-B, cx1, cy1).transpose(Image.ROTATE_180)

        # Vollbild der Originalseite
        full_mat = fitz.Matrix(sf, sf)
        full_pix = page.get_pixmap(matrix=full_mat, alpha=False,
                                    clip=fitz.Rect(cx0, cy0, cx1, cy1))
        full_img = Image.frombytes("RGB", (full_pix.width, full_pix.height), full_pix.samples)
        full_pix = None
        _gc.collect()

        # Gesamtbild aufbauen
        bx = int(B * sf)
        pw = int(W * sf)
        ph = int(H * sf)
        nw_px = pw + 2 * bx
        nh_px = ph + 2 * bx

        canvas = Image.new("RGB", (nw_px, nh_px), (255, 255, 255))

        # Streifen einfügen und sofort freigeben
        canvas.paste(corner_tl.resize((bx, bx), Image.LANCZOS), (0, 0)); corner_tl = None
        canvas.paste(strip_t.resize((pw, bx), Image.LANCZOS), (bx, 0)); strip_t = None
        canvas.paste(corner_tr.resize((bx, bx), Image.LANCZOS), (bx+pw, 0)); corner_tr = None
        canvas.paste(strip_l.resize((bx, ph), Image.LANCZOS), (0, bx)); strip_l = None
        canvas.paste(full_img, (bx, bx)); full_img = None
        canvas.paste(strip_r.resize((bx, ph), Image.LANCZOS), (bx+pw, bx)); strip_r = None
        canvas.paste(corner_bl.resize((bx, bx), Image.LANCZOS), (0, bx+ph)); corner_bl = None
        canvas.paste(strip_b.resize((pw, bx), Image.LANCZOS), (bx, bx+ph)); strip_b = None
        canvas.paste(corner_br.resize((bx, bx), Image.LANCZOS), (bx+pw, bx+ph)); corner_br = None
        _gc.collect()

        new_doc = fitz.open()
        new_w = W + 2 * B
        new_h = H + 2 * B
        new_page = new_doc.new_page(width=new_w, height=new_h)

        canvas_buf = _io.BytesIO()
        canvas.save(canvas_buf, format="JPEG", quality=92)
        canvas = None  # Canvas freigeben
        _gc.collect()

        new_page.insert_image(
            fitz.Rect(0, 0, new_w, new_h),
            stream=canvas_buf.getvalue()
        )
        canvas_buf = None

        pdf_bytes = new_doc.tobytes(garbage=4, deflate=True)
        new_doc.close()
        _gc.collect()

        fixes_applied.append(f"Beschnittzugabe {expected_bleed_mm:.1f} mm durch Randspiegelung hinzugefügt")
        return pdf_bytes, fixes_applied

    pdf_bytes = doc.tobytes(garbage=4, deflate=True)

    if not fixes_applied:
        fixes_applied.append("Keine Korrekturen notwendig")

    return pdf_bytes, fixes_applied


# ─────────────────────────────────────────────
#  USER STORE — Upstash Redis REST API
#  Kostenlos, persistent, keine Verifizierung nötig
# ─────────────────────────────────────────────
import json
import os
import urllib.request
import urllib.error
import urllib.parse

ADMIN_EMAIL    = os.environ.get("ADMIN_EMAIL", "dm@dcp-online.de")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "supersize")
UPSTASH_URL    = os.environ.get("UPSTASH_URL", "").rstrip("/")
UPSTASH_TOKEN  = os.environ.get("UPSTASH_TOKEN", "")

# SMTP config (Railway env vars)
SMTP_HOST      = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT      = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER      = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD  = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM      = os.environ.get("SMTP_FROM", "noreply@pps.live")
NOTIFY_EMAIL   = os.environ.get("NOTIFY_EMAIL", "hello@studiomarx.com")

TRIAL_LIMIT    = int(os.environ.get("TRIAL_LIMIT", "20"))

_users: dict = {}
_loaded: bool = False

def load_users() -> dict:
    global _users, _loaded
    if _loaded:
        return _users
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        _loaded = True
        return _users
    try:
        url = f"{UPSTASH_URL}/get/pps_users"
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            raw = data.get("result")
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
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        return
    try:
        payload = json.dumps(users)
        url = f"{UPSTASH_URL}/set/pps_users"
        req = urllib.request.Request(
            url,
            data=payload.encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {UPSTASH_TOKEN}",
                "Content-Type": "application/json"
            }
        )
        urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise HTTPException(500, f"Upstash Fehler {e.code}: {body}")
    except Exception as e:
        raise HTTPException(500, f"Speichern fehlgeschlagen: {str(e)}")

# ─────────────────────────────────────────────
#  UPSTASH HELPERS: TRIAL STORE
# ─────────────────────────────────────────────

def _upstash_get(key: str):
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        return None
    try:
        url = f"{UPSTASH_URL}/get/{urllib.parse.quote(key)}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            return data.get("result")
    except Exception:
        return None

def _upstash_set(key: str, value: str, ex: int = None):
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        return
    try:
        url = f"{UPSTASH_URL}/set/{urllib.parse.quote(key)}"
        if ex:
            url += f"?ex={ex}"
        req = urllib.request.Request(
            url,
            data=value.encode(),
            method="POST",
            headers={"Authorization": f"Bearer {UPSTASH_TOKEN}", "Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass

def _upstash_incr(key: str) -> int:
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        return 0
    try:
        url = f"{UPSTASH_URL}/incr/{urllib.parse.quote(key)}"
        req = urllib.request.Request(url, method="POST",
                                     headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            return int(data.get("result", 0))
    except Exception:
        return 0

# ─────────────────────────────────────────────
#  SMTP E-MAIL
# ─────────────────────────────────────────────
import smtplib
import random
import string
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime as _dt

def _send_email(to: str, subject: str, html: str, text: str = ""):
    if not SMTP_USER or not SMTP_PASSWORD:
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"PPS <{SMTP_FROM}>"
        msg["To"]      = to
        if text:
            msg.attach(MIMEText(text, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as s:
            s.ehlo()
            s.starttls()
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.sendmail(SMTP_FROM, to, msg.as_string())
        return True
    except Exception as e:
        print(f"[PPS] SMTP error: {e}")
        return False

def _gen_password(length: int = 10) -> str:
    chars = string.ascii_letters + string.digits
    return "".join(random.choices(chars, k=length))

def _is_business_email(email: str) -> bool:
    free = {"gmail.com","googlemail.com","yahoo.com","yahoo.de","hotmail.com",
            "hotmail.de","outlook.com","outlook.de","web.de","gmx.de","gmx.net",
            "icloud.com","me.com","aol.com","t-online.de","freenet.de","posteo.de"}
    domain = email.strip().lower().split("@")[-1]
    return domain not in free

# ─────────────────────────────────────────────
#  AUTH ENDPOINTS
# ─────────────────────────────────────────────
from pydantic import BaseModel

class UserRequest(BaseModel):
    name: str
    email: str
    password: str

class TrialRequest(BaseModel):
    name: str
    email: str
    company: str

@app.post("/trial/request")
def request_trial(req: TrialRequest):
    email = req.email.strip().lower()
    name  = req.name.strip()
    company = req.company.strip()

    # Business email check
    if not _is_business_email(email):
        raise HTTPException(400, "Bitte eine Business-E-Mail-Adresse verwenden.")

    # Already has account?
    users = load_users()
    if email in users:
        raise HTTPException(409, "Diese E-Mail-Adresse hat bereits Zugang.")

    # Already requested trial?
    existing = _upstash_get(f"trial:{email}")
    if existing:
        raise HTTPException(409, "Fuer diese E-Mail-Adresse wurde bereits ein Test-Zugang angefragt.")

    # Generate credentials
    password = _gen_password(10)
    now_str  = _dt.now().strftime("%d.%m.%Y %H:%M")

    # Save as trial user (with limit tracking)
    users[email] = {
        "name":     name,
        "password": password,
        "role":     "trial",
        "company":  company,
        "trial_analyses": 0,
        "trial_limit":    TRIAL_LIMIT,
        "created":  now_str,
    }
    save_users(users)

    # Mark trial requested in Upstash (90 days TTL)
    _upstash_set(f"trial:{email}", json.dumps({"name": name, "company": company, "created": now_str}), ex=7776000)

    # Send welcome email to user
    welcome_html = f"""
    <div style="font-family:'DM Mono',monospace;max-width:520px;margin:0 auto;padding:2rem;color:#1a1a18">
      <div style="margin-bottom:2rem">
        <strong style="font-size:18px">PPS</strong>
        <span style="font-size:10px;color:#9a9a94;letter-spacing:.1em;margin-left:.75rem;text-transform:uppercase">Pre Production Service</span>
      </div>
      <h2 style="font-family:'Instrument Serif',Georgia,serif;font-size:1.75rem;margin-bottom:1rem">Willkommen, {name}.</h2>
      <p style="font-size:14px;color:#5a5a56;line-height:1.7;margin-bottom:1.5rem">
        Ihr Test-Zugang fuer PPS XPRESS ist bereit. Sie haben <strong>{TRIAL_LIMIT} Analysen</strong> zur freien Verfuegung.
      </p>
      <div style="background:#f5f3ee;border:1px solid #d0cdc4;border-radius:4px;padding:1.5rem;margin-bottom:1.5rem">
        <div style="font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:#9a9a94;margin-bottom:.75rem">Ihre Zugangsdaten</div>
        <div style="margin-bottom:.5rem"><span style="color:#9a9a94">E-Mail:</span> {email}</div>
        <div><span style="color:#9a9a94">Zugangscode:</span> <strong>{password}</strong></div>
      </div>
      <a href="https://studiomarx.com/pps.html" style="display:inline-block;background:#1a1a18;color:white;padding:12px 24px;border-radius:2px;text-decoration:none;font-size:12px;letter-spacing:.1em;text-transform:uppercase">
        PPS XPRESS starten &rarr;
      </a>
      <p style="margin-top:2rem;font-size:11px;color:#9a9a94;line-height:1.6">
        Nach {TRIAL_LIMIT} Analysen koennen Sie einfach Kontakt aufnehmen, um einen vollstaendigen Zugang zu erhalten.<br>
        <a href="mailto:hello@studiomarx.com" style="color:#2d5a3d">hello@studiomarx.com</a>
      </p>
      <div style="margin-top:2rem;padding-top:1.5rem;border-top:1px solid #d0cdc4;font-size:10px;color:#9a9a94;letter-spacing:.06em">
        PPS XPRESS &mdash; Studio M LDA &middot; DSGVO-konform &middot; Keine Datenspeicherung
      </div>
    </div>"""

    _send_email(email, f"Ihr PPS Test-Zugang — {TRIAL_LIMIT} Analysen warten auf Sie", welcome_html)

    # Notify studio
    notify_html = f"""
    <div style="font-family:monospace;padding:1.5rem;color:#1a1a18">
      <strong>Neue Trial-Anfrage</strong><br><br>
      Name: {name}<br>
      Firma: {company}<br>
      E-Mail: {email}<br>
      Passwort: {password}<br>
      Zeitpunkt: {now_str}
    </div>"""
    _send_email(NOTIFY_EMAIL, f"PPS Trial: {name} ({company})", notify_html)

    return {"success": True, "message": f"Test-Zugang fuer {email} angelegt. Bitte E-Mail pruefen."}

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
#  ANALYSE-REPORT ALS PDF
# ─────────────────────────────────────────────
def _generate_report_bytes(result_data, filename, job_name, print_w, print_h, scale,
                            fixes_applied=None, scale_val=1, preview_bytes=None):
    """Generiert Report-PDF via ReportLab im Look der HTML-Analyse."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                     TableStyle, HRFlowable, Image as RLImage,
                                     KeepTogether)
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    import io as _io
    from datetime import datetime

    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    overall = result_data.get("overall_status", "ok") if result_data else "ok"
    sc = {"ok": "#2d6a3f", "warn": "#c96a10", "error": "#c0392b"}
    si = {"ok": "✓", "warn": "!", "error": "✕"}
    status_label = {"ok": "Druckfertig", "warn": "Warnungen vorhanden",
                    "error": "Fehler gefunden"}.get(overall, "–")
    overall_color = colors.HexColor(sc.get(overall, "#1a1a18"))

    def ps(name, **kw):
        defaults = {'fontName': 'Helvetica', 'fontSize': 9}
        defaults.update(kw)
        return ParagraphStyle(name, **defaults)

    buf = _io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            rightMargin=20*mm, leftMargin=20*mm,
                            topMargin=18*mm, bottomMargin=18*mm)
    story = []

    # ── Header ──
    story.append(Paragraph(
        '<font size="8" color="#9a9a94">PRE PRODUCTION SERVICE · DCP</font>',
        ps('brand', spaceAfter=2)))
    story.append(Paragraph("Analyse-Report",
        ps('title', fontSize=22, fontName='Helvetica-Bold',
           textColor=colors.HexColor('#1a1a18'), spaceAfter=2)))
    story.append(Paragraph(now,
        ps('date', fontSize=9, textColor=colors.HexColor('#9a9a94'), spaceAfter=10)))
    story.append(HRFlowable(width="100%", thickness=0.5,
                            color=colors.HexColor('#d0cdc4'), spaceAfter=12))

    # ── Status Banner ──
    banner = Table([[
        Paragraph('<font size="12"><b>' + status_label + '</b></font><br/>'
                  '<font size="8" color="#9a9a94">' + (filename or "–") + '</font>',
                  ps('banner'))
    ]], colWidths=[170*mm])
    banner.setStyle(TableStyle([
        ('LEFTPADDING', (0,0), (-1,-1), 10),
        ('RIGHTPADDING', (0,0), (-1,-1), 10),
        ('TOPPADDING', (0,0), (-1,-1), 8),
        ('BOTTOMPADDING', (0,0), (-1,-1), 8),
        ('BACKGROUND', (0,0), (-1,-1), colors.white),
        ('LINEBEFORE', (0,0), (0,-1), 3, overall_color),
    ]))
    story.append(banner)
    story.append(Spacer(1, 10))

    # ── Preview ──
    if preview_bytes:
        try:
            from PIL import Image as PILImg
            pil_img = PILImg.open(_io.BytesIO(preview_bytes))
            pw, ph = pil_img.size
            max_w = 140*mm
            max_h = 80*mm
            ratio = min(max_w/pw, max_h/ph)
            disp_w = pw * ratio
            disp_h = ph * ratio
            rl_img = RLImage(_io.BytesIO(preview_bytes), width=disp_w, height=disp_h)
            preview_table = Table([[rl_img]], colWidths=[170*mm])
            preview_table.setStyle(TableStyle([
                ('ALIGN', (0,0), (-1,-1), 'CENTER'),
                ('BACKGROUND', (0,0), (-1,-1), colors.white),
                ('TOPPADDING', (0,0), (-1,-1), 8),
                ('BOTTOMPADDING', (0,0), (-1,-1), 8),
            ]))
            story.append(preview_table)
            story.append(Spacer(1, 10))
        except Exception:
            pass

    # ── Meta ──
    meta_data = [
        ["Auftragsname", job_name or "–", "Druckgröße", f"{print_w:.0f} × {print_h:.0f} mm"],
        ["Datei", (filename or "–")[:40], "Maßstab", f"1:{scale_val}"],
    ]
    meta_t = Table(meta_data, colWidths=[28*mm, 57*mm, 28*mm, 57*mm])
    meta_t.setStyle(TableStyle([
        ('FONTNAME', (0,0), (-1,-1), 'Helvetica'),
        ('FONTNAME', (0,0), (0,-1), 'Helvetica-Bold'),
        ('FONTNAME', (2,0), (2,-1), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,-1), 8),
        ('TEXTCOLOR', (0,0), (0,-1), colors.HexColor('#9a9a94')),
        ('TEXTCOLOR', (2,0), (2,-1), colors.HexColor('#9a9a94')),
        ('TEXTCOLOR', (1,0), (1,-1), colors.HexColor('#1a1a18')),
        ('TEXTCOLOR', (3,0), (3,-1), colors.HexColor('#1a1a18')),
        ('TOPPADDING', (0,0), (-1,-1), 5),
        ('BOTTOMPADDING', (0,0), (-1,-1), 5),
        ('ROWBACKGROUNDS', (0,0), (-1,-1), [colors.HexColor('#faf8f5'), colors.white]),
        ('BACKGROUND', (0,0), (-1,-1), colors.white),
        ('LINEBELOW', (0,0), (-1,-2), 0.3, colors.HexColor('#ece9e2')),
    ]))
    story.append(meta_t)
    story.append(Spacer(1, 14))

    # ── Checks ──
    story.append(Paragraph("PRÜFERGEBNISSE",
        ps('ch', fontSize=8, fontName='Helvetica-Bold',
           textColor=colors.HexColor('#9a9a94'), spaceAfter=6,
           letterSpacing=1)))

    checks = result_data.get("checks", []) if result_data else []
    for c in checks:
        s = c.get("status", "ok")
        color = sc.get(s, "#1a1a18")
        icon = si.get(s, "·")
        label = c.get("label", "").upper()
        value = c.get("value", "–")
        note = c.get("note") or ""

        # Details (Bilder)
        det_content = ""
        imgs = c.get("details", {}).get("images", [])
        det_rows = []
        for img in imgs:
            dpi = img.get("dpi_at_1to1", 0)
            dcls = "error" if dpi < 25 else ("warn" if dpi < 50 else "ok")
            dcol = sc.get(dcls, "#1a1a18")
            det_rows.append([
                Paragraph(f'<font color="{dcol}"><b>{dpi:.1f} DPI</b></font>',
                          ps('di', fontSize=8)),
                Paragraph(f'{img.get("width_px",img.get("width",0))}×{img.get("height_px",img.get("height",0))}px · '
                          f'{img.get("colorspace","?")} · {dpi:.1f} DPI@1:1',
                          ps('dn', fontSize=8, textColor=colors.HexColor('#9a9a94'))),
            ])

        body_content = [
            Paragraph(label, ps('cl', fontSize=8, fontName='Helvetica-Bold',
                                textColor=colors.HexColor('#9a9a94'))),
            Paragraph(f'<b>{value}</b>',
                      ps('cv', fontSize=11, textColor=colors.HexColor('#1a1a18'),
                         spaceAfter=2)),
            Paragraph(note, ps('cn', fontSize=8,
                               textColor=colors.HexColor('#9a9a94'), leading=11)),
        ]
        if det_rows:
            dt = Table(det_rows, colWidths=[22*mm, 120*mm])
            dt.setStyle(TableStyle([
                ('FONTSIZE', (0,0), (-1,-1), 8),
                ('TOPPADDING', (0,0), (-1,-1), 2),
                ('BOTTOMPADDING', (0,0), (-1,-1), 2),
                ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#f5f3ee')),
                ('LINEBELOW', (0,0), (-1,-2), 0.3, colors.HexColor('#ebe9e2')),
            ]))
            body_content.append(Spacer(1, 4))
            body_content.append(dt)

        row = Table([
            [Paragraph(f'<font color="{color}"><b>{icon}</b></font>',
                       ps('ic', fontSize=13)),
             body_content]
        ], colWidths=[8*mm, 152*mm])
        row.setStyle(TableStyle([
            ('VALIGN', (0,0), (-1,-1), 'TOP'),
            ('TOPPADDING', (0,0), (-1,-1), 8),
            ('BOTTOMPADDING', (0,0), (-1,-1), 8),
            ('LEFTPADDING', (0,0), (-1,-1), 6),
            ('RIGHTPADDING', (0,0), (-1,-1), 6),
            ('BACKGROUND', (0,0), (-1,-1), colors.white),
            ('LINEBELOW', (0,0), (-1,-1), 0.3, colors.HexColor('#f0ede6')),
        ]))
        story.append(KeepTogether(row))

    # ── Korrekturen ──
    if fixes_applied:
        story.append(Spacer(1, 10))
        has_warning = any('⚠' in f for f in fixes_applied)
        bg_color = colors.HexColor('#fef3cd') if has_warning else colors.HexColor('#e8f4ec')
        label_color = '#c96a10' if has_warning else '#2d6a3f'
        text_color = colors.HexColor('#c96a10') if has_warning else colors.HexColor('#2d6a3f')

        # Normale Korrekturen und Warnungen trennen
        normal = [f for f in fixes_applied if '⚠' not in f]
        warnings = [f for f in fixes_applied if '⚠' in f]

        rows = []
        if normal:
            rows.append([
                Paragraph(f'<b><font color="#2d6a3f">KORREKTUREN</font></b>',
                          ps('fl', fontSize=8)),
                Paragraph(' · '.join(normal),
                          ps('fi', fontSize=8, textColor=colors.HexColor('#2d6a3f'))),
            ])
        for w in warnings:
            rows.append([
                Paragraph('<b><font color="#c0392b">⚠ ACHTUNG</font></b>',
                          ps('wl', fontSize=8)),
                Paragraph(w.replace('⚠ ', ''),
                          ps('wi', fontSize=8, textColor=colors.HexColor('#c0392b'))),
            ])

        fixes_t = Table(rows, colWidths=[30*mm, 140*mm])
        fixes_t.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#e8f4ec')),
            ('BACKGROUND', (0, len(normal)), (-1,-1), colors.HexColor('#fdecea')),
            ('TOPPADDING', (0,0), (-1,-1), 7),
            ('BOTTOMPADDING', (0,0), (-1,-1), 7),
            ('LEFTPADDING', (0,0), (-1,-1), 10),
            ('LINEBELOW', (0,0), (-1,-2), 0.3, colors.HexColor('#d0cdc4')),
        ]))
        story.append(fixes_t)

    # ── Footer ──
    story.append(Spacer(1, 16))
    story.append(HRFlowable(width="100%", thickness=0.5,
                            color=colors.HexColor('#d0cdc4')))
    story.append(Paragraph(
        f"PPS Version //A_1.9 · Pre Production Service · DCP · {now}",
        ps('ft', fontSize=7, textColor=colors.HexColor('#9a9a94'),
           alignment=TA_CENTER, spaceBefore=6)))

    doc.build(story)
    return buf.getvalue()


@app.post("/report")
async def generate_report(
    file: UploadFile = File(...),
    print_width_mm: float = Form(...),
    print_height_mm: float = Form(...),
    scale: int = Form(10),
    job_name: Optional[str] = Form(""),
):
    """Generiert einen Analyse-Report als PDF."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
    from reportlab.lib.enums import TA_LEFT, TA_CENTER
    import io as _io
    from datetime import datetime

    data = await file.read()
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        raise HTTPException(422, "PDF konnte nicht geöffnet werden.")

    result = run_analysis(doc, data, print_width_mm, print_height_mm, scale, job_name, file.filename)
    doc.close()

    # PDF Report erstellen
    buf = _io.BytesIO()
    pdf = SimpleDocTemplate(buf, pagesize=A4,
                            rightMargin=20*mm, leftMargin=20*mm,
                            topMargin=20*mm, bottomMargin=20*mm)

    styles = getSampleStyleSheet()
    story = []

    # Header
    header_style = ParagraphStyle('header', fontSize=20, fontName='Helvetica-Bold',
                                   spaceAfter=4, textColor=colors.HexColor('#1a1a18'))
    sub_style = ParagraphStyle('sub', fontSize=9, fontName='Helvetica',
                                textColor=colors.HexColor('#9a9a94'), spaceAfter=16)
    label_style = ParagraphStyle('label', fontSize=8, fontName='Helvetica-Bold',
                                  textColor=colors.HexColor('#5a5a56'), spaceBefore=12, spaceAfter=2)
    value_style = ParagraphStyle('value', fontSize=11, fontName='Helvetica',
                                  textColor=colors.HexColor('#1a1a18'), spaceAfter=2)
    note_style = ParagraphStyle('note', fontSize=8, fontName='Helvetica',
                                 textColor=colors.HexColor('#9a9a94'), spaceAfter=8)

    story.append(Paragraph("PPS – Pre Production Service", header_style))
    story.append(Paragraph("Analyse-Report", sub_style))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor('#d0cdc4')))
    story.append(Spacer(1, 12))

    # Meta
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    meta = [
        ["Auftragsname", result.get("job_name") or result.get("filename", "–")],
        ["Datei", result.get("filename", "–")],
        ["Druckgröße", f"{print_width_mm:.0f} × {print_height_mm:.0f} mm"],
        ["Maßstab", f"1:{scale}"],
        ["Datum", now],
        ["Gesamtstatus", {"ok": "✓ Druckfertig", "warn": "⚠ Warnungen", "error": "✕ Fehler gefunden"}
                         .get(result.get("overall_status",""), "–")],
    ]
    t = Table(meta, colWidths=[50*mm, 120*mm])
    t.setStyle(TableStyle([
        ('FONTNAME', (0,0), (0,-1), 'Helvetica-Bold'),
        ('FONTNAME', (1,0), (1,-1), 'Helvetica'),
        ('FONTSIZE', (0,0), (-1,-1), 9),
        ('TEXTCOLOR', (0,0), (0,-1), colors.HexColor('#5a5a56')),
        ('TEXTCOLOR', (1,0), (1,-1), colors.HexColor('#1a1a18')),
        ('TOPPADDING', (0,0), (-1,-1), 4),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
        ('ROWBACKGROUNDS', (0,0), (-1,-1), [colors.HexColor('#f5f3ee'), colors.white]),
    ]))
    story.append(t)
    story.append(Spacer(1, 16))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor('#d0cdc4')))
    story.append(Spacer(1, 8))

    # Checks
    story.append(Paragraph("PRÜFERGEBNISSE", ParagraphStyle('h2', fontSize=9,
                fontName='Helvetica-Bold', textColor=colors.HexColor('#9a9a94'),
                spaceBefore=8, spaceAfter=8, letterSpacing=1)))

    status_colors = {"ok": '#2d6a3f', "warn": '#c96a10', "error": '#c0392b'}
    status_icons  = {"ok": "✓", "warn": "!", "error": "✕"}

    for check in result.get("checks", []):
        s = check.get("status", "ok")
        color = colors.HexColor(status_colors.get(s, '#1a1a18'))
        icon = status_icons.get(s, "·")

        row_data = [[
            Paragraph(f'<font color="{status_colors.get(s, "#1a1a18")}">{icon}</font>',
                      ParagraphStyle('icon', fontSize=12, fontName='Helvetica-Bold')),
            Paragraph(f'<b>{check.get("label","").upper()}</b><br/>'
                      f'{check.get("value","–")}',
                      ParagraphStyle('cv', fontSize=9, fontName='Helvetica',
                                     textColor=colors.HexColor('#1a1a18'), leading=14)),
            Paragraph(check.get("note",""),
                      ParagraphStyle('cn', fontSize=8, fontName='Helvetica',
                                     textColor=colors.HexColor('#9a9a94'), leading=11)),
        ]]
        ct = Table(row_data, colWidths=[8*mm, 60*mm, 100*mm])
        ct.setStyle(TableStyle([
            ('VALIGN', (0,0), (-1,-1), 'TOP'),
            ('TOPPADDING', (0,0), (-1,-1), 6),
            ('BOTTOMPADDING', (0,0), (-1,-1), 6),
            ('LINEBELOW', (0,0), (-1,-1), 0.3, colors.HexColor('#eceae3')),
        ]))
        story.append(ct)

    story.append(Spacer(1, 16))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor('#d0cdc4')))
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        f"PPS Version //A_1.7 · Pre Production Service · DCP · {now}",
        ParagraphStyle('footer', fontSize=7, fontName='Helvetica',
                        textColor=colors.HexColor('#9a9a94'), alignment=TA_CENTER)
    ))

    pdf.build(story)
    report_bytes = buf.getvalue()

    filename = file.filename.replace(".pdf", "") + "_PPS_Report.pdf"
    return Response(
        content=report_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


# ─────────────────────────────────────────────
#  HEALTH CHECK
# ─────────────────────────────────────────────
@app.get("/debug/store")
def debug_store():
    upstash_url = os.environ.get("UPSTASH_URL", "")
    upstash_token = os.environ.get("UPSTASH_TOKEN", "")
    if not upstash_url or not upstash_token:
        return {"error": "UPSTASH_URL oder UPSTASH_TOKEN nicht gesetzt"}
    try:
        url = f"{upstash_url.rstrip('/')}/get/pps_users"
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {upstash_token}"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            return {"status": "ok", "result": data.get("result")}
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}", "body": e.read().decode()}
    except Exception as e:
        return {"error": str(e)}

@app.get("/health")
def health():
    return {"status": "ok", "service": "PPS API", "version": "1.9.6"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)

