const API = "https://examroutine.onrender.com";
const $ = id => document.getElementById(id);
let data = null;
let busy = false;

$("routine").addEventListener("change", e => {
  const file = e.target.files[0];
  $("routineName").textContent = file ? file.name : "Required";
});
$("seatPlan").addEventListener("change", e => {
  const file = e.target.files[0];
  $("seatName").textContent = file ? file.name : "Optional — adds rooms & seats";
});
$("analyze").onclick = analyze;
$("generate").onclick = generateRoutine;
$("png").onclick = downloadPNG;
$("pdf").onclick = downloadPDF;
$("pngMobile").onclick = downloadPNG;
$("pdfMobile").onclick = downloadPDF;

function status(message, error=false, loading=false) {
  const el = $("status");
  el.textContent = message;
  el.classList.remove("hidden","error","loading");
  if(error) el.classList.add("error");
  if(loading) el.classList.add("loading");
}

async function analyze() {
  if (busy) return;
  const routine = $("routine").files[0];
  const seatPlan = $("seatPlan").files[0];
  const section = $("section").value.trim();
  if(!routine) return status("Please upload the exam routine PDF.", true);
  if(!routine.name.toLowerCase().endsWith(".pdf")) return status("The exam routine must be a PDF file.", true);
  if(seatPlan && !seatPlan.name.toLowerCase().endsWith(".pdf")) return status("The seat plan must be a PDF file.", true);
  if(!section) return status("Please enter your section, e.g. 65_L.", true);

  const form = new FormData();
  form.append("routine_pdf", routine);
  form.append("section", section);
  if(seatPlan) form.append("seat_plan_pdf", seatPlan);

  busy = true;
  $("analyze").disabled = true;
  $("analyze").classList.add("loading-button");
  status("Processing the PDFs and matching your section…", false, true);
  try {
    const response = await fetch(`${API}/api/analyze`, {method:"POST", body:form});
    const body = await response.json().catch(() => ({}));
    if(!response.ok) {
      let message = body.detail || `Server error (${response.status})`;
      if(Array.isArray(body.detail)) message = body.detail.map(x => x.msg || "Validation error").join(", ");
      throw new Error(message);
    }
    data = body;
    renderResult();
    status(`Done. Found ${body.exam_count} exam(s) for ${body.section}.`);
  } catch(err) {
    console.error(err);
    status(`Could not analyze the PDFs. ${err.message}`, true);
  } finally {
    busy = false;
    $("analyze").disabled = false;
    $("analyze").classList.remove("loading-button");
  }
}

function renderResult() {
  $("result").classList.remove("hidden");
  $("preview").classList.add("hidden");
  $("resultTitle").textContent = `${data.section} — ${data.exam_count} examination(s)`;
  $("match").textContent = data.seat_plan_uploaded ? `${data.matched_seat_count}/${data.exam_count} seat matches` : "Routine only";
  $("warnings").innerHTML = (data.warnings || []).map(w => `<div class="warning">${escapeHtml(w)}</div>`).join("");
  $("exams").innerHTML = (data.exams || []).map((x,i) => `
    <article class="exam">
      <div class="exam-date-block"><div class="date">${formatDate(x.date)}</div><div class="code">${escapeHtml(x.day)} · Slot ${escapeHtml(x.slot)}</div></div>
      <div class="exam-course"><label>Course code</label><input data-i="${i}" data-key="course_code" value="${escapeAttr(x.course_code)}"><label>Course name</label><input data-i="${i}" data-key="course_name" value="${escapeAttr(x.course_name)}"></div>
      <div class="exam-time"><label>Exam time</label><input data-i="${i}" data-key="time" value="${escapeAttr(x.time)}"></div>
      <div class="rooms"><label>Rooms & seats</label>${x.rooms?.length ? x.rooms.map(r => `<div class="roomline"><span>Room ${escapeHtml(r.room)}</span><strong>${Number(r.seats)||0} seats</strong></div>`).join("") : `<span class="muted">${data.seat_plan_uploaded ? "No matched seat allocation" : "Seat plan not uploaded"}</span>`}</div>
    </article>`).join("");
  document.querySelectorAll("#exams input").forEach(input => input.addEventListener("input", () => {
    const i = Number(input.dataset.i); data.exams[i][input.dataset.key] = input.value;
  }));
}

function generateRoutine() {
  if(!data?.exams?.length) return status("Analyze the documents first.", true);
  const exams = [...data.exams].sort((a,b) => String(a.date).localeCompare(String(b.date)));
  data.exams = exams;
  const session = [data.semester,data.year].filter(Boolean).join(" ") || "EXAMINATION";
  const hasSeats = Boolean(data.seat_plan_uploaded);
  const totalStudents = getTotalStudents(exams);
  const section = escapeHtml(data.section);
  $("routineOutput").innerHTML = buildResponsiveRoutine(exams,session,hasSeats,totalStudents,section);
  $("exportStage").innerHTML = buildExportRoutine(exams,session,hasSeats,totalStudents,section);
  $("preview").classList.remove("hidden");
  $("preview").scrollIntoView({behavior:"smooth",block:"start"});
}

function buildResponsiveRoutine(exams,session,hasSeats,totalStudents,section) {
  const batch = getBatch(section, exams);
  return `<div class="routine-screen">
    <div class="routine-head">
      <div class="export-title"><span>${section}</span> FINAL EXAMINATION ROUTINE</div>
      <div class="session-line">${escapeHtml(String(session).replace("-","–"))}</div>
    </div>
    <div class="meta routine-meta">
      <div><span>SECTION</span><strong>${section}</strong></div>
      <div><span>EXAMINATIONS</span><strong>${exams.length}</strong></div>
      <div><span>BATCH</span><strong>${escapeHtml(batch)}</strong></div>
    </div>
    <div class="routine-table-wrap">
      <table class="routine-table ${hasSeats ? "with-seats" : "without-seats"}">
        <thead><tr>
          <th>DATE</th><th>DAY</th><th>COURSE</th><th>TIME</th>
          ${hasSeats ? "<th>ROOM</th><th>SEATS / ROOM</th>" : ""}
          <th>TOTAL</th>
        </tr></thead>
        <tbody>${exams.map((x,i)=>routineRow(x,i,hasSeats)).join("")}</tbody>
      </table>
    </div>
    <div class="note"><b>NOTE:</b> This schedule is based on the official routine published by the Examination Committee, FSIT.</div>
  </div>`;
}

function buildExportRoutine(exams,session,hasSeats,totalStudents,section) {
  const batch = getBatch(section, exams);
  const height = Math.max(1067, 450 + exams.length*145);
  return `<div class="export-routine ${hasSeats ? "export-with-seats" : "export-without-seats"}" style="height:${height}px">
    <div class="export-top"><div class="export-title"><span>${section}</span> FINAL EXAMINATION ROUTINE</div></div>
    <div class="export-session"><i></i><strong>${escapeHtml(String(session).replace("-","–"))}</strong><i></i></div>
    <div class="export-meta routine-meta-export">
      <div class="export-meta-item"><div><span>SECTION</span><strong>${section}</strong></div></div>
      <div class="export-meta-item"><div><span>EXAMINATIONS</span><strong>${exams.length}</strong></div></div>
      <div class="export-meta-item"><div><span>BATCH</span><strong>${escapeHtml(batch)}</strong></div></div>
    </div>
    <table class="export-table ${hasSeats ? "with-seats" : "without-seats"}">
      <thead><tr>
        <th>DATE</th><th>DAY</th><th>COURSE</th><th>TIME</th>
        ${hasSeats ? "<th>ROOM</th><th>SEATS / ROOM</th>" : ""}
        <th>TOTAL</th>
      </tr></thead>
      <tbody>${exams.map((x,i)=>exportRow(x,i,hasSeats)).join("")}</tbody>
    </table>
    <div class="export-note"><span>NOTE:</span> This schedule is based on the official routine published by the Examination Committee, FSIT.</div>
  </div>`;
}

function routineRow(x,i,hasSeats) {
  const total = getExamTotal(x);
  return `<tr class="screen-row-${i%4}">
    <td class="date-cell"><span class="date-icon"><span class="calendar-glyph"></span></span><strong>${formatDate(x.date)}</strong></td>
    <td class="center">${escapeHtml(x.day)}</td>
    <td class="course-cell"><b>${escapeHtml(x.course_code)}</b><span class="course-name"> — ${escapeHtml(x.course_name)}</span></td>
    <td class="center time-cell">${escapeHtml(x.time||"—")}</td>
    ${hasSeats ? `<td class="rooms-cell">${x.rooms?.length?x.rooms.map(r=>`<div>${escapeHtml(r.room)}</div>`).join(""):"—"}</td>
      <td class="center seats-cell">${x.rooms?.length?x.rooms.map(r=>`<div>${Number(r.seats)||0}</div>`).join(""):"—"}</td>` : ""}
    <td class="center total-cell"><strong>${total}</strong></td>
  </tr>`;
}

function exportRow(x,i,hasSeats) {
  const total = getExamTotal(x);
  return `<tr class="export-row-${i%4}">
    <td class="export-date"><div class="export-date-flex"><span class="export-calendar"><span class="calendar-glyph"></span></span><strong>${formatDate(x.date)}</strong></div></td>
    <td class="export-center">${escapeHtml(x.day)}</td>
    <td class="export-course"><b>${escapeHtml(x.course_code)}</b><span> — ${escapeHtml(x.course_name)}</span></td>
    <td class="export-center export-time">${escapeHtml(x.time||"—")}</td>
    ${hasSeats ? `<td class="export-rooms">${x.rooms?.length?x.rooms.map(r=>`<div>${escapeHtml(r.room)}</div>`).join(""):"—"}</td>
      <td class="export-seats">${x.rooms?.length?x.rooms.map(r=>`<div>${Number(r.seats)||0}</div>`).join(""):"—"}</td>` : ""}
    <td class="export-total">${total}</td>
  </tr>`;
}

function getExamTotal(x) {
  const explicit = Number(x.total_students);
  if (Number.isFinite(explicit) && explicit > 0) return explicit;
  if (x.rooms?.length) return x.rooms.reduce((sum,r)=>sum+(Number(r.seats)||0),0);
  return "—";
}

function getBatch(section, exams) {
  const match = String(section || "").match(/^(\d+)/);
  if (match) return match[1];
  const fromExam = exams.map(x => String(x.batch || "").match(/\d+/)).find(Boolean);
  return fromExam ? fromExam[0] : "—";
}
function getTotalStudents(exams) {
  const values = exams.map(getExamTotal).map(Number).filter(Number.isFinite);
  return values.length ? Math.max(...values) : "—";
}
async function ensureScript(src,test) {
  if(test()) return;
  await new Promise((resolve,reject)=>{const script=document.createElement("script");script.src=src;script.onload=resolve;script.onerror=reject;document.head.appendChild(script);});
}
async function makeExportCanvas() {
  if(!data) throw new Error("Generate the routine first.");
  await ensureScript("https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js",()=>Boolean(window.html2canvas));
  if(!$('exportStage').firstElementChild) generateRoutine();
  await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
  const target = $("exportStage").firstElementChild;
  return html2canvas(target,{width:target.offsetWidth,height:target.offsetHeight,scale:2,useCORS:true,backgroundColor:"#fff",logging:false});
}
async function downloadPNG() {
  if(!data) return status("Generate your routine first.",true);
  try { status("Preparing your high-quality PNG…",false,true); const canvas=await makeExportCanvas(); const a=document.createElement("a"); a.download=`${safeFileName(data.section)}_exam_routine.png`; a.href=canvas.toDataURL("image/png",1); a.click(); status("PNG downloaded successfully."); }
  catch(err){console.error(err);status(`Could not create PNG: ${err.message}`,true);}
}
async function downloadPDF() {
  if(!data) return status("Generate your routine first.",true);
  try { status("Preparing your PDF…",false,true); await ensureScript("https://cdnjs.cloudflare.com/ajax/libs/jspdf/2.5.1/jspdf.umd.min.js",()=>Boolean(window.jspdf?.jsPDF)); const canvas=await makeExportCanvas(); const pdf=new window.jspdf.jsPDF({orientation:"landscape",unit:"mm",format:[297,canvas.height/canvas.width*297],compress:true}); pdf.addImage(canvas.toDataURL("image/png",1),"PNG",0,0,297,canvas.height/canvas.width*297,undefined,"FAST"); pdf.save(`${safeFileName(data.section)}_exam_routine.pdf`); status("PDF downloaded successfully."); }
  catch(err){console.error(err);status(`Could not create PDF: ${err.message}`,true);}
}
function safeFileName(v){return String(v||"exam").replace(/[^a-z0-9_-]+/gi,"_");}
function formatDate(value){if(!value)return "—";const d=new Date(`${value}T00:00:00`);if(Number.isNaN(d.getTime()))return escapeHtml(value);return d.toLocaleDateString("en-GB",{day:"2-digit",month:"short",year:"numeric"});}
function escapeHtml(value){return String(value??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[c]));}
function escapeAttr(value){return escapeHtml(value);}
