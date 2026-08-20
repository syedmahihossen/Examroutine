// ExamRoutine frontend — patched
// Firebase-first lookup, form submit, dark mode, collapsed verification, mobile cards

const queryApi = new URLSearchParams(location.search).get("api");
const isLocal =
  location.protocol === "file:" ||
  location.hostname === "127.0.0.1" ||
  location.hostname === "localhost";

const API =
  window.EXAM_API ||
  queryApi ||
  (isLocal ? "http://127.0.0.1:8000" : "https://examroutine.onrender.com");
const FIREBASE_BASE_URL = "https://examroutine-d5392-default-rtdb.firebaseio.com/routines";
const FIREBASE_META_URL = "https://examroutine-d5392-default-rtdb.firebaseio.com/metadata.json";
const FIREBASE_INDEX_URL = "https://examroutine-d5392-default-rtdb.firebaseio.com/routines.json?shallow=true";
const THEME_KEY = "examroutine-theme";

const $ = (id) => document.getElementById(id);
let data = null;
let busy = false;
let detailsOpen = false;
let statusTimer = null;
let sharedMeta = null; // { routine_url, seat_plan_url } from Firebase metadata
let sectionIndex = null; // string[] of known section keys from Firebase
let sectionIndexPromise = null;

initTheme();
bindEvents();
applyUrlParams();
checkBackend();
prefetchMetadata();
prefetchSectionIndex();
maybeAutoSearchFromUrl();

function bindEvents() {
  $("routineForm").addEventListener("submit", (e) => {
    e.preventDefault();
    autoAnalyze();
  });
  $("png").addEventListener("click", downloadPNG);
  $("pdf").addEventListener("click", downloadPDF);
  $("pngMobile").addEventListener("click", downloadPNG);
  $("pdfMobile").addEventListener("click", downloadPDF);
  $("shareLink").addEventListener("click", copyShareLink);
  $("shareLinkMobile").addEventListener("click", copyShareLink);
  $("toggleDetails").addEventListener("click", toggleDetails);
  $("themeToggle").addEventListener("click", toggleTheme);

  // Restore last section if URL did not provide one
  if (!$("section").value) {
    try {
      const saved = localStorage.getItem("examroutine-section");
      if (saved && /^\d{2,3}_[A-Z0-9]+$/i.test(saved)) {
        $("section").value = saved;
      }
    } catch (_) {}
  }
}

/** Priority A2: ?section=65_K&semester=summer&year=2026&exam=final&auto=1 */
function applyUrlParams() {
  const q = new URLSearchParams(location.search);
  const section = (q.get("section") || "").trim().toUpperCase().replace(/-/g, "_");
  if (/^\d{2,3}_[A-Z0-9]+$/.test(section)) {
    $("section").value = section;
  }
  const semester = (q.get("semester") || "").toLowerCase();
  if (["spring", "summer", "fall"].includes(semester)) {
    $("semester").value = semester;
  }
  const year = (q.get("year") || "").trim();
  if (/^20\d{2}$/.test(year)) {
    const sel = $("academicYear");
    if (![...sel.options].some((o) => o.value === year)) {
      const opt = document.createElement("option");
      opt.value = year;
      opt.textContent = year;
      sel.appendChild(opt);
    }
    sel.value = year;
  }
  const exam = (q.get("exam") || q.get("exam_type") || q.get("type") || "").toLowerCase();
  if (exam === "mid" || exam === "final") {
    $("examType").value = exam;
  }
}

function maybeAutoSearchFromUrl() {
  const q = new URLSearchParams(location.search);
  const section = (q.get("section") || "").trim();
  const auto = q.get("auto");
  // Auto-run when section is in URL, unless auto=0
  if (section && auto !== "0" && auto !== "false") {
    // Slight delay so the form is painted
    setTimeout(() => autoAnalyze(), 50);
  }
}

function syncUrlFromForm() {
  try {
    const section = $("section").value.trim().toUpperCase().replace(/-/g, "_");
    if (!/^\d{2,3}_[A-Z0-9]+$/.test(section)) return;
    const params = new URLSearchParams();
    params.set("section", section);
    params.set("semester", $("semester").value);
    params.set("year", $("academicYear").value);
    params.set("exam", $("examType").value);
    const url = `${location.pathname}?${params.toString()}`;
    history.replaceState(null, "", url);
  } catch (_) {}
}

/** Priority A3: official PDF URLs from Firebase metadata */
async function prefetchMetadata() {
  try {
    const r = await fetch(FIREBASE_META_URL, { cache: "no-store" });
    if (!r.ok) return;
    const meta = await r.json();
    if (meta && (meta.routine_url || meta.seat_plan_url)) {
      sharedMeta = meta;
      // If routine already on screen without source links, refresh the box
      if (data?.exams?.length) renderSource();
    }
  } catch (e) {
    console.warn("Metadata prefetch failed:", e.message);
  }
}

function ensureSourceFromMeta(payload) {
  if (!payload) return payload;
  const hasSource =
    payload.source && (payload.source.routine_url || payload.source.seat_plan_url);
  if (hasSource || !sharedMeta) return payload;
  return {
    ...payload,
    source: {
      automatic: true,
      routine_url: sharedMeta.routine_url || null,
      seat_plan_url: sharedMeta.seat_plan_url || null,
      routine_title: "Official examination routine (from Notice Board cache)"
    }
  };
}

/** Priority A1: re-render labels when exam end times pass */
function startStatusClock() {
  stopStatusClock();
  if (!data?.exams?.length) return;

  const tick = () => {
    if (!data?.exams?.length || document.hidden) return;
    // Re-render routine UI only (banner, Done/Ongoing badges) — no network
    try {
      generateRoutine();
      if (detailsOpen) renderResult();
    } catch (e) {
      console.warn("Status refresh failed:", e);
    }
  };

  // Align to the next minute boundary, then every 30s
  const msToNextMinute = 60000 - (Date.now() % 60000);
  statusTimer = setTimeout(() => {
    tick();
    statusTimer = setInterval(tick, 30000);
  }, Math.min(msToNextMinute, 30000));
}

function stopStatusClock() {
  if (!statusTimer) return;
  clearTimeout(statusTimer);
  clearInterval(statusTimer);
  statusTimer = null;
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && data?.exams?.length) {
    try {
      generateRoutine();
    } catch (_) {}
  }
});

function initTheme() {
  let theme = "light";
  try {
    theme = localStorage.getItem(THEME_KEY) || "light";
  } catch (_) {}
  if (theme === "dark") {
    document.documentElement.setAttribute("data-theme", "dark");
  } else {
    document.documentElement.removeAttribute("data-theme");
  }
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = theme === "dark" ? "#0b1220" : "#082c5c";
}

function toggleTheme() {
  const isDark = document.documentElement.getAttribute("data-theme") === "dark";
  const next = isDark ? "light" : "dark";
  if (next === "dark") {
    document.documentElement.setAttribute("data-theme", "dark");
  } else {
    document.documentElement.removeAttribute("data-theme");
  }
  try {
    localStorage.setItem(THEME_KEY, next);
  } catch (_) {}
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = next === "dark" ? "#0b1220" : "#082c5c";
}

async function checkBackend() {
  try {
    const r = await fetch(`${API}/api/health`, { cache: "no-store" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    console.log("ExamRoutine backend:", await r.json());
  } catch (e) {
    console.warn("Backend unavailable:", e.message);
  }
}

function status(message, error = false, loading = false) {
  const el = $("status");
  el.textContent = message;
  el.classList.remove("hidden", "error", "loading");
  if (error) el.classList.add("error");
  if (loading) el.classList.add("loading");
}

function setBusy(value) {
  busy = value;
  const button = $("autoAnalyze");
  button.disabled = value;
  button.classList.toggle("loading-button", value);
  button.setAttribute("aria-busy", value ? "true" : "false");
}

function toggleDetails() {
  detailsOpen = !detailsOpen;
  const panel = $("exams");
  const btn = $("toggleDetails");
  if (detailsOpen) {
    panel.hidden = false;
    panel.classList.remove("collapsed");
    btn.textContent = "Hide details";
    btn.setAttribute("aria-expanded", "true");
  } else {
    panel.hidden = true;
    panel.classList.add("collapsed");
    btn.textContent = "Show details";
    btn.setAttribute("aria-expanded", "false");
  }
}

function matchesFormFilters(payload, examType, semester, year) {
  if (!payload || !payload.exams || !payload.exams.length) return false;
  const et = String(payload.exam_type || "").toLowerCase();
  const sem = String(payload.semester || "").toLowerCase();
  const yr = String(payload.year || "");
  // Soft match: if metadata missing, accept (legacy cache entries)
  if (et && et !== examType) return false;
  if (sem && sem !== semester && !sem.includes(semester)) return false;
  if (yr && year && yr !== String(year)) return false;
  return true;
}


/** Priority B4: legacy flat key (production) + optional structured key under routines_v2
 *  NOTE: Never nest under /routines/{section}/... — that would wipe the legacy object in RTDB.
 */
const FIREBASE_V2_BASE = "https://examroutine-d5392-default-rtdb.firebaseio.com/routines_v2";

function structuredRoutineUrl(section, examType, semester, year) {
  return `${FIREBASE_V2_BASE}/${encodeURIComponent(section)}/${encodeURIComponent(examType)}/${encodeURIComponent(semester)}/${encodeURIComponent(year)}.json`;
}

function legacyRoutineUrl(section) {
  return `${FIREBASE_BASE_URL}/${encodeURIComponent(section)}.json`;
}

function isUsableRoutine(json) {
  return Boolean(json && Array.isArray(json.exams) && json.exams.length > 0);
}

async function fetchFirebaseRoutine(section, examType, semester, year) {
  // 1) Legacy flat key first (what production actually has today)
  try {
    const r = await fetch(legacyRoutineUrl(section), { cache: "no-store" });
    if (r.ok) {
      const json = await r.json();
      if (isUsableRoutine(json) && matchesFormFilters(json, examType, semester, year)) {
        return { data: json, source: "legacy" };
      }
      if (isUsableRoutine(json)) {
        // Prefer showing cached routine even if semester/year metadata differs slightly
        return { data: json, source: "legacy" };
      }
    }
  } catch (e) {
    console.warn("Legacy Firebase miss:", e.message);
  }

  // 2) Structured key (routines_v2) — populated only after backend redeploy
  try {
    const r = await fetch(structuredRoutineUrl(section, examType, semester, year), {
      cache: "no-store"
    });
    if (r.ok) {
      const json = await r.json();
      if (isUsableRoutine(json)) {
        return { data: json, source: "structured" };
      }
    }
  } catch (e) {
    console.warn("Structured Firebase miss:", e.message);
  }

  return null;
}

/** Priority B6: section index for "Did you mean?" */
async function prefetchSectionIndex() {
  if (sectionIndexPromise) return sectionIndexPromise;
  sectionIndexPromise = (async () => {
    try {
      const r = await fetch(FIREBASE_INDEX_URL, { cache: "no-store" });
      if (!r.ok) return;
      const map = await r.json();
      if (map && typeof map === "object") {
        sectionIndex = Object.keys(map)
          .filter((k) => /^\d{2,3}_[A-Z0-9]+$/i.test(k))
          .map((k) => k.toUpperCase());
      }
    } catch (e) {
      console.warn("Section index prefetch failed:", e.message);
    }
  })();
  return sectionIndexPromise;
}

function suggestSections(input, limit = 5) {
  if (!sectionIndex || !sectionIndex.length) return [];
  const q = String(input || "").toUpperCase().replace(/-/g, "_");
  if (!q) return [];

  const scored = sectionIndex.map((sec) => {
    let score = 0;
    if (sec === q) score = 1000;
    else if (sec.startsWith(q)) score = 500 - (sec.length - q.length);
    else if (sec.includes(q)) score = 200;
    else {
      // same batch prefix e.g. 65_
      const batch = q.split("_")[0];
      if (batch && sec.startsWith(batch + "_")) score = 100;
      // letter distance on suffix
      const qa = q.split("_")[1] || "";
      const sa = sec.split("_")[1] || "";
      if (batch && sec.startsWith(batch + "_") && qa && sa) {
        if (sa.startsWith(qa) || qa.startsWith(sa)) score = 150;
        else if (Math.abs(sa.charCodeAt(0) - qa.charCodeAt(0)) === 1) score = 80;
      }
    }
    return { sec, score };
  });

  return scored
    .filter((x) => x.score > 0)
    .sort((a, b) => b.score - a.score || a.sec.localeCompare(b.sec))
    .slice(0, limit)
    .map((x) => x.sec);
}

function showSuggestions(section, opts = {}) {
  const suggestions = suggestSections(section);
  const el = $("status");
  el.classList.remove("hidden", "loading");
  el.classList.add("error");

  const chips = suggestions.length
    ? `<div class="suggest-row">Did you mean?
        ${suggestions
          .map(
            (s) =>
              `<button type="button" class="suggest-chip" data-section="${escapeAttr(s)}">${escapeHtml(s)}</button>`
          )
          .join("")}
      </div>`
    : `<div class="suggest-row">No similar sections found in the cache.</div>`;

  const liveBtn = opts.allowLive
    ? `<div class="suggest-row">
         <button type="button" class="suggest-chip suggest-live" id="forceLiveSearch">Search live on Notice Board</button>
       </div>`
    : "";

  el.innerHTML = `
    <div>Section <strong>${escapeHtml(section)}</strong> was not found in the cache.</div>
    ${chips}
    ${liveBtn}`;

  el.querySelectorAll(".suggest-chip[data-section]").forEach((btn) => {
    btn.addEventListener("click", () => {
      $("section").value = btn.getAttribute("data-section");
      autoAnalyze();
    });
  });

  const live = el.querySelector("#forceLiveSearch");
  if (live) {
    live.addEventListener("click", () => forceLiveSearch(section));
  }
}

/** Bypass cache and hit Render (cold start possible). */
async function forceLiveSearch(section) {
  if (busy) return;
  $("section").value = section;
  // Mark so autoAnalyze skips the early "not in index" return
  window.__forceLiveSearch = true;
  await autoAnalyze();
  window.__forceLiveSearch = false;
}

/** Priority B5: skeleton while loading */
function showSkeleton() {
  const box = $("skeleton");
  if (box) box.classList.remove("hidden");
  $("result").classList.add("hidden");
  $("preview").classList.add("hidden");
}

function hideSkeleton() {
  const box = $("skeleton");
  if (box) box.classList.add("hidden");
}

async function autoAnalyze() {
  if (busy) return;

  const section = $("section").value.trim().toUpperCase().replace(/-/g, "_");
  const examType = $("examType").value;
  const semester = $("semester").value;
  const year = $("academicYear").value.trim();

  if (!/^\d{2,3}_[A-Z0-9]+$/.test(section)) {
    return status("Enter a valid section such as 65_L or 65_N.", true);
  }
  if (!year || !/^20\d{2}$/.test(year)) {
    return status("Enter a valid academic year, for example 2026.", true);
  }

  try {
    localStorage.setItem("examroutine-section", section);
  } catch (_) {}

  setBusy(true);
  $("result").classList.add("hidden");
  $("preview").classList.add("hidden");
  $("sourceInfo").classList.add("hidden");
  detailsOpen = false;
  $("exams").hidden = true;
  $("exams").classList.add("collapsed");
  $("toggleDetails").textContent = "Show details";
  $("toggleDetails").setAttribute("aria-expanded", "false");

  const steps = [
    "Checking cache…",
    "Connecting to the DIU Notice Board…",
    "Finding the official CSE routine…",
    "Downloading and matching your section…",
    "Almost done — building your routine…"
  ];
  let step = 0;
  status(steps[0], false, true);
  const progress = setInterval(() => {
    step = Math.min(step + 1, steps.length - 1);
    status(steps[step], false, true);
  }, 4500);

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 120000);

  showSkeleton();

  try {
    // 1. Firebase instant load (legacy first, then routines_v2)
    try {
      await prefetchSectionIndex();
      const fbHit = await fetchFirebaseRoutine(section, examType, semester, year);

      if (fbHit && isUsableRoutine(fbHit.data)) {
        console.log("Loaded from Firebase:", fbHit.source);
        data = ensureSourceFromMeta(fbHit.data);
        clearInterval(progress);
        clearTimeout(timer);
        hideSkeleton();

        syncUrlFromForm();
        renderSource();
        renderResult();
        generateRoutine();
        startStatusClock();

        const fbData = fbHit.data;
        const seatMessage = fbData.seat_plan_available
          ? `${fbData.matched_seat_count}/${fbData.exam_count} seat allocations matched.`
          : "No matching seat plan was available, so the routine is shown without room/seat columns.";
        const scope = fbData.seat_plan_available ? fbData.section : `Batch ${fbData.batch}`;
        const via = fbHit.source === "structured" ? "structured cache" : "instant cache";
        status(`Done. Found ${fbData.exam_count} examination(s) for ${scope}. ${seatMessage} (${via})`);
        setBusy(false);
        $("preview").scrollIntoView({ behavior: "smooth", block: "start" });
        return;
      }

      // Not in Firebase — suggest similar sections immediately (don't wait for cold Render)
      await prefetchSectionIndex();
      const known = Array.isArray(sectionIndex) && sectionIndex.includes(section);
      if (!known && !window.__forceLiveSearch) {
        clearInterval(progress);
        clearTimeout(timer);
        hideSkeleton();
        showSuggestions(section, { allowLive: true });
        setBusy(false);
        return;
      }
    } catch (err) {
      console.warn("Firebase fetch missed or failed, falling back to Render:", err);
    }

    // 2. Render fallback (known section missing from cache, or force live)
    const q = new URLSearchParams({
      section,
      exam_type: examType,
      semester,
      year,
      include_seat_plan: "true"
    });

    const response = await fetch(`${API}/api/auto-analyze?${q.toString()}`, {
      method: "GET",
      signal: controller.signal,
      cache: "no-store"
    });

    const body = await response.json().catch(() => ({}));

    if (!response.ok) {
      if (
        response.status === 404 ||
        (body.detail && String(body.detail).includes("not found"))
      ) {
        throw new Error(
          "Section not found in the official routine. Please check your spelling and try again."
        );
      }
      let message = body.detail || `Server error (${response.status})`;
      if (Array.isArray(body.detail)) {
        message = body.detail.map((x) => x.msg || "Validation error").join(", ");
      }
      throw new Error(typeof message === "string" ? message : "Automatic lookup failed.");
    }

    if (JSON.stringify(body).includes("not found in the seat-plan PDF")) {
      throw new Error(
        `Section ${section} is invalid or not found in the official seat plan. Please check your spelling.`
      );
    }

    data = ensureSourceFromMeta(body);
    hideSkeleton();
    syncUrlFromForm();
    renderSource();
    renderResult();
    generateRoutine();
    startStatusClock();

    const seatMessage = body.seat_plan_available
      ? `${body.matched_seat_count}/${body.exam_count} seat allocations matched.`
      : "No matching seat plan was available, so the routine is shown without room/seat columns.";
    const scope = body.seat_plan_available ? body.section : `Batch ${body.batch}`;
    const cacheHint = body.cached ? " (cached — instant)" : "";
    status(`Done. Found ${body.exam_count} examination(s) for ${scope}. ${seatMessage}${cacheHint}`);
    $("preview").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) {
    console.error(e);
    hideSkeleton();
    const msg = String(e.message || "");
    if (
      msg.includes("not found") ||
      msg.includes("Section not found") ||
      msg.includes("invalid or not found")
    ) {
      await prefetchSectionIndex();
      showSuggestions(section);
    } else if (e.name === "AbortError") {
      status(
        "The lookup timed out. Please try again — the second attempt is usually much faster.",
        true
      );
    } else if (msg.includes("Failed to fetch") || msg.includes("NetworkError")) {
      status("Could not reach the server. Check your connection or try again in a moment.", true);
    } else {
      status(msg || "Automatic lookup failed.", true);
    }
  } finally {
    clearInterval(progress);
    clearTimeout(timer);
    hideSkeleton();
    setBusy(false);
  }
}

function renderSource() {
  const source = data?.source;
  const box = $("sourceInfo");
  if (!source) {
    // Still show a minimal note when cache has no source object
    if (data?.exams?.length) {
      box.innerHTML = `
        <div class="source-ok">
          <span class="source-dot">✓</span>
          <div>
            <strong>Routine loaded</strong>
            <small>Based on the official CSE examination schedule. Open the Notice Board to verify the latest PDF.</small>
          </div>
        </div>
        <div class="source-links">
          <a href="https://daffodilvarsity.edu.bd/noticeboard" target="_blank" rel="noopener">Open DIU Notice Board ↗</a>
        </div>
      `;
      box.classList.remove("hidden");
    } else {
      box.classList.add("hidden");
    }
    return;
  }

  box.innerHTML = `
    <div class="source-ok">
      <span class="source-dot">✓</span>
      <div>
        <strong>Collected automatically from DIU Notice Board</strong>
        <small>${escapeHtml(source.routine_title || "Official examination routine")}</small>
      </div>
    </div>
    <div class="source-links">
      ${
        source.routine_url
          ? `<a href="${escapeAttr(source.routine_url)}" target="_blank" rel="noopener">Open routine ↗</a>`
          : ""
      }
      ${
        source.seat_plan_url
          ? `<a href="${escapeAttr(source.seat_plan_url)}" target="_blank" rel="noopener">Open seat plan ↗</a>`
          : `<span>No matching seat plan was found.</span>`
      }
    </div>
  `;
  box.classList.remove("hidden");
}

function renderResult() {
  const exams = data?.exams || [];
  $("result").classList.remove("hidden");
  $("resultTitle").textContent = data.seat_plan_available
    ? `${data.section} — ${data.exam_count} examination(s)`
    : `Batch ${data.batch} — ${data.exam_count} examination(s)`;

  if (data.seat_plan_available) {
    $("match").textContent = `${data.matched_seat_count}/${data.exam_count} seat matches`;
  } else {
    $("match").textContent = "Routine only";
  }

  $("warnings").innerHTML = (data.warnings || [])
    .map((w) => `<div class="warning">${escapeHtml(w)}</div>`)
    .join("");

  $("exams").innerHTML = exams
    .map((x) => {
      const rooms = x.rooms?.length
        ? x.rooms
            .map(
              (r) => `
          <div class="roomline">
            <span>Room ${escapeHtml(r.room)}</span>
            <strong>${Number(r.seats) || 0} seats</strong>
          </div>`
            )
            .join("")
        : `<span class="muted">${
            data.seat_plan_available ? "No matched seat allocation" : "Seat plan not available"
          }</span>`;

      return `
      <article class="exam">
        <div class="exam-date-block">
          <div class="date">${formatDate(x.date)}</div>
          <div class="code">${escapeHtml(x.day)} · Slot ${escapeHtml(x.slot || "—")}</div>
        </div>
        <div class="exam-course">
          <label>Course</label>
          <strong>${escapeHtml(x.course_code)}</strong>
          <span>${escapeHtml(x.course_name)}</span>
        </div>
        <div class="exam-time">
          <label>Exam time</label>
          <strong>${escapeHtml(x.time || "—")}</strong>
        </div>
        ${data.seat_plan_available ? `<div class="rooms"><label>Rooms & seats</label>${rooms}</div>` : ""}
      </article>
    `;
    })
    .join("");
}

function generateRoutine() {
  if (!data?.exams?.length) return;

  const exams = [...data.exams].sort((a, b) => {
    const date = String(a.date).localeCompare(String(b.date));
    return date || String(a.slot).localeCompare(String(b.slot));
  });
  data.exams = exams;

  const session = [data.semester, data.year].filter(Boolean).join(" ") || "EXAMINATION";
  const hasSeats = Boolean(data.seat_plan_available && data.matched_seat_count > 0);
  const batch = getBatch(data.section, exams);
  const identity = hasSeats ? escapeHtml(data.section) : `BATCH ${escapeHtml(batch)}`;
  const examType = String(data.exam_type || "final").toUpperCase();
  const title = `${examType} EXAMINATION ROUTINE`;
  const nextIdx = findNextExamIndex(exams);

  $("routineOutput").innerHTML = buildRoutine(exams, session, hasSeats, identity, title, batch, nextIdx);
  $("exportStage").innerHTML = buildExport(exams, session, hasSeats, identity, title, batch);
  $("preview").classList.remove("hidden");
}

function findNextExamIndex(exams) {
  for (let i = 0; i < exams.length; i++) {
    if (!isExamFinished(exams[i])) return i;
  }
  return -1;
}

function parseExamDate(value) {
  if (!value) return null;
  const d = new Date(`${value}T00:00:00`);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** Parse strings like "09:00 AM - 11:00 AM" or "9:00AM-11:00AM". Returns {start, end} in minutes from midnight, or null. */
function parseTimeRange(timeStr) {
  if (!timeStr) return null;
  const text = String(timeStr).trim();
  const parts = text.split(/\s*[-–—]\s*/);
  const parseOne = (chunk) => {
    const m = String(chunk)
      .trim()
      .match(/^(\d{1,2}):(\d{2})\s*(AM|PM)?$/i);
    if (!m) return null;
    let h = Number(m[1]);
    const min = Number(m[2]);
    const ap = (m[3] || "").toUpperCase();
    if (ap === "PM" && h < 12) h += 12;
    if (ap === "AM" && h === 12) h = 0;
    if (!ap && h > 23) return null;
    return h * 60 + min;
  };
  if (parts.length >= 2) {
    const start = parseOne(parts[0]);
    const end = parseOne(parts[1]);
    if (start == null || end == null) return null;
    return { start, end };
  }
  const only = parseOne(parts[0]);
  if (only == null) return null;
  // Single time → assume 2-hour exam
  return { start: only, end: only + 120 };
}

/** End Date of an exam (date + end time). Falls back to end of that calendar day. */
function getExamEndDate(exam) {
  const base = parseExamDate(exam?.date);
  if (!base) return null;
  const range = parseTimeRange(exam?.time);
  const end = new Date(base);
  if (range) {
    end.setHours(Math.floor(range.end / 60), range.end % 60, 0, 0);
  } else {
    end.setHours(23, 59, 59, 999);
  }
  return end;
}

/** Start Date of an exam (date + start time). */
function getExamStartDate(exam) {
  const base = parseExamDate(exam?.date);
  if (!base) return null;
  const range = parseTimeRange(exam?.time);
  const start = new Date(base);
  if (range) {
    start.setHours(Math.floor(range.start / 60), range.start % 60, 0, 0);
  } else {
    start.setHours(0, 0, 0, 0);
  }
  return start;
}

function isExamFinished(exam) {
  const end = getExamEndDate(exam);
  if (!end) return false;
  return Date.now() > end.getTime();
}

function isExamOngoing(exam) {
  const start = getExamStartDate(exam);
  const end = getExamEndDate(exam);
  if (!start || !end) return false;
  const now = Date.now();
  return now >= start.getTime() && now <= end.getTime();
}

function daysUntilDate(value) {
  const d = parseExamDate(value);
  if (!d) return null;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  return Math.round((d - today) / 86400000);
}

function relativeLabel(exam) {
  if (!exam) return "";
  if (isExamFinished(exam)) return "Done";
  if (isExamOngoing(exam)) return "Ongoing";
  const n = daysUntilDate(exam.date);
  if (n === null) return "";
  if (n === 0) return "Today";
  if (n === 1) return "Tomorrow";
  if (n < 0) return "Done";
  return `In ${n} days`;
}

function formatDateParts(value) {
  if (!value) return { day: "—", month: "", year: "" };
  const d = parseExamDate(value);
  if (!d) return { day: escapeHtml(value), month: "", year: "" };
  return {
    day: String(d.getDate()).padStart(2, "0"),
    month: d.toLocaleDateString("en-GB", { month: "short" }),
    year: String(d.getFullYear())
  };
}

function calendarIcon() {
  return `<svg class="calendar-svg" viewBox="0 0 48 48" aria-hidden="true">
    <rect x="8" y="10" width="32" height="30" rx="4" fill="none" stroke="currentColor" stroke-width="4"/>
    <path d="M8 18h32M15 6v9M33 6v9" fill="none" stroke="currentColor" stroke-width="4" stroke-linecap="round"/>
    <path d="M15 25h3M22.5 25h3M30 25h3M15 32h3M22.5 32h3M30 32h3" stroke="currentColor" stroke-width="3" stroke-linecap="round"/>
  </svg>`;
}

function buildNextBanner(exams, nextIdx) {
  if (nextIdx < 0) {
    return `<div class="next-banner next-done">All listed examinations are in the past.</div>`;
  }
  const x = exams[nextIdx];
  const rel = relativeLabel(x);
  const rooms = x.rooms?.length
    ? x.rooms.map((r) => `R${escapeHtml(r.room)}`).join(", ")
    : "";
  return `
    <div class="next-banner">
      <div class="next-left">
        <span class="next-tag ${rel === "Ongoing" ? "ongoing-tag" : ""}">${escapeHtml(rel)}</span>
        <div>
          <strong>Next: ${escapeHtml(x.course_code)}</strong>
          <span class="next-sub">${escapeHtml(x.course_name)}</span>
        </div>
      </div>
      <div class="next-right">
        <span>${formatDate(x.date)} · ${escapeHtml(x.day)}</span>
        <span>${escapeHtml(x.time || "—")}${rooms ? ` · ${rooms}` : ""}</span>
      </div>
    </div>`;
}

function buildMobileCards(exams, hasSeats, nextIdx) {
  return exams
    .map((x, i) => {
      const rooms = x.rooms?.length
        ? x.rooms
            .map(
              (r) =>
                `<span class="room-chip">R${escapeHtml(r.room)} <em>${Number(r.seats) || 0}</em></span>`
            )
            .join("")
        : "—";
      const total = getExamTotal(x);
      const rel = relativeLabel(x);
      const isNext = i === nextIdx;
      const isPast = isExamFinished(x);
      const isOngoing = isExamOngoing(x);
      return `
      <article class="mobile-exam ${isNext ? "is-next" : ""} ${isPast ? "is-past" : ""} ${isOngoing ? "is-ongoing" : ""}">
        <div class="me-top">
          <div class="me-date">${formatDate(x.date)}</div>
          <div class="me-badges">
            ${isNext ? `<span class="badge-next">Next</span>` : ""}
            ${rel ? `<span class="badge-rel">${escapeHtml(rel)}</span>` : ""}
            <span class="me-day">${escapeHtml(x.day)}</span>
          </div>
        </div>
        <div class="me-course"><span class="code-pill">${escapeHtml(x.course_code)}</span></div>
        <div class="me-name">${escapeHtml(x.course_name)}</div>
        <div class="me-meta">
          <div><span>TIME</span><strong>${escapeHtml(x.time || "—")}</strong></div>
          ${
            hasSeats
              ? `<div><span>ROOMS</span><div class="chip-row">${rooms}</div></div>
                 <div><span>TOTAL</span><strong>${total}</strong></div>`
              : ""
          }
        </div>
      </article>`;
    })
    .join("");
}

function buildRoutine(exams, session, hasSeats, identity, title, batch, nextIdx) {
  const seatHeaders = hasSeats
    ? `<th>ROOM</th><th>SEATS</th><th>TOTAL</th>`
    : "";

  return `
    <div class="routine-screen">
      <div class="routine-head">
        <div class="export-title"><span>${identity}</span> ${escapeHtml(title)}</div>
        <div class="session-line"><i></i><strong>${escapeHtml(session.replace("-", "–"))}</strong><i></i></div>
      </div>

      ${buildNextBanner(exams, nextIdx)}

      <div class="meta routine-meta">
        ${
          hasSeats
            ? `<div><span>SECTION</span><strong>${identity}</strong></div>`
            : `<div><span>BATCH</span><strong>${escapeHtml(batch)}</strong></div>`
        }
        <div><span>EXAMINATIONS</span><strong>${exams.length}</strong></div>
        ${hasSeats ? `<div><span>BATCH</span><strong>${escapeHtml(batch)}</strong></div>` : ""}
      </div>

      <div class="routine-table-wrap">
        <table class="routine-table ${hasSeats ? "with-seats" : "without-seats"}">
          <thead><tr>
            <th>DATE</th><th>DAY</th><th>COURSE</th><th>TIME</th>${seatHeaders}
          </tr></thead>
          <tbody>${exams.map((x, i) => screenRow(x, i, hasSeats, nextIdx)).join("")}</tbody>
        </table>
      </div>

      <div class="mobile-exam-list">
        ${buildMobileCards(exams, hasSeats, nextIdx)}
      </div>

      <div class="note">
        <b>NOTE:</b> This schedule is based on the official routine published by the Examination Committee, FSIT. Always verify against the official PDF.
      </div>
    </div>
  `;
}

function buildExport(exams, session, hasSeats, identity, title, batch) {
  const height = Math.max(980, 430 + exams.length * 145);
  const seatHeaders = hasSeats
    ? `<th>ROOM</th><th>SEATS / ROOM</th><th>TOTAL</th>`
    : "";

  return `
    <div class="export-routine ${hasSeats ? "export-with-seats" : "export-without-seats"}" style="height:${height}px">
      <div class="export-top">
        <div class="export-title"><span>${identity}</span> ${escapeHtml(title)}</div>
      </div>

      <div class="export-session"><i></i><strong>${escapeHtml(session.replace("-", "–"))}</strong><i></i></div>

      <div class="export-meta">
        ${
          hasSeats
            ? `<div class="export-meta-item"><div><span>SECTION</span><strong>${identity}</strong></div></div>`
            : `<div class="export-meta-item"><div><span>BATCH</span><strong>${escapeHtml(batch)}</strong></div></div>`
        }
        <div class="export-meta-item"><div><span>EXAMINATIONS</span><strong>${exams.length}</strong></div></div>
        ${
          hasSeats
            ? `<div class="export-meta-item"><div><span>BATCH</span><strong>${escapeHtml(batch)}</strong></div></div>`
            : ""
        }
      </div>

      <table class="export-table ${hasSeats ? "with-seats" : "without-seats"}">
        <thead><tr>
          <th>DATE</th><th>DAY</th><th>COURSE</th><th>TIME</th>${seatHeaders}
        </tr></thead>
        <tbody>${exams.map((x, i) => exportRow(x, i, hasSeats)).join("")}</tbody>
      </table>

      <div class="export-note">
        <span>NOTE:</span> This schedule is based on the official routine published by the Examination Committee, FSIT.
      </div>
    </div>
  `;
}

function screenRow(x, i, hasSeats, nextIdx) {
  const parts = formatDateParts(x.date);
  const isNext = i === nextIdx;
  const isPast = isExamFinished(x);
  const isOngoing = isExamOngoing(x);
  const rel = relativeLabel(x);

  const rooms = x.rooms?.length
    ? x.rooms.map((r) => `<div class="room-no">${escapeHtml(r.room)}</div>`).join("")
    : "—";
  const seats = x.rooms?.length
    ? x.rooms.map((r) => `<div class="seat-no">${Number(r.seats) || 0}</div>`).join("")
    : "—";
  const total = getExamTotal(x);

  return `
    <tr class="screen-row-${i % 4} ${isNext ? "row-next" : ""} ${isPast ? "row-past" : ""} ${isOngoing ? "row-ongoing" : ""}">
      <td class="date-cell">
        <div class="date-stack">
          <div class="date-main">
            <span class="date-icon">${calendarIcon()}</span>
            <div class="date-text">
              <strong>${parts.day} ${parts.month}</strong>
              <span class="date-year">${parts.year}</span>
            </div>
          </div>
          ${rel ? `<span class="date-rel ${isNext ? "is-next" : ""} ${isPast ? "is-past" : ""} ${isOngoing ? "is-ongoing" : ""}">${escapeHtml(rel)}</span>` : ""}
        </div>
      </td>
      <td class="center day-cell">${escapeHtml(x.day)}</td>
      <td class="course-cell">
        <span class="code-pill">${escapeHtml(x.course_code)}</span>
        <span class="course-name">${escapeHtml(x.course_name)}</span>
      </td>
      <td class="center time-cell">${escapeHtml(x.time || "—")}</td>
      ${
        hasSeats
          ? `<td class="rooms-cell">${rooms}</td>
             <td class="center seats-cell">${seats}</td>
             <td class="center total-cell"><strong>${total}</strong></td>`
          : ""
      }
    </tr>
  `;
}

function exportRow(x, i, hasSeats) {
  const rooms = x.rooms?.length
    ? x.rooms.map((r) => `<div>${escapeHtml(r.room)}</div>`).join("")
    : "—";
  const counts = x.rooms?.length
    ? x.rooms.map((r) => `<div>${Number(r.seats) || 0}</div>`).join("")
    : "—";
  const total = getExamTotal(x);

  return `
    <tr class="export-row-${i % 4}">
      <td class="export-date"><div class="export-date-flex"><span class="export-calendar">${calendarIcon()}</span><strong>${formatDate(x.date)}</strong></div></td>
      <td class="export-center">${escapeHtml(x.day)}</td>
      <td class="export-course"><b>${escapeHtml(x.course_code)}</b><span> — ${escapeHtml(x.course_name)}</span></td>
      <td class="export-center export-time">${escapeHtml(x.time || "—")}</td>
      ${
        hasSeats
          ? `<td class="export-rooms">${rooms}</td><td class="export-seats">${counts}</td><td class="export-total">${total}</td>`
          : ""
      }
    </tr>
  `;
}

function getExamTotal(x) {
  const explicit = Number(x.total_students);
  if (Number.isFinite(explicit) && explicit > 0) return explicit;
  if (x.rooms?.length) return x.rooms.reduce((sum, r) => sum + (Number(r.seats) || 0), 0);
  return "—";
}

function getBatch(section, exams) {
  const m = String(section || "").match(/^(\d+)/);
  if (m) return m[1];
  const fromExam = exams.map((x) => String(x.batch || "").match(/\d+/)).find(Boolean);
  return fromExam ? fromExam[0] : "—";
}

async function makeCanvas() {
  if (!data) throw new Error("Generate the routine first.");
  if (!window.html2canvas) throw new Error("PNG/PDF library failed to load. Refresh the page.");

  const target = $("exportStage").firstElementChild;
  if (!target) throw new Error("Routine export is not ready.");

  await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  return window.html2canvas(target, {
    width: target.offsetWidth,
    height: target.offsetHeight,
    scale: 2,
    useCORS: true,
    backgroundColor: "#ffffff",
    logging: false
  });
}

function buildShareUrl() {
  const section = ($("section").value || data?.section || "")
    .trim()
    .toUpperCase()
    .replace(/-/g, "_");
  const params = new URLSearchParams();
  if (/^\d{2,3}_[A-Z0-9]+$/.test(section)) params.set("section", section);
  params.set("semester", $("semester").value || String(data?.semester || "summer").toLowerCase());
  params.set("year", $("academicYear").value || String(data?.year || "2026"));
  params.set(
    "exam",
    $("examType").value || String(data?.exam_type || "final").toLowerCase()
  );
  return `${location.origin}${location.pathname}?${params.toString()}`;
}

async function copyShareLink() {
  if (!data?.exams?.length) {
    return status("Generate your routine first, then copy the link.", true);
  }
  syncUrlFromForm();
  const url = buildShareUrl();
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(url);
    } else {
      const ta = document.createElement("textarea");
      ta.value = url;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.left = "-9999px";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    }
    status("Link copied — share it with your classmates.");
    flashShareButtons("✓ Copied");
  } catch (e) {
    console.error(e);
    status(`Could not copy automatically. Link: ${url}`, true);
  }
}

function flashShareButtons(label) {
  for (const id of ["shareLink", "shareLinkMobile"]) {
    const btn = $(id);
    if (!btn) continue;
    const prev = btn.textContent;
    btn.textContent = label;
    btn.classList.add("share-copied");
    setTimeout(() => {
      btn.textContent = prev;
      btn.classList.remove("share-copied");
    }, 1800);
  }
}

async function downloadPNG() {
  if (!data) return status("Generate your routine first.", true);
  try {
    status("Preparing high-quality PNG…", false, true);
    const canvas = await makeCanvas();
    const a = document.createElement("a");
    const fileScope = data.seat_plan_available ? data.section : `batch_${data.batch}`;
    a.download = `${safe(fileScope)}_${String(data.exam_type || "exam").toLowerCase()}_routine.png`;
    a.href = canvas.toDataURL("image/png", 1);
    a.click();
    status("PNG downloaded successfully.");
  } catch (e) {
    console.error(e);
    status(`Could not create PNG: ${e.message}`, true);
  }
}

async function downloadPDF() {
  if (!data) return status("Generate your routine first.", true);
  try {
    status("Preparing high-quality PDF…", false, true);
    if (!window.jspdf?.jsPDF) throw new Error("PDF library failed to load. Refresh the page.");

    const canvas = await makeCanvas();
    const pageWidth = 297;
    const pageHeight = (canvas.height / canvas.width) * pageWidth;
    const pdf = new window.jspdf.jsPDF({
      orientation: "landscape",
      unit: "mm",
      format: [pageWidth, pageHeight],
      compress: true
    });
    pdf.addImage(
      canvas.toDataURL("image/png", 1),
      "PNG",
      0,
      0,
      pageWidth,
      pageHeight,
      undefined,
      "FAST"
    );
    const fileScope = data.seat_plan_available ? data.section : `batch_${data.batch}`;
    pdf.save(`${safe(fileScope)}_${String(data.exam_type || "exam").toLowerCase()}_routine.pdf`);
    status("PDF downloaded successfully.");
  } catch (e) {
    console.error(e);
    status(`Could not create PDF: ${e.message}`, true);
  }
}

function safe(value) {
  return String(value || "exam").replace(/[^a-z0-9_-]+/gi, "_");
}

function formatDate(value) {
  if (!value) return "—";
  const d = new Date(`${value}T00:00:00`);
  if (Number.isNaN(d.getTime())) return escapeHtml(value);
  return d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
}

function escapeHtml(value) {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#039;"
      })[c]
  );
}

function escapeAttr(value) {
  return escapeHtml(value);
}
