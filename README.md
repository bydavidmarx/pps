# PPS Backend – Pre Production Service API

FastAPI + PyMuPDF Backend für die PDF-Tiefenanalyse.

## Was dieses Backend kann (vs. Browser-Version)

| Feature | Browser | Backend |
|---|---|---|
| Seitenverhältnis | ✓ | ✓ |
| Beschnittzugabe | ~ | ✓ exakt (TrimBox/BleedBox) |
| Beschnittzeichen | ~ geschätzt | ✓ exakt |
| Bildauflösung | ✗ | ✓ **jedes Bild einzeln, exakte DPI** |
| Farbraum | ✗ | ✓ CMYK/RGB pro Bild |
| ICC-Profil | ✗ | ✓ Name + Validierung |
| Schriften | ~ | ✓ eingebettet/nicht eingebettet |
| Fix: Beschnittzeichen | ✗ | ✓ |
| Fix: Beschnittzugabe | ✗ | ✓ |
| Fix: Farbraum (Phase 3) | ✗ | via Ghostscript |

---

## Lokaler Start (Entwicklung)

```bash
# 1. Python-Umgebung
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 2. Abhängigkeiten
pip install -r requirements.txt

# 3. Server starten
python main.py
# → läuft auf http://localhost:8000
```

API-Dokumentation: http://localhost:8000/docs

---

## Deployment auf Railway (empfohlen, ~5€/Monat)

1. Account auf https://railway.app anlegen
2. "New Project" → "Deploy from GitHub"
3. Diesen Ordner als Repository pushen
4. Railway erkennt den Dockerfile automatisch
5. URL wird automatisch vergeben (z.B. `pps-api.railway.app`)

---

## Deployment auf Render (kostenlose Option)

1. Account auf https://render.com
2. "New Web Service" → GitHub repo verbinden
3. Build Command: `pip install -r requirements.txt`
4. Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. Free tier: verfügbar (cold start ~30 Sek.)

---

## API Endpoints

### POST /analyze
PDF analysieren und Qualitätsbericht erhalten.

**Form-Felder:**
- `file` — PDF-Datei (max. 50 MB)
- `print_width_mm` — Druckbreite in mm (z.B. 3000)
- `print_height_mm` — Druckhöhe in mm (z.B. 4000)
- `scale` — Maßstab (z.B. 10 für 1:10)
- `job_name` — Auftragsname (optional)

**Antwort:**
```json
{
  "filename": "meinpdf.pdf",
  "overall_status": "error",
  "summary": "2 Fehler gefunden...",
  "checks": [
    {
      "id": "resolution",
      "label": "Bildauflösung",
      "status": "warn",
      "value": "3 Bilder · 31.2–68.4 DPI (= 312–684 DPI bei 1:1)",
      "note": "1 Bild unter 50 DPI. Upscaling möglich.",
      "fixable": true,
      "details": {
        "images": [
          {"dpi_in_doc": 31.2, "dpi_at_1to1": 312, "colorspace": "RGB", ...}
        ]
      }
    }
  ],
  "meta": { ... }
}
```

### POST /fix
PDF automatisch korrigieren und korrigiertes PDF herunterladen.

**Form-Felder:**
- `file` — Original-PDF
- `print_width_mm`, `print_height_mm`, `scale` — wie bei /analyze
- `fix_cropmarks` — Beschnittzeichen entfernen (true/false)
- `fix_bleed` — Beschnittzugabe hinzufügen (true/false)
- `fix_colorspace` — Farbraum konvertieren (true/false, Phase 3)

**Antwort:** Korrigiertes PDF als Download

### GET /health
```json
{"status": "ok", "service": "PPS API", "version": "1.0.0"}
```

---

## Frontend einbinden

Im PPS-Browser-Widget die API-URL eintragen:
```javascript
const API_URL = "https://deine-url.railway.app";

// Analyse aufrufen
const formData = new FormData();
formData.append("file", pdfFile);
formData.append("print_width_mm", 3000);
formData.append("print_height_mm", 4000);
formData.append("scale", 10);

const response = await fetch(`${API_URL}/analyze`, {
  method: "POST",
  body: formData
});
const result = await response.json();
```

---

## Geplante Erweiterungen (Phase 3)

- **Ghostscript-Integration** für RGB→CMYK Konvertierung mit ISO Coated v2
- **Real-ESRGAN Upscaling** für Bilder zwischen 25–50 DPI
- **User-Auth** via Supabase
- **Job-History** — alle analysierten Dateien pro Kunde
- **White-Label** — eigenes Logo und Farben pro Kunde
