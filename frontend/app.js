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
const THEME_KEY = "examroutine-theme";

const $ = (id) => document.getElementById(id);
let data = null;
let busy = false;
let detailsOpen = false;

initTheme();
bindEvents();
checkBackend();

function bindEvents() {
  $("routineForm").addEventListener("submit", (e) => {
    e.preventDefault();
    autoAnalyze();
  });
  $("png").addEventListener("click", downloadPNG);
  $("pdf").addEventListener("click", downloadPDF);
  $("pngMobile").addEventListener("click", downloadPNG);
  $("pdfMobile").addEventListener("click", downloadPDF);
  $("toggleDetails").addEventListener("click", toggleDetails);
  $("themeToggle").addEventListener("click", toggleTheme);

  // Restore last section if available
  try {
    const saved = localStorage.getItem("examroutine-section");
    if (saved && /^\d{2,3}_[A-Z0-9]+$/i.test(saved)) {
      $("section").value = saved;
    }
  } catch (_) {}
}

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

  try {
    // 1. Firebase instant load
    try {
      const fbResponse = await fetch(`${FIREBASE_BASE_URL}/${section}.json`, {
        cache: "no-store"
      });
      const fbData = await fbResponse.json();

      if (fbData && matchesFormFilters(fbData, examType, semester, year)) {
        console.log("Loaded from Firebase cache");
        data = fbData;
        clearInterval(progress);
        clearTimeout(timer);

        renderSource();
        renderResult();
        generateRoutine();

        const seatMessage = fbData.seat_plan_available
          ? `${fbData.matched_seat_count}/${fbData.exam_count} seat allocations matched.`
          : "No matching seat plan was available, so the routine is shown without room/seat columns.";
        const scope = fbData.seat_plan_available ? fbData.section : `Batch ${fbData.batch}`;
        status(`Done. Found ${fbData.exam_count} examination(s) for ${scope}. ${seatMessage} (instant cache)`);
        setBusy(false);
        $("preview").scrollIntoView({ behavior: "smooth", block: "start" });
        return;
      }

      if (fbData && fbData.exams && fbData.exams.length) {
        console.log("Firebase had data but filters did not match; falling back to live lookup");
      }
    } catch (err) {
      console.warn("Firebase fetch missed or failed, falling back to Render:", err);
    }

    // 2. Render fallback
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

    data = body;
    renderSource();
    renderResult();
    generateRoutine();

    const seatMessage = body.seat_plan_available
      ? `${body.matched_seat_count}/${body.exam_count} seat allocations matched.`
      : "No matching seat plan was available, so the routine is shown without room/seat columns.";
    const scope = body.seat_plan_available ? body.section : `Batch ${body.batch}`;
    const cacheHint = body.cached ? " (cached — instant)" : "";
    status(`Done. Found ${body.exam_count} examination(s) for ${scope}. ${seatMessage}${cacheHint}`);
    $("preview").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) {
    console.error(e);
    let message;
    if (e.name === "AbortError") {
      message =
        "The lookup timed out. Please try again — the second attempt is usually much faster.";
    } else if (
      String(e.message || "").includes("Failed to fetch") ||
      String(e.message || "").includes("NetworkError")
    ) {
      message = "Could not reach the server. Check your connection or try again in a moment.";
    } else {
      message = e.message || "Automatic lookup failed.";
    }
    status(message, true);
  } finally {
    clearInterval(progress);
    clearTimeout(timer);
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
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  for (let i = 0; i < exams.length; i++) {
    const d = parseExamDate(exams[i].date);
    if (d && d >= today) return i;
  }
  return -1;
}

function parseExamDate(value) {
  if (!value) return null;
  const d = new Date(`${value}T00:00:00`);
  return Number.isNaN(d.getTime()) ? null : d;
}

function daysUntil(value) {
  const d = parseExamDate(value);
  if (!d) return null;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  return Math.round((d - today) / 86400000);
}

function relativeLabel(value) {
  const n = daysUntil(value);
  if (n === null) return "";
  if (n < 0) return "Done";
  if (n === 0) return "Today";
  if (n === 1) return "Tomorrow";
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
  const rel = relativeLabel(x.date);
  const rooms = x.rooms?.length
    ? x.rooms.map((r) => `R${escapeHtml(r.room)}`).join(", ")
    : "";
  return `
    <div class="next-banner">
      <div class="next-left">
        <span class="next-tag">${escapeHtml(rel)}</span>
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
      const rel = relativeLabel(x.date);
      const isNext = i === nextIdx;
      const isPast = (daysUntil(x.date) ?? 0) < 0;
      return `
      <article class="mobile-exam ${isNext ? "is-next" : ""} ${isPast ? "is-past" : ""}">
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
  const isPast = (daysUntil(x.date) ?? 0) < 0;
  const rel = relativeLabel(x.date);

  const rooms = x.rooms?.length
    ? x.rooms.map((r) => `<div class="room-no">${escapeHtml(r.room)}</div>`).join("")
    : "—";
  const seats = x.rooms?.length
    ? x.rooms.map((r) => `<div class="seat-no">${Number(r.seats) || 0}</div>`).join("")
    : "—";
  const total = getExamTotal(x);

  return `
    <tr class="screen-row-${i % 4} ${isNext ? "row-next" : ""} ${isPast ? "row-past" : ""}">
      <td class="date-cell">
        <div class="date-stack">
          <div class="date-main">
            <span class="date-icon">${calendarIcon()}</span>
            <div class="date-text">
              <strong>${parts.day} ${parts.month}</strong>
              <span class="date-year">${parts.year}</span>
            </div>
          </div>
          ${rel ? `<span class="date-rel ${isNext ? "is-next" : ""} ${isPast ? "is-past" : ""}">${escapeHtml(rel)}</span>` : ""}
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
