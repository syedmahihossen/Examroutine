# ExamRoutine Auto v8.1

Fast automatic CSE Mid/Final exam routine generator for DIU students.

**New in v8.1**
- Proactive background refresh (GitHub Actions) so new routines are discovered *before* students click
- Longer cache TTL (30 min)
- Protected `/api/refresh` endpoint
- Frontend ready for **Cloudflare Pages**
- Clear free deployment path

---

## Architecture (Free & Fast)

| Part              | Service              | Cost     | Purpose                                      |
|-------------------|----------------------|----------|----------------------------------------------|
| Frontend          | Cloudflare Pages     | Free     | Static site, global CDN, instant             |
| Backend API       | Render               | Free     | Playwright discovery + PDF/XLSX parsing      |
| Auto-refresh      | GitHub Actions       | Free     | Checks notice board every 25 min, warms cache|

Students almost never wait for a cold Playwright crawl because the scheduled job keeps the cache warm.

---

## Features

- Mid and Final only
- Spring, Summer, Fall
- Any academic year
- Any CSE section (`65_L`, `65_N`, `66_A`, …)
- Automatic DIU Notice Board discovery
- Supports PDF and XLSX exam routines
- Finds separate seat-plan notice when available
- Seat plan is optional (ROOM / SEATS / TOTAL only appear when matched)
- 30-minute result + document cache
- Prevents duplicate simultaneous crawls
- PNG / PDF export

---

## 1. Deploy Backend (Render – Free)

1. Push this repository to GitHub.
2. Go to [render.com](https://render.com) → **New → Web Service**.
3. Connect the repository.
4. Settings:
   - **Root Directory**: leave empty (or set to project root)
   - **Build Command**:  
     `pip install -r backend/requirements.txt && python -m playwright install chromium`
   - **Start Command**:  
     `uvicorn backend.main:app --host 0.0.0.0 --port $PORT`
   - **Health Check Path**: `/api/health`
5. After deploy, open the service → **Environment**.
   - Copy the value of `REFRESH_SECRET` (Render generates it automatically if you used `render.yaml`).
6. Note your backend URL, e.g. `https://examroutine-api.onrender.com`.

> Free Render instances sleep after ~15 min of inactivity.  
> The GitHub Actions job will wake them up regularly.

---

## 2. Deploy Frontend (Cloudflare Pages – Free)

1. Go to [dash.cloudflare.com](https://dash.cloudflare.com) → **Workers & Pages** → **Create** → **Pages**.
2. Connect the same GitHub repository.
3. Build settings:
   - **Framework preset**: None
   - **Build command**: leave empty
   - **Build output directory**: `frontend`
4. Deploy.

After deploy, the site will use the production API URL defined in `frontend/app.js`  
(`https://examroutine.onrender.com` by default).  

If your Render URL is different, either:

- Change the fallback URL inside `frontend/app.js`, **or**
- Open the site with `?api=https://your-backend.onrender.com`

---

## 3. Enable Automatic Refresh (GitHub Actions – Free)

1. In your GitHub repository go to **Settings → Secrets and variables → Actions**.
2. Add two **Repository secrets**:
   - `BACKEND_URL` → `https://your-service.onrender.com` (no trailing slash)
   - `REFRESH_SECRET` → the same secret you set/copied from Render
3. (Optional) Add repository **Variables** if you want different defaults:
   - `DEFAULT_SECTION` = `65_L`
   - `DEFAULT_SEMESTER` = `summer`
   - `DEFAULT_YEAR` = `2026`
   - `DEFAULT_EXAM_TYPE` = `final`

The workflow (`.github/workflows/refresh-routine.yml`) runs every 25 minutes and also supports manual runs from the Actions tab.

---

## Local Development

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

```bash
cd frontend
python3 -m http.server 5500 --bind 127.0.0.1
```

Open http://127.0.0.1:5500

---

## API

| Endpoint | Description |
|----------|-------------|
| `GET /api/health` | Health check |
| `GET /api/auto-analyze?section=65_L&exam_type=final&semester=summer&year=2026` | Main endpoint used by the frontend |
| `GET/POST /api/refresh?exam_type=final&semester=summer&year=2026&secret=...` | Force refresh (used by GitHub Actions) |
| `GET /api/cache-status` | Cache diagnostics |
| `GET /api/discovery-debug?...` | See what the notice-board crawler finds |

---

## Important behaviour

- A missing seat plan is **not** treated as an error. The routine is still shown.
- Room / Seats / Total columns appear only when a matching seat plan is verified.
- The crawler ranks notice cards first, then opens only the strongest candidates.
- Document cache is shared across all sections of the same exam session (one download serves everyone).

---

## Tips for best free experience

1. After deploying, manually trigger the GitHub Action once to warm the cache.
2. If the first request after a long sleep is slow, wait 30–60 seconds and try again (Render is waking up).
3. You can change the cron frequency in the workflow file if needed (e.g. every 15 min).

Enjoy the faster experience!
