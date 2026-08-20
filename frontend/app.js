// API base URL resolution order:
// 1. ?api=... query parameter (useful for testing)
// 2. window.EXAM_API (set via a small config script if needed)
// 3. localhost when developing
// 4. Production Render backend
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
const $ = id => document.getElementById(id);
let data = null;
let busy = false;

$("autoAnalyze").addEventListener("click", autoAnalyze);
$("png").addEventListener("click", downloadPNG);
$("pdf").addEventListener("click", downloadPDF);
$("pngMobile").addEventListener("click", downloadPNG);
$("pdfMobile").addEventListener("click", downloadPDF);

async function checkBackend() {
  try {
    const r = await fetch(`${API}/api/health`, { cache: "no-store" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    console.log("ExamRoutine backend:", await r.json());
  } catch (e) {
    console.warn("Backend unavailable:", e.message);
  }
}
checkBackend();

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

  setBusy(true);
  $("result").classList.add("hidden");
  $("preview").classList.add("hidden");
  $("sourceInfo").classList.add("hidden");

  const steps = [
    "Connecting to the DIU Notice Board…",
    "Finding the official CSE routine…",
    "Downloading and checking the routine…",
    "Matching your section with the seat plan…",
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
    // --- 100% FIREBASE-DRIVEN LOGIC ---
    const fbResponse = await fetch(`${FIREBASE_BASE_URL}/${section}.json`);
    const fbData = await fbResponse.json();

    // Check if data exists AND matches the selected semester and year
    if (fbData && fbData.exams && fbData.exams.length > 0 && 
        fbData.semester.toLowerCase() === semester.toLowerCase() && 
        fbData.year == year) {
        
      console.log("Loaded instantly from Firebase!");
      data = fbData;
      
      // Stop the loading text immediately
      clearInterval(progress);

      renderSource();
      renderResult();
      generateRoutine();

      const seatMessage = fbData.seat_plan_available
        ? `${fbData.matched_seat_count}/${fbData.exam_count} seat allocations matched.`
        : "No matching seat plan was available, so the routine is shown without room/seat columns.";

      const scope = fbData.seat_plan_available ? fbData.section : `Batch ${fbData.batch}`;
      
      status(`Done. Found ${fbData.exam_count} examination(s) for ${scope}. ${seatMessage} (Firebase Cache - Instant)`);
      
    } else {
      // If it's missing or from an old semester, throw an error immediately!
      throw new Error("Section not found in the current official routine. Please check your spelling and try again.");
    }

  } catch (e) {
    console.error(e);
    let message;
    if (e.name === "AbortError") {
      message = "The lookup timed out. Check your connection.";
    } else if (String(e.message || "").includes("Failed to fetch") || String(e.message || "").includes("NetworkError")) {
      message = "Could not reach the database. Check your connection.";
    } else {
      message = e.message || "Automatic lookup failed.";
    }
    status(message, true);
  } finally {
    clearInterval(progress);
    clearTimeout(timer);
    setBusy(false);
  }
function renderSource() {
  const source = data?.source;
  const box = $("sourceInfo");
  if (!source) {
    box.classList.add("hidden");
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
      ${source.routine_url ? `<a href="${escapeAttr(source.routine_url)}" target="_blank" rel="noopener">Open routine ↗</a>` : ""}
      ${source.seat_plan_url
        ? `<a href="${escapeAttr(source.seat_plan_url)}" target="_blank" rel="noopener">Open seat plan ↗</a>`
        : `<span>No matching seat plan was found.</span>`}
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
    .map(w => `<div class="warning">${escapeHtml(w)}</div>`)
    .join("");

  $("exams").innerHTML = exams.map(x => {
    const rooms = x.rooms?.length
      ? x.rooms.map(r => `
          <div class="roomline">
            <span>Room ${escapeHtml(r.room)}</span>
            <strong>${Number(r.seats) || 0} seats</strong>
          </div>`).join("")
      : `<span class="muted">${data.seat_plan_available ? "No matched seat allocation" : "Seat plan not available"}</span>`;

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
  }).join("");
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

  $("routineOutput").innerHTML = buildRoutine(exams, session, hasSeats, identity, title, batch);
  $("exportStage").innerHTML = buildExport(exams, session, hasSeats, identity, title, batch);
  $("preview").classList.remove("hidden");
}

function calendarIcon() {
  return `<svg class="calendar-svg" viewBox="0 0 48 48" aria-hidden="true">
    <rect x="8" y="10" width="32" height="30" rx="4" fill="none" stroke="currentColor" stroke-width="4"/>
    <path d="M8 18h32M15 6v9M33 6v9" fill="none" stroke="currentColor" stroke-width="4" stroke-linecap="round"/>
    <path d="M15 25h3M22.5 25h3M30 25h3M15 32h3M22.5 32h3M30 32h3" stroke="currentColor" stroke-width="3" stroke-linecap="round"/>
  </svg>`;
}

function buildRoutine(exams, session, hasSeats, identity, title, batch) {
  const seatHeaders = hasSeats
    ? `<th>ROOM</th><th>SEATS / ROOM</th><th>TOTAL</th>`
    : "";

  return `
    <div class="routine-screen">
      <div class="routine-head">
        <div class="export-title"><span>${identity}</span> ${escapeHtml(title)}</div>
        <div class="session-line"><i></i><strong>${escapeHtml(session.replace("-", "–"))}</strong><i></i></div>
      </div>

      <div class="meta routine-meta">
        ${hasSeats
          ? `<div><span>SECTION</span><strong>${identity}</strong></div>`
          : `<div><span>BATCH</span><strong>${escapeHtml(batch)}</strong></div>`}
        <div><span>EXAMINATIONS</span><strong>${exams.length}</strong></div>
        ${hasSeats ? `<div><span>BATCH</span><strong>${escapeHtml(batch)}</strong></div>` : ""}
      </div>

      <div class="routine-table-wrap">
        <table class="routine-table ${hasSeats ? "with-seats" : "without-seats"}">
          <thead><tr>
            <th>DATE</th><th>DAY</th><th>COURSE</th><th>TIME</th>${seatHeaders}
          </tr></thead>
          <tbody>${exams.map((x, i) => screenRow(x, i, hasSeats)).join("")}</tbody>
        </table>
      </div>

      <div class="note">
        <b>NOTE:</b> This schedule is based on the official routine published by the Examination Committee, FSIT.
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
        ${hasSeats
          ? `<div class="export-meta-item"><div><span>SECTION</span><strong>${identity}</strong></div></div>`
          : `<div class="export-meta-item"><div><span>BATCH</span><strong>${escapeHtml(batch)}</strong></div></div>`}
        <div class="export-meta-item"><div><span>EXAMINATIONS</span><strong>${exams.length}</strong></div></div>
        ${hasSeats ? `<div class="export-meta-item"><div><span>BATCH</span><strong>${escapeHtml(batch)}</strong></div></div>` : ""}
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

function screenRow(x, i, hasSeats) {
  const seats = x.rooms?.length ? x.rooms.map(r => `<div>${escapeHtml(r.room)}</div>`).join("") : "—";
  const seatCounts = x.rooms?.length ? x.rooms.map(r => `<div>${Number(r.seats) || 0}</div>`).join("") : "—";
  const total = getExamTotal(x);

  return `
    <tr class="screen-row-${i % 4}">
      <td class="date-cell"><span class="date-icon">${calendarIcon()}</span><strong>${formatDate(x.date)}</strong></td>
      <td class="center">${escapeHtml(x.day)}</td>
      <td class="course-cell"><b>${escapeHtml(x.course_code)}</b><span class="course-name"> — ${escapeHtml(x.course_name)}</span></td>
      <td class="center time-cell">${escapeHtml(x.time || "—")}</td>
      ${hasSeats ? `<td class="rooms-cell">${seats}</td><td class="center seats-cell">${seatCounts}</td><td class="center total-cell"><strong>${total}</strong></td>` : ""}
    </tr>
  `;
}

function exportRow(x, i, hasSeats) {
  const rooms = x.rooms?.length ? x.rooms.map(r => `<div>${escapeHtml(r.room)}</div>`).join("") : "—";
  const counts = x.rooms?.length ? x.rooms.map(r => `<div>${Number(r.seats) || 0}</div>`).join("") : "—";
  const total = getExamTotal(x);

  return `
    <tr class="export-row-${i % 4}">
      <td class="export-date"><div class="export-date-flex"><span class="export-calendar">${calendarIcon()}</span><strong>${formatDate(x.date)}</strong></div></td>
      <td class="export-center">${escapeHtml(x.day)}</td>
      <td class="export-course"><b>${escapeHtml(x.course_code)}</b><span> — ${escapeHtml(x.course_name)}</span></td>
      <td class="export-center export-time">${escapeHtml(x.time || "—")}</td>
      ${hasSeats ? `<td class="export-rooms">${rooms}</td><td class="export-seats">${counts}</td><td class="export-total">${total}</td>` : ""}
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
  const fromExam = exams.map(x => String(x.batch || "").match(/\d+/)).find(Boolean);
  return fromExam ? fromExam[0] : "—";
}

async function makeCanvas() {
  if (!data) throw new Error("Generate the routine first.");
  if (!window.html2canvas) throw new Error("PNG/PDF library failed to load. Refresh the page.");

  const target = $("exportStage").firstElementChild;
  if (!target) throw new Error("Routine export is not ready.");

  await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
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
    const pageHeight = canvas.height / canvas.width * pageWidth;
    const pdf = new window.jspdf.jsPDF({
      orientation: "landscape",
      unit: "mm",
      format: [pageWidth, pageHeight],
      compress: true
    });
    pdf.addImage(canvas.toDataURL("image/png", 1), "PNG", 0, 0, pageWidth, pageHeight, undefined, "FAST");
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
  return String(value ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
  }[c]));
}

function escapeAttr(value) {
  return escapeHtml(value);
}
