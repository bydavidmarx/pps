"""
PPS – Pre Production Service
Backend API · Version 2.4.1
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
import sys
import gc
import json
import os
import re
from typing import Optional

app = FastAPI(title="PPS API", version="2.3.2", docs_url=None)

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
    import asyncio, gc
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Nur PDF-Dateien erlaubt.")

    # Trial limit check
    if user_email:
        _check_and_increment_trial(user_email.strip().lower())

    data = await file.read()
    if len(data) > 200 * 1024 * 1024:
        raise HTTPException(413, "Datei zu groß (max. 200 MB).")

    if user_email:
        _track_usage(user_email.strip().lower(), len(data))

    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        raise HTTPException(422, "PDF konnte nicht geöffnet werden.")

    try:
        loop = asyncio.get_event_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: run_analysis(
                doc, data, print_width_mm, print_height_mm, scale, job_name, file.filename)),
            timeout=120.0
        )
    except asyncio.TimeoutError:
        doc.close()
        _notify_error(f"Analyse-Timeout: {file.filename} ({len(data)//1024//1024}MB)")
        raise HTTPException(504, "Analyse-Timeout (>120s). Bitte kleinere Datei versuchen.")
    except Exception as e:
        doc.close()
        _notify_error(f"Analyse-Fehler: {file.filename} — {e}")
        raise HTTPException(500, f"Analyse fehlgeschlagen: {str(e)[:200]}")
    finally:
        try: doc.close()
        except: pass
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
    user_email: Optional[str] = Form(""),
):
    import gc, asyncio
    data = await file.read()
    if len(data) > 200 * 1024 * 1024:
        raise HTTPException(413, "Datei zu groß (max. 200 MB).")

    if user_email:
        _track_usage(user_email.strip().lower(), len(data))

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

    # Schritt 3: Upscaling auf Original-PDF (mit Timeout)
    try:
        if fix_resolution:
            loop = asyncio.get_event_loop()
            upscale_result = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: upscale_images_in_pdf(doc, scale)),
                timeout=180.0
            )
            upscaled_bytes, upscaled_count = upscale_result
            if upscaled_bytes:
                doc.close()
                doc = fitz.open(stream=upscaled_bytes, filetype="pdf")
                del upscaled_bytes
                gc.collect()
    except asyncio.TimeoutError:
        _notify_error(f"Upscaling-Timeout: {file.filename}")
        # Weiter ohne Upscaling — besser als gar nichts
    except Exception as _up_err:
        print(f"[PPS] Upscaling error: {_up_err}", file=sys.stderr)
        # Weiter ohne Upscaling

    # Schritt 4: Bleed + Cropmarks fixen (mit Timeout)
    try:
        loop = asyncio.get_event_loop()
        fix_result = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: apply_fixes(
                doc, data, print_width_mm, print_height_mm, scale,
                fix_cropmarks, fix_bleed, fix_colorspace)),
            timeout=180.0
        )
        fixed_pdf, fixes_applied = fix_result
    except asyncio.TimeoutError:
        doc.close()
        del data
        _notify_error(f"Fix-Timeout: {file.filename}")
        raise HTTPException(504, "Fix-Timeout (>180s). Datei ist moeglicherweise zu komplex.")
    except Exception as e:
        import traceback as _tb
        _tb.print_exc(file=sys.stderr)
        print(f"[PPS] Fix-Fehler: {type(e).__name__}: {e}", file=sys.stderr)
        try: doc.close()
        except: pass
        try: del data
        except: pass
        _notify_error(f"Fix-Fehler: {file.filename} — {type(e).__name__}: {e}")
        raise HTTPException(500, f"Fix fehlgeschlagen: {type(e).__name__}: {str(e)[:300]}")
    doc.close()
    del data  # Original-Bytes nicht mehr nötig
    gc.collect()

    if upscaled_count > 0:
        fixes_applied.append(f"{upscaled_count} Pixel-Bild(er) hochgerechnet auf ~52 PPI")

    # Warnung für nicht-fixbare Bilder
    if analysis_result:
        for check in analysis_result.get("checks", []):
            if check.get("id") == "resolution":
                imgs = check.get("details", {}).get("images", [])
                bad = [i for i in imgs if i.get("dpi_at_1to1", 0) < 25]
                if bad:
                    fixes_applied.append(
                        f"Hinweis: {len(bad)} Bild(er) unter 25 PPI wurden hochgerechnet "
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
        if report_bytes:
            print(f"[PPS] report generated: {len(report_bytes)} bytes", file=sys.stderr)
        del preview_bytes
        gc.collect()
    except Exception as _rep_err:
        import traceback
        print(f"[PPS] report error: {type(_rep_err).__name__}: {_rep_err}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        report_bytes = None

    if report_bytes:
        # ZIP mit PDF + Report
        zip_buf = _zip_io.BytesIO()
        with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(fixed_name, fixed_pdf)
            zf.writestr(base_name + "_PPS_Report.pdf", report_bytes)
        zip_buf.seek(0)
        # HTTP-Header dürfen nur Latin-1 — Dateinamen mit Umlauten sanitieren
        def _safe_filename(name):
            return name.encode("ascii", "replace").decode("ascii").replace("?", "_")
        def _safe_header(text):
            return text.encode("latin-1", "replace").decode("latin-1")

        return Response(
            content=zip_buf.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{_safe_filename(base_name)}_PPS.zip"',
                     "X-Fixes-Applied": _safe_header(", ".join(fixes_applied))}
        )
    else:
        return Response(
            content=fixed_pdf,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{_safe_filename(fixed_name)}"',
                     "X-Fixes-Applied": _safe_header(", ".join(fixes_applied))}
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
    # TrimBox zuverlässig ermitteln: sowohl PyMuPDF-Vergleich als auch Raw-Dict
    _tb = page.trimbox
    _mb = page.mediabox
    # Prüfe auch im rohen PDF-Objekt ob /TrimBox gesetzt ist
    try:
        _raw = doc.xref_object(page.xref)
        _has_trimbox_in_dict = '/TrimBox' in _raw
    except Exception:
        _has_trimbox_in_dict = False
    if _tb != _mb or (_has_trimbox_in_dict and abs(_tb.width - _mb.width) > 0.5):
        trimbox = _tb
    else:
        trimbox = None

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
    bleed_in_page = False
    if trimbox:
        bleed_mm = ((media_w_mm - trim_w_mm) / 2 + (media_h_mm - trim_h_mm) / 2) / 2
    else:
        excess_w = trim_w_mm - expected_w_mm
        excess_h = trim_h_mm - expected_h_mm
        tol = expected_bleed_mm * 0.50  # 50% Toleranz - fängt auch leicht abweichende Werte
        if (excess_w > expected_bleed_mm * 1.2 and
            abs((excess_w/2) - expected_bleed_mm) < tol and
            abs((excess_h/2) - expected_bleed_mm) < tol):
            bleed_mm = (excess_w/2 + excess_h/2) / 2
            bleed_in_page = True
        elif excess_w > expected_bleed_mm * 1.5:
            bleed_mm = (excess_w/2 + excess_h/2) / 2
            bleed_in_page = True
        else:
            bleed_mm = 0.0

    # ── Ratio check ──
    if bleed_in_page:
        net_w_mm = trim_w_mm - 2 * bleed_mm
        net_h_mm = trim_h_mm - 2 * bleed_mm
    else:
        net_w_mm = trim_w_mm
        net_h_mm = trim_h_mm

    ratio_diff_w = abs(net_w_mm - expected_w_mm)
    ratio_diff_h = abs(net_h_mm - expected_h_mm)
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
        "status": "ok" if (ratio_ok or bleed_in_page) else "error",
        "value": f"PDF (Netto): {trim_w_mm:.1f} × {trim_h_mm:.1f} mm · Erwartet: {expected_w_mm:.1f} × {expected_h_mm:.1f} mm",
        "note": f"Verhältnis stimmt überein (1:{scale})." if ratio_ok
                else (f"Beschnitt ist in der Seitengröße eingerechnet — das ist bekannt. Netto-Format stimmt mit dem Druckmaß überein." if bleed_in_page and ratio_diff_w < 1 and ratio_diff_h < 1
                      else f"Abweichung: Breite {ratio_diff_w:+.1f} mm, Höhe {ratio_diff_h:+.1f} mm. Bitte Datei prüfen."),
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
        return ("warn", f"{len(images)} Bild(er) — PPI nicht messbar", "Auflösung konnte nicht bestimmt werden.")

    # dpi_at_1to1 ist die echte Druckauflösung (dpi_in_doc / scale)
    min_at_1to1 = min(i["dpi_at_1to1"] for i in valid)
    max_at_1to1 = max(i["dpi_at_1to1"] for i in valid)
    min_in_doc  = min(i["dpi_in_doc"]  for i in valid)
    max_in_doc  = max(i["dpi_in_doc"]  for i in valid)

    min_print = 50.0   # Minimum PPI bei 1:1
    crit_print = 25.0  # Kritisch — zu niedrig für Upscaling

    below_min  = [i for i in valid if i["dpi_at_1to1"] < min_print]
    below_crit = [i for i in valid if i["dpi_at_1to1"] < crit_print]

    value = (f"{len(images)} Bild(er) · "
             f"{min_in_doc:.0f}–{max_in_doc:.0f} PPI im Dokument "
             f"(= {min_at_1to1:.0f}–{max_at_1to1:.0f} PPI bei 1:1)")

    if below_crit:
        return ("error", value,
                f"{len(below_crit)} Bild(er) unter {crit_print:.0f} PPI (1:1). "
                f"Zu niedrig für Upscaling — bitte Originaldatei in höherer Auflösung liefern.")
    elif below_min:
        return ("warn", value,
                f"{len(below_min)} Bild(er) unter {min_print:.0f} PPI (1:1). "
                f"Upscaling (2×) über 'Fix It' möglich.")
    else:
        return ("ok", value,
                f"Alle Bilder erreichen mindestens {min_print:.0f} PPI bei 1:1. Auflösung ausreichend.")


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

            # Alle unter 50 PPI werden hochgerechnet (auch unter 25 PPI)
            # Report-Warnung macht auf kritisch schlechte Auflösung aufmerksam
            # Ziel: gerade über die 50 PPI-Schwelle (nicht pauschal 100 PPI)
            # Beispiel: Bild bei 40 PPI → Faktor 50/40 = 1.25x, nicht 2.5x
            target_dpi = min_dpi_1to1 * 1.05   # 5% über der Schwelle = sicher drüber
            factor = target_dpi / dpi_1to1       # z.B. 52.5/40 = 1.31x
            factor = min(factor, 2.0)            # max 2x als Sicherheitsgrenze
            factor = max(factor, 1.0)            # nie verkleinern

            print(f"[PPS] upscale {dpi_1to1:.0f}→{dpi_1to1*factor:.0f} PPI@1:1 "
                  f"(factor={factor:.2f}, {img_w_px}x{img_h_px}→"
                  f"{int(img_w_px*factor)}x{int(img_h_px*factor)}px)",
                  file=__import__('sys').stderr)

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

    # ── Kachel-Erkennung: nebeneinanderliegende Bilder NICHT einzeln skalieren ──
    # PDFs speichern große Bilder manchmal als Gitter von Kacheln (je ein xref).
    # Einzelskalierung zerstört die Ausrichtung → falsch zusammengesetztes Bild.
    def _is_adjacent(r1, r2, tol=3.0):
        h_adj = abs(r1.x1-r2.x0) < tol or abs(r2.x1-r1.x0) < tol
        y_ov  = r1.y0 < r2.y1 and r2.y0 < r1.y1
        v_adj = abs(r1.y1-r2.y0) < tol or abs(r2.y1-r1.y0) < tol
        x_ov  = r1.x0 < r2.x1 and r2.x0 < r1.x1
        return (h_adj and y_ov) or (v_adj and x_ov)

    all_rects = [(item["xref"], item["rect"]) for item in to_upscale]
    tiled_xrefs = set()
    for i, (xi, ri) in enumerate(all_rects):
        for j, (xj, rj) in enumerate(all_rects):
            if i != j and _is_adjacent(ri, rj):
                tiled_xrefs.add(xi)
                tiled_xrefs.add(xj)

    if tiled_xrefs:
        skipped = [t for t in to_upscale if t["xref"] in tiled_xrefs]
        to_upscale = [t for t in to_upscale if t["xref"] not in tiled_xrefs]
        print(f"[PPS] Kachel-Erkennung: {len(skipped)} Kachel-Bild(er) vom Upscaling ausgeschlossen",
              file=sys.stderr)

    if not to_upscale:
        return None, 0

    try:
        import pikepdf
    except ImportError:
        print("[PPS] pikepdf not installed — upscaling not available", file=sys.stderr)
        return None, 0
    import io as _io2

    upscaled_count = 0
    pdf_bytes_orig = doc.tobytes()
    pdf_pk = pikepdf.open(_io2.BytesIO(pdf_bytes_orig))
    page_pk = pdf_pk.pages[0]

    # Alle XObjects der Seite mit ihrer Objektnummer indexieren.
    # Wir matchen nach xref-Nummer (= pikepdf-Objektnummer), NICHT nach Dimensionen —
    # bei Kacheln haben viele Tiles exakt gleiche Width/Height, Dimensions-Matching
    # würde immer nur den ersten Treffer ersetzen → falsche Positionen.
    resources = page_pk.Resources
    xobjects = resources.get("/XObject", {})
    xref_to_xobj_name = {}
    for name in list(xobjects.keys()):
        try:
            obj_num = xobjects[name].objgen[0]  # pikepdf obj-Nummer = fitz xref
            xref_to_xobj_name[obj_num] = name
        except Exception:
            pass

    for item in to_upscale:
        try:
            img = Image.open(_io.BytesIO(item["bytes"]))

            new_w = int(item["w"] * item["factor"])
            new_h = int(item["h"] * item["factor"])

            if img.mode == 'L':
                img_up = img.resize((new_w, new_h), Image.LANCZOS).convert('RGB')
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

            # Exaktes Matching über xref-Nummer → immer das richtige XObject
            xobj_name = xref_to_xobj_name.get(item["xref"])
            if not xobj_name:
                print(f"[PPS] xref {item['xref']} nicht in xobjects gefunden", file=sys.stderr)
                continue

            stream_kwargs = dict(
                Type=pikepdf.Name("/XObject"),
                Subtype=pikepdf.Name("/Image"),
                Width=new_w,
                Height=new_h,
                ColorSpace=pikepdf.Name(cs_name),
                BitsPerComponent=8,
                Filter=pikepdf.Name("/DCTDecode"),
            )
            # CMYK: /Decode [1 0 1 0 1 0 1 0] für Adobe-Konvention
            if img.mode == 'CMYK':
                stream_kwargs["Decode"] = pikepdf.Array([1,0,1,0,1,0,1,0])

            new_stream = pdf_pk.make_stream(new_jpeg, **stream_kwargs)
            xobjects[xobj_name] = pdf_pk.make_indirect(new_stream)
            upscaled_count += 1
            print(f"[PPS] REPLACED xref={item['xref']} '{xobj_name}': "
                  f"{item['w']}x{item['h']}→{new_w}x{new_h} "
                  f"| {item['dpi_1to1']:.0f}→{item['dpi_1to1']*item['factor']:.0f} PPI@1:1",
                  file=sys.stderr)

        except Exception as e:
            print(f"[PPS] upscale item error xref={item.get('xref')}: {e}", file=sys.stderr)

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

        # Bleed-inklusive Erkennung
        media_w_pt = mediabox.x1 - mediabox.x0
        media_h_pt = mediabox.y1 - mediabox.y0
        expected_w_pt = (print_w / scale) / PT_TO_MM
        expected_h_pt = (print_h / scale) / PT_TO_MM
        excess_w_pt = media_w_pt - expected_w_pt
        excess_h_pt = media_h_pt - expected_h_pt
        tol_pt = expected_bleed_pt * 0.35

        bleed_in_page = (
            not has_trimbox and
            excess_w_pt > expected_bleed_pt * 1.2 and
            abs(excess_w_pt/2 - expected_bleed_pt) < tol_pt and
            abs(excess_h_pt/2 - expected_bleed_pt) < tol_pt
        )
        # Kein loses Fallback — nur bei präziser Übereinstimmung des excess mit erwartetem Bleed

        if bleed_in_page:
            actual_bleed_w = excess_w_pt / 2
            actual_bleed_h = excess_h_pt / 2
            trim_rect = fitz.Rect(
                mediabox.x0 + actual_bleed_w, mediabox.y0 + actual_bleed_h,
                mediabox.x1 - actual_bleed_w, mediabox.y1 - actual_bleed_h
            )
            page.set_trimbox(trim_rect)
            fixes_applied.append(
                f"TrimBox gesetzt: Netto {round((trim_rect.x1-trim_rect.x0)*PT_TO_MM,1)} x "
                f"{round((trim_rect.y1-trim_rect.y0)*PT_TO_MM,1)} mm"
            )
            pdf_bytes = doc.tobytes(garbage=4, deflate=True)
            return pdf_bytes, fixes_applied

        if trimbox and trimbox != mediabox:
            # TrimBox vorhanden → sauber definierter Druckbereich
            clip_rect = trimbox
        else:
            # Keine TrimBox → nur wenn tatsächlich Schnittmarken erkannt wurden
            # _estimate_trim_area aufrufen, sonst direkt Mediabox verwenden.
            # WICHTIG: Nie _estimate_trim_area blind aufrufen — es kann
            # fälschlicherweise dünne Vektorlinien im Bildinhalt als
            # Schnittmarken interpretieren und eine zu kleine clip_rect liefern.
            actual_cropmarks = detect_cropmarks(page, media_w_pt * PT_TO_MM,
                                                media_h_pt * PT_TO_MM,
                                                media_w_pt * PT_TO_MM,
                                                media_h_pt * PT_TO_MM)
            if actual_cropmarks:
                clip_rect = _estimate_trim_area(page, mediabox)
                # Sicherheitsprüfung: clip_rect darf nicht zu klein sein
                if (clip_rect.width < mediabox.width * 0.7 or
                        clip_rect.height < mediabox.height * 0.7):
                    print(f"[PPS] _estimate_trim_area result too small "
                          f"({clip_rect.width*PT_TO_MM:.1f}mm) — using mediabox",
                          file=__import__('sys').stderr)
                    clip_rect = mediabox
            else:
                # Keine Schnittmarken → Mediabox ist der Druckbereich
                clip_rect = mediabox

        # ── WICHTIG: Größe des Ausgabe-PDFs immer aus den SOLL-Maßen berechnen ──
        # clip_rect wird NUR für das Rendering (welcher Bereich wird gerendert) verwendet.
        # new_w/new_h und TrimBox basieren immer auf den bekannten Druckmaßen.
        # Das verhindert falsche Seitengrößen durch PDF-Koordinaten-Quirks.
        W = expected_w_pt   # = print_w/scale in Punkten — IMMER korrekt
        H = expected_h_pt   # = print_h/scale in Punkten — IMMER korrekt
        B = expected_bleed_pt

        # Render-Koordinaten: welcher Bereich wird abgetastet?
        # clip_rect bestimmt die Quelle, W/H bestimmen das Ziel.
        cx0 = clip_rect.x0
        cy0 = clip_rect.y0
        cx1 = clip_rect.x1
        cy1 = clip_rect.y1

        # Debug-Ausgabe: Abweichung zwischen clip_rect und Soll-Maß
        if abs(clip_rect.width - expected_w_pt) > 10:  # > ~3.5mm Abweichung
            import sys
            print(f"[PPS] clip_rect.width={clip_rect.width*25.4/72:.1f}mm ≠ "
                  f"expected={expected_w_pt*25.4/72:.1f}mm — using expected for page size",
                  file=sys.stderr)

        from PIL import Image
        import io as _io
        import gc as _gc

        # Renderauflösung für Randspiegelung: max 150 DPI — Bleed-Streifen
        # brauchen keine hohe Auflösung. Bei 500 DPI (scale=10) würde das
        # Canvas allein 840 MB RAM brauchen → OOM/502 auf Railway.
        # 150 DPI ergibt ~30 MB — optisch identisch für Randspiegelung.
        dpi = max(min(int(50 * scale), 150), 72)
        print(f"[PPS] bleed render: {dpi} DPI (scale={scale}, RAM-safe cap 150)", file=__import__('sys').stderr)
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
        GRAB_MM = 5.0 / scale
        GRAB_PT = min(GRAB_MM / PT_TO_MM, W*0.15, H*0.15, B)
        GRAB_PT = max(GRAB_PT, 2.0)
        strip_l = render_strip(cx0, cy0, cx0+GRAB_PT, cy1).transpose(Image.FLIP_LEFT_RIGHT)
        strip_r = render_strip(cx1-GRAB_PT, cy0, cx1, cy1).transpose(Image.FLIP_LEFT_RIGHT)
        strip_t = render_strip(cx0, cy0, cx1, cy0+GRAB_PT).transpose(Image.FLIP_TOP_BOTTOM)
        strip_b = render_strip(cx0, cy1-GRAB_PT, cx1, cy1).transpose(Image.FLIP_TOP_BOTTOM)
        corner_tl = render_strip(cx0, cy0, cx0+GRAB_PT, cy0+GRAB_PT).transpose(Image.ROTATE_180)
        corner_tr = render_strip(cx1-GRAB_PT, cy0, cx1, cy0+GRAB_PT).transpose(Image.ROTATE_180)
        corner_bl = render_strip(cx0, cy1-GRAB_PT, cx0+GRAB_PT, cy1).transpose(Image.ROTATE_180)
        corner_br = render_strip(cx1-GRAB_PT, cy1-GRAB_PT, cx1, cy1).transpose(Image.ROTATE_180)

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

        # ── TrimBox setzen — IMMER auf Basis der Soll-Maße ──
        trimbox_w = new_w - 2 * B
        if abs(trimbox_w - expected_w_pt) > expected_w_pt * 0.02:
            import sys as _sys
            print(f"[PPS] TrimBox-Korrektur: {trimbox_w*PT_TO_MM:.1f}mm → {expected_w_pt*PT_TO_MM:.1f}mm",
                  file=_sys.stderr)
            trim_rect = fitz.Rect(B, B, expected_w_pt + B, expected_h_pt + B)
        else:
            trim_rect = fitz.Rect(B, B, new_w - B, new_h - B)
        new_page.set_trimbox(trim_rect)
        print(f"[PPS] TrimBox: {trim_rect} | Beschnitt: {B:.1f}pt = {B*PT_TO_MM:.1f}mm",
              file=__import__('sys').stderr)

        pdf_bytes = new_doc.tobytes(garbage=4, deflate=True)
        new_doc.close()
        _gc.collect()

        fixes_applied.append(f"Beschnittzugabe {expected_bleed_mm:.1f} mm durch Randspiegelung hinzugefügt")
        trim_w_out = round((trim_rect.x1 - trim_rect.x0) * PT_TO_MM, 1)
        trim_h_out = round((trim_rect.y1 - trim_rect.y0) * PT_TO_MM, 1)
        fixes_applied.append(f"TrimBox gesetzt: Nettogröße {trim_w_out} × {trim_h_out} mm")
        return pdf_bytes, fixes_applied

    pdf_bytes = doc.tobytes(garbage=4, deflate=True)

    if not fixes_applied:
        fixes_applied.append("Keine Korrekturen notwendig")

    return pdf_bytes, fixes_applied


# ─────────────────────────────────────────────
#  USER STORE — Upstash Redis REST API
#  Kostenlos, persistent, keine Verifizierung nötig
# ─────────────────────────────────────────────
import urllib.request
import urllib.error
import urllib.parse

ADMIN_EMAIL    = os.environ.get("ADMIN_EMAIL", "dm@dcp-online.de")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "supersize")

# Kommagetrennte Admin-E-Mails unterstützen
_ADMIN_EMAILS  = [e.strip().lower() for e in ADMIN_EMAIL.split(",") if e.strip()]

def _is_admin(email: str, password: str) -> bool:
    """Prüft ob E-Mail + Passwort einem Admin-Account entsprechen."""
    return email.strip().lower() in _ADMIN_EMAILS and password == ADMIN_PASSWORD
UPSTASH_URL    = os.environ.get("UPSTASH_URL", "").rstrip("/")
UPSTASH_TOKEN  = os.environ.get("UPSTASH_TOKEN", "")

# SMTP config (Railway env vars)
FORMSPREE_ID   = os.environ.get("FORMSPREE_ID", "xaqlyark")
SMTP_HOST      = os.environ.get("SMTP_HOST", "smtp.ionos.de")
SMTP_PORT      = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER      = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD  = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM      = os.environ.get("SMTP_FROM", "hello@studiomarx.com")
NOTIFY_EMAIL   = os.environ.get("NOTIFY_EMAIL", "hello@studiomarx.com")

TRIAL_LIMIT    = int(os.environ.get("TRIAL_LIMIT", "20"))

_users: dict = {}
_loaded: bool = False

def load_users() -> dict:
    global _users, _loaded
    if _loaded and _users:
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
#  USAGE TRACKING
# ─────────────────────────────────────────────
def _get_current_month() -> str:
    from datetime import datetime
    return datetime.utcnow().strftime("%Y-%m")

def _track_usage(email: str, file_size_bytes: int):
    if not email: return
    key = f"pps_usage:{email.strip().lower()}"
    month = _get_current_month()
    try:
        raw = _upstash_get(key)
        data = json.loads(raw) if raw else {}
        if data.get("month") != month:
            data = {"month": month, "analyses": 0, "mb": 0.0}
        data["analyses"] = data.get("analyses", 0) + 1
        data["mb"] = round(data.get("mb", 0.0) + file_size_bytes / 1024 / 1024, 2)
        _upstash_set(key, json.dumps(data))
    except Exception as e:
        print(f"[PPS] usage tracking error: {e}", file=sys.stderr)

def _get_usage(email: str) -> dict:
    month = _get_current_month()
    if not email: return {"analyses": 0, "mb": 0.0, "month": month}
    key = f"pps_usage:{email.strip().lower()}"
    try:
        raw = _upstash_get(key)
        if not raw: return {"analyses": 0, "mb": 0.0, "month": month}
        data = json.loads(raw)
        if data.get("month") != month: return {"analyses": 0, "mb": 0.0, "month": month}
        return {"analyses": data.get("analyses", 0), "mb": round(data.get("mb", 0.0), 2), "month": month}
    except Exception:
        return {"analyses": 0, "mb": 0.0, "month": month}

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

def _notify_error(message: str):
    """Sendet eine Fehler-Benachrichtigung an den Admin."""
    try:
        from datetime import datetime
        now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        html = f"""
        <div style="font-family:monospace;padding:1.5rem;color:#1a1a18;background:#fdf0ee;border-left:4px solid #c0392b">
          <strong style="color:#c0392b">&#9888; PPS Fehler-Alarm</strong><br><br>
          <b>Zeitpunkt:</b> {now}<br>
          <b>Meldung:</b> {message}<br><br>
          <small style="color:#9a9a94">PPS Backend &mdash; Automatische Benachrichtigung</small>
        </div>"""
        _send_email(NOTIFY_EMAIL, f"&#9888; PPS Fehler: {message[:60]}", html)
    except Exception:
        pass  # Notification darf nie einen weiteren Fehler verursachen

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
    if _is_admin(email, password):
        admin_names = {
            "dm@dcp-online.de":      "David Marx",
            "o.knaup@dcp-online.de": "O. Knaup",
        }
        name = admin_names.get(email, "Admin")
        return {"success": True, "role": "admin", "name": name}
    users = load_users()
    if email in users and users[email]["password"] == password:
        return {"success": True, "role": "customer", "name": users[email]["name"], "usage": _get_usage(email)}
    raise HTTPException(401, "E-Mail oder Passwort ungültig.")

@app.get("/login")
def login_get(email: str, password: str):
    email = email.strip().lower()
    password = password.strip()
    if _is_admin(email, password):
        admin_names = {
            "dm@dcp-online.de":      "David Marx",
            "o.knaup@dcp-online.de": "O. Knaup",
        }
        name = admin_names.get(email, "Admin")
        return {"success": True, "role": "admin", "name": name}
    users = load_users()
    if email in users and users[email]["password"] == password:
        return {"success": True, "role": "customer", "name": users[email]["name"], "usage": _get_usage(email)}
    raise HTTPException(401, "E-Mail oder Passwort ungültig.")

@app.get("/admin/users")
def get_users(admin_email: str, admin_password: str):
    if not _is_admin(admin_email, admin_password):
        raise HTTPException(403, "Nicht autorisiert.")
    users = load_users()
    return {"users": [{"email": e, "name": d.get("name",""), "role": d.get("role","customer"), "company": d.get("company","")} for e, d in users.items()]}

@app.post("/admin/users")
def create_user(req: UserRequest, admin_email: str, admin_password: str):
    if not _is_admin(admin_email, admin_password):
        raise HTTPException(403, "Nicht autorisiert.")
    users = load_users()
    email = req.email.strip().lower()
    users[email] = {"name": req.name.strip(), "password": req.password.strip()}
    save_users(users)
    return {"success": True, "message": f"Benutzer {email} angelegt."}

@app.delete("/admin/users/{email}")
def delete_user(email: str, admin_email: str, admin_password: str):
    if not _is_admin(admin_email, admin_password):
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
        value = c.get("value") or "–"
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
                Paragraph(f'<font color="{dcol}"><b>{dpi:.1f} PPI</b></font>',
                          ps('di', fontSize=8)),
                Paragraph(f'{img.get("width_px",img.get("width",0))}×{img.get("height_px",img.get("height",0))}px · '
                          f'{img.get("colorspace","?")} · {dpi:.1f} PPI@1:1',
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
        _fix_styles = [
            ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#e8f4ec')),
            ('TOPPADDING', (0,0), (-1,-1), 7),
            ('BOTTOMPADDING', (0,0), (-1,-1), 7),
            ('LEFTPADDING', (0,0), (-1,-1), 10),
            ('LINEBELOW', (0,0), (-1,-2), 0.3, colors.HexColor('#d0cdc4')),
        ]
        if normal and warnings:
            _fix_styles.append(('BACKGROUND', (0, len(normal)), (-1,-1), colors.HexColor('#fdecea')))
        fixes_t.setStyle(TableStyle(_fix_styles))
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
#  ADMIN DASHBOARD ENDPOINTS
# ─────────────────────────────────────────────

class NotificationMsg(BaseModel):
    title: str
    message: str
    type: str = "info"   # info | warn | err

@app.post("/admin/notification")
def push_notification(req: NotificationMsg, admin_email: str, admin_password: str):
    """Pushes a notification to all users via Upstash."""
    if not _is_admin(admin_email, admin_password):
        raise HTTPException(403, "Nicht autorisiert.")
    from datetime import datetime as _dt2
    notif = {
        "id": int(_dt2.now().timestamp() * 1000),
        "title": req.title,
        "sub": req.message,
        "type": req.type,
        "time": _dt2.now().strftime("%H:%M"),
        "created": _dt2.now().strftime("%d.%m.%Y %H:%M"),
        "read": False,
    }
    existing_raw = _upstash_get("pps_global_notifications") or "[]"
    try:
        notifs = json.loads(existing_raw)
    except Exception:
        notifs = []
    notifs.insert(0, notif)
    notifs = notifs[:50]  # max 50
    _upstash_set("pps_global_notifications", json.dumps(notifs))
    return {"success": True, "notification": notif}

@app.get("/admin/notifications")
def get_notifications(admin_email: str, admin_password: str):
    if not _is_admin(admin_email, admin_password):
        raise HTTPException(403, "Nicht autorisiert.")
    raw = _upstash_get("pps_global_notifications") or "[]"
    try:
        return {"notifications": json.loads(raw)}
    except Exception:
        return {"notifications": []}

@app.delete("/admin/notifications")
def clear_notifications(admin_email: str, admin_password: str):
    if not _is_admin(admin_email, admin_password):
        raise HTTPException(403, "Nicht autorisiert.")
    _upstash_set("pps_global_notifications", "[]")
    return {"success": True}

@app.get("/system/notifications")
def get_global_notifications():
    """Public endpoint — returns global notifications for all users."""
    raw = _upstash_get("pps_global_notifications") or "[]"
    try:
        return {"notifications": json.loads(raw)}
    except Exception:
        return {"notifications": []}

@app.post("/admin/smtp-test")
def smtp_test(admin_email: str, admin_password: str):
    """Sends a test email to the admin address."""
    if not _is_admin(admin_email, admin_password):
        raise HTTPException(403, "Nicht autorisiert.")
    from datetime import datetime as _dt3
    html = f"""<div style="font-family:monospace;padding:1.5rem;color:#1a1a18">
      <strong style="color:#2d5a3d">&#10003; PPS SMTP Test erfolgreich</strong><br><br>
      Zeitpunkt: {_dt3.now().strftime("%d.%m.%Y %H:%M:%S")}<br>
      Von: {SMTP_FROM}<br>
      An: {NOTIFY_EMAIL}<br><br>
      <small style="color:#9a9a94">PPS Admin &mdash; SMTP-Test</small>
    </div>"""
    ok = _send_email(NOTIFY_EMAIL, "PPS SMTP Test", html)
    if ok:
        return {"success": True, "message": f"Test-Mail an {NOTIFY_EMAIL} gesendet."}
    else:
        raise HTTPException(500, "SMTP-Versand fehlgeschlagen. Bitte Variablen pruefen.")

@app.post("/admin/users/{email}/upgrade")
def upgrade_user(email: str, admin_email: str, admin_password: str):
    """Upgrades a trial user to full customer."""
    if not _is_admin(admin_email, admin_password):
        raise HTTPException(403, "Nicht autorisiert.")
    users = load_users()
    email = email.strip().lower()
    if email not in users:
        raise HTTPException(404, "Benutzer nicht gefunden.")
    users[email]["role"] = "customer"
    users[email].pop("trial_analyses", None)
    users[email].pop("trial_limit", None)
    save_users(users)
    return {"success": True, "message": f"{email} auf Vollzugang upgraded."}

@app.get("/admin/stats")
def get_stats(admin_email: str, admin_password: str):
    """Returns trial and user statistics."""
    if not _is_admin(admin_email, admin_password):
        raise HTTPException(403, "Nicht autorisiert.")
    users = load_users()
    trials = {e: u for e, u in users.items() if u.get("role") == "trial"}
    customers = {e: u for e, u in users.items() if u.get("role") != "trial"}
    total_analyses = sum(int(u.get("trial_analyses", 0)) for u in trials.values())
    return {
        "total_users": len(users),
        "trial_users": len(trials),
        "customer_users": len(customers),
        "total_trial_analyses": total_analyses,
        "trials": [
            {
                "email": e,
                "name": u.get("name",""),
                "company": u.get("company",""),
                "analyses_used": int(u.get("trial_analyses", 0)),
                "trial_limit": int(u.get("trial_limit", 10)),
                "created": u.get("created",""),
            }
            for e, u in trials.items()
        ],
        "customers": [
            {"email": e, "name": u.get("name",""), "company": u.get("company","")}
            for e, u in customers.items()
        ]
    }

# ─────────────────────────────────────────────
#  SYSTEM BANNER ENDPOINT
# ─────────────────────────────────────────────
class BannerUpdate(BaseModel):
    message: str = ""
    type: str = "info"       # info | warn | err
    link_text: str = ""
    link_url: str = ""

@app.get("/system/banner")
def get_banner():
    """Liefert die aktuelle System-Bannermeldung."""
    raw = _upstash_get("pps_banner")
    if not raw:
        return {"message": "", "type": "info"}
    try:
        return json.loads(raw)
    except Exception:
        return {"message": "", "type": "info"}

@app.post("/system/banner")
def set_banner(req: BannerUpdate, admin_email: str, admin_password: str):
    """Setzt oder loescht die System-Bannermeldung (nur Admin)."""
    if not _is_admin(admin_email, admin_password):
        raise HTTPException(403, "Nicht autorisiert.")
    if req.message:
        _upstash_set("pps_banner", json.dumps({
            "message": req.message,
            "type": req.type,
            "link_text": req.link_text,
            "link_url": req.link_url,
        }))
        return {"success": True, "action": "set", "message": req.message}
    else:
        # Leer = Banner entfernen
        try:
            url = f"{UPSTASH_URL}/del/pps_banner"
            req2 = urllib.request.Request(url, method="POST",
                headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"})
            urllib.request.urlopen(req2, timeout=5)
        except Exception:
            pass
        return {"success": True, "action": "cleared"}

# ─────────────────────────────────────────────
#  REPORT AN ISSUE ENDPOINT
# ─────────────────────────────────────────────
class IssueReport(BaseModel):
    user: str
    page: str
    message: str
    timestamp: str
    screenshot: Optional[str] = None
    backend: Optional[str] = None
    store_in_dashboard: Optional[bool] = False  # Frontend-Flag, wird in Upstash gespeichert

    class Config:
        extra = 'ignore'  # Unbekannte Felder ignorieren statt 422 zu werfen

@app.post("/report-issue")
def report_issue(req: IssueReport):
    """Empfaengt Fehlerberichte — sofort in Upstash, E-Mail im Hintergrund."""
    import threading, json as _j2, time as _t2

    # 1. Sofort in Upstash speichern (< 200ms) → schnelle Antwort
    report_id = str(int(_t2.time() * 1000))
    try:
        existing = _upstash_get("pps_reports") or "[]"
        reports = _j2.loads(existing)
        reports.insert(0, {
            "id": report_id,
            "user": req.user,
            "page": req.page,
            "message": req.message,
            "timestamp": req.timestamp,
            "status": "open",
            "has_screenshot": bool(req.screenshot)
        })
        _upstash_set("pps_reports", _j2.dumps(reports[:100]))
    except Exception as e:
        print(f"[PPS] report upstash error: {e}", file=sys.stderr)

    # 2. E-Mail im Hintergrund — blockiert den Request nicht
    def _send_bg():
        try:
            screenshot_html = ""
            if req.screenshot and req.screenshot.startswith("data:image"):
                screenshot_html = f'''<div style="margin-top:1rem">
              <div style="font-size:10px;color:#9a9a94;margin-bottom:.5rem;font-family:monospace;text-transform:uppercase;letter-spacing:.1em">Screenshot</div>
              <img src="{req.screenshot}" style="width:100%;border:1px solid #d0cdc4;border-radius:3px" alt="Screenshot">
            </div>'''
            html = f"""<div style="font-family:monospace;max-width:600px;padding:1.5rem;color:#1a1a18">
          <div style="background:#f5f3ee;border-left:3px solid #c0392b;padding:1rem;margin-bottom:1.5rem">
            <strong style="color:#c0392b">&#9888; Neuer Fehlerbericht</strong>
          </div>
          <table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:1rem">
            <tr><td style="color:#9a9a94;padding:4px 8px;width:120px">Nutzer</td><td style="padding:4px 8px"><b>{req.user}</b></td></tr>
            <tr><td style="color:#9a9a94;padding:4px 8px">Seite</td><td style="padding:4px 8px">{req.page}</td></tr>
            <tr><td style="color:#9a9a94;padding:4px 8px">Zeitpunkt</td><td style="padding:4px 8px">{req.timestamp}</td></tr>
          </table>
          <div style="background:white;border:1px solid #d0cdc4;border-radius:3px;padding:1rem;font-size:14px;line-height:1.6">{req.message}</div>
          {screenshot_html}
          <div style="margin-top:1.5rem;font-size:10px;color:#9a9a94">PPS XPRESS — Automatischer Fehlerbericht · ID: {report_id}</div>
        </div>"""
            _send_email(NOTIFY_EMAIL,
                        f"PPS Fehlerbericht: {req.user} — {req.message[:60]}", html)
        except Exception as e:
            print(f"[PPS] report email error: {e}", file=sys.stderr)

    threading.Thread(target=_send_bg, daemon=True).start()

    # 3. Sofort zurück — Frontend wartet nicht auf SMTP
    return {"success": True, "id": report_id}

# ─────────────────────────────────────────────
#  HEALTH CHECK
# ─────────────────────────────────────────────


@app.get("/admin/reports")
def get_reports(admin_email: str, admin_password: str):
    """Alle Fehlerberichte aus Upstash."""
    if not _is_admin(admin_email, admin_password):
        raise HTTPException(403, "Nicht autorisiert.")
    try:
        raw = _upstash_get("pps_reports") or "[]"
        reports = json.loads(raw)
        return {"reports": reports, "count": len(reports)}
    except Exception as e:
        return {"reports": [], "count": 0}

@app.delete("/admin/reports/{report_id}")
def delete_report(report_id: str, admin_email: str, admin_password: str):
    """Einzelnen Bericht löschen."""
    if not _is_admin(admin_email, admin_password):
        raise HTTPException(403, "Nicht autorisiert.")
    try:
        raw = _upstash_get("pps_reports") or "[]"
        reports = json.loads(raw)
        reports = [r for r in reports if r.get("id") != report_id]
        _upstash_set("pps_reports", json.dumps(reports))
        return {"success": True}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.post("/admin/reports/{report_id}/status")
def update_report_status(report_id: str, admin_email: str, admin_password: str, status: str = "resolved"):
    """Status eines Berichts ändern (open/resolved)."""
    if not _is_admin(admin_email, admin_password):
        raise HTTPException(403, "Nicht autorisiert.")
    try:
        raw = _upstash_get("pps_reports") or "[]"
        reports = json.loads(raw)
        for r in reports:
            if r.get("id") == report_id:
                r["status"] = status
                break
        _upstash_set("pps_reports", json.dumps(reports))
        return {"success": True}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/admin/config")
def get_admin_config(admin_email: str, admin_password: str):
    """Returns safe admin configuration values (no passwords)."""
    if not _is_admin(admin_email, admin_password):
        raise HTTPException(403, "Nicht autorisiert.")
    return {
        "smtp_host": SMTP_HOST or "(nicht gesetzt)",
        "smtp_user": SMTP_USER or "(nicht gesetzt)",
        "notify_email": NOTIFY_EMAIL or "(nicht gesetzt)",
        "trial_limit": TRIAL_LIMIT,
        "admin_email": ADMIN_EMAIL,
    }

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

@app.post("/track")
def track_usage(req: dict):
    try:
        email = req.get("email","").strip().lower()
        password = req.get("password","").strip()
        size_mb = float(req.get("size_mb", 0))
        if not email: return {"success": False}
        users = load_users()
        if email not in users or users[email].get("password") != password:
            return {"success": False}
        _track_usage(email, int(size_mb * 1024 * 1024))
        return {"success": True, "usage": _get_usage(email)}
    except Exception as e:
        return {"success": False}

@app.get("/usage")
def get_usage_endpoint(email: str, password: str):
    email = email.strip().lower()
    users = load_users()
    if email not in users or users[email]["password"] != password:
        raise HTTPException(401, "Nicht autorisiert.")
    return _get_usage(email)

@app.get("/admin/usage-all")
def get_all_usage(admin_email: str, admin_password: str):
    if not _is_admin(admin_email, admin_password):
        raise HTTPException(403, "Nicht autorisiert.")
    users = load_users()
    month = _get_current_month()
    result = []
    for email, udata in users.items():
        usage = _get_usage(email)
        result.append({"email": email, "name": udata.get("name",""),
            "role": udata.get("role","customer"),
            "analyses": usage.get("analyses",0), "mb": usage.get("mb",0.0),
            "month": usage.get("month",month)})
    result.sort(key=lambda x: x["analyses"], reverse=True)
    return {"month": month, "users": result,
            "total_analyses": sum(r["analyses"] for r in result),
            "total_mb": round(sum(r["mb"] for r in result), 2)}

@app.post("/parse-offer")
def parse_offer(req: dict):
    import urllib.error as _ue
    ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
    if not ANTHROPIC_KEY:
        raise HTTPException(500, "ANTHROPIC_API_KEY nicht gesetzt.")
    pdf_text = req.get("pdf_text", "")[:8000]
    prompt = f"""Extrahiere aus diesem DCP Angebots-Text alle Informationen. Antworte NUR mit JSON ohne Backticks:
{{"angebot_nr":"...","datum":"...","kd_nr":"...","kunde":"...","projekt":"...","objekt_bez":"...","ansprechpartner":"...","objekte":[{{"name":"...","breite_mm":1234,"hoehe_mm":5678,"menge":1}}],"gesamtbetrag":"...","lieferzeit":"..."}}
Text:\n{pdf_text}"""
    payload = json.dumps({"model":"claude-sonnet-4-20250514","max_tokens":1000,
        "messages":[{"role":"user","content":prompt}]}).encode()
    try:
        req2 = urllib.request.Request("https://api.anthropic.com/v1/messages",
            data=payload, method="POST",
            headers={"Content-Type":"application/json","x-api-key":ANTHROPIC_KEY,
                     "anthropic-version":"2023-06-01"})
        with urllib.request.urlopen(req2, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            raw = result["content"][0]["text"].strip().replace("```json","").replace("```","").strip()
            return {"success": True, "data": json.loads(raw)}
    except Exception as e:
        raise HTTPException(500, f"Fehler: {str(e)[:200]}")

@app.get("/health")
def health():
    return {"status": "ok", "service": "PPS API", "version": "2.3.2"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
    

