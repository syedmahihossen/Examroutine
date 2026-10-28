# DIU ExamRoutine — corrected deployment package

This project finds DIU CSE Mid/Final routines, parses the official routine and optional seat plan, and publishes per-section data to Firebase Realtime Database.

## Important security setup

A Firebase credential was hard-coded in the source file you uploaded. Treat it as exposed and rotate/revoke it. The corrected code does not contain that credential.

In Render, set:

- `REFRESH_SECRET`: a long random secret (protects `/api/refresh`).
- `FIREBASE_DB_URL`: `https://examroutine-d5392-default-rtdb.firebaseio.com`.
- `FIREBASE_SERVICE_ACCOUNT_JSON`: the full Firebase service-account JSON as a single-line JSON environment variable. Use a service account with database write permissions. Do not commit this value to GitHub or share it in chat.

The frontend only needs public read access to the Firebase paths it displays. Keep writes restricted; the backend Admin SDK uses the service account and bypasses client database rules.

## Deploy

1. Put this folder's contents at the root of the GitHub repository.
2. Deploy `render.yaml` to Render. Confirm the service root is the repository root.
3. Add `FIREBASE_SERVICE_ACCOUNT_JSON` in Render Environment settings.
4. Set `REFRESH_SECRET` in Render. If Render generated it automatically, copy its current value into the GitHub Actions repository secret named `REFRESH_SECRET`.
5. In GitHub repository **Settings → Secrets and variables → Actions → Variables**, add repository variable `RENDER_API_URL` with the exact public Render URL, for example `https://examroutine-api.onrender.com` (use the URL actually shown in your Render dashboard).
6. Enable the GitHub Actions workflow in `.github/workflows/refresh-routines.yml`, then use **Actions → Refresh DIU Exam Routines → Run workflow**.
7. Host `frontend/` on Cloudflare Pages, setting the build output directory to `frontend` and no build command.

## Refresh behavior

`POST /api/refresh` checks the current academic year for Spring/Summer/Fall Mid and Final sessions, refreshes discovery caches, parses verified routines, and writes session-specific records to `routines_v2/{section}/{exam_type}/{semester}/{year}`. It also updates the legacy `routines/{section}` path for older clients. A failure in one session is reported separately rather than silently treated as success.

Optional manual refresh for a single session:

`POST /api/refresh?semester=fall&year=2026&exam_type=mid`

Send `Authorization: Bearer <REFRESH_SECRET>`.

## How discovery works (v10)

DIU's main notice board is now a JavaScript app, so the old browser crawl saw no notices. Discovery now reads the server-rendered CSE department list (`/department-notice/cse`, falling back to the mirror hosts), so it is CSE-only by construction and needs no browser (Playwright was removed).

- Titles are classified by semester, year, Mid/Final and routine/seat plan. Improvement exams, class routines and registration notices are ignored. "CSE" is not required in the title.
- The newest matching notice wins, so "Updated ..." notices replace older ones. A routine is accepted only if its PDF verifies (right semester/year, contains the batch).
- The seat plan is taken from the same notice when bundled ("Routine and Seat Plan"), otherwise from the newest separate seat-plan notice, and must overlap the routine.

If a routine is published but not found, open `/api/discovery-debug?semester=fall&year=2026&exam_type=mid` and check which notices are listed and how they were classified.

## Local checks

```bash
cd backend
pip install -r requirements.txt
python -m pytest -q test_core.py test_noticeboard.py
uvicorn main:app --reload
```

If a routine is published but not discovered, open `/api/discovery-debug?semester=fall&year=2026&exam_type=mid` and inspect candidate titles/URLs. Do not treat a 200 response from the refresh endpoint as proof of a section update unless its `processed` list contains the session.
