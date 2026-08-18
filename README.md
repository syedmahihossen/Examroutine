# ExamRoutine

Fast automatic CSE Mid/Final exam routine generator for DIU.

## Features

- Mid and Final only
- Spring, Summer, Fall
- Any academic year
- Any CSE section such as `65_L`, `65_N`, `66_A`
- Automatic DIU Notice Board discovery
- Supports PDF and XLSX exam routines
- Finds the separate seat-plan notice when available
- Seat plan is optional: if unavailable, ROOM / SEATS / TOTAL are omitted
- Matches section + date + slot + course
- 5-minute result cache
- Prevents duplicate simultaneous crawls for the same request
- One short Playwright discovery pass before document validation
- Quiet PDF logging (no repeated CropBox warnings)
- PNG/PDF export from a fixed export canvas

## Local

### Backend

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

### Frontend

In another terminal:

```bash
cd frontend
python3 -m http.server 5500 --bind 127.0.0.1
```

Open `http://127.0.0.1:5500`.

The frontend automatically uses `http://127.0.0.1:8000` on localhost and `https://examroutine.onrender.com` when deployed.

## Render backend

Use the included `render.yaml` (recommended) or set:

**Build command**

```bash
bash build.sh
```

**Start command**

```bash
uvicorn backend.main:app --host 0.0.0.0 --port $PORT
```

**Environment**

| Key | Value |
|-----|--------|
| `PLAYWRIGHT_BROWSERS_PATH` | `/opt/render/project/src/.playwright` |

Important: the build **must** run `playwright install chromium`. If you only
`pip install playwright`, the Python package is present but the browser binary
is missing and you will see:

`BrowserType.launch: Executable doesn't exist ...`

After changing the build command, trigger a **manual deploy / clear build cache**
on Render so Chromium is installed fresh.

## API

Health:

`GET /api/health`

Automatic routine:

`GET /api/auto-analyze?section=65_L&exam_type=final&semester=summer&year=2026&include_seat_plan=true`

## Important behavior

The system does not treat a missing seat plan as a failure. The routine is still generated from the official routine. Room/seat/total columns appear only when a matching seat plan is actually verified.

The crawler first ranks notice cards using title/context metadata. It then opens only the strongest routine/seat-plan candidates and validates the actual downloaded document before accepting it. This avoids parsing dozens of unrelated PDFs.
