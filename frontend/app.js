const API = "https://examroutine.onrender.com/";

const $ = id => document.getElementById(id);

$("routine").addEventListener("change", e => {
  $("routineName").textContent = e.target.files[0]?.name || "Required";
});
$("seatPlan").addEventListener("change", e => {
  $("seatName").textContent = e.target.files[0]?.name || "Optional — adds rooms & seats";
});

$("analyze").onclick = analyze;
$("generate").onclick = generateRoutine;
$("print").onclick = () => window.print();
$("png").onclick = downloadPNG;

let data = null;

function status(message, error=false) {
  const el = $("status");
  el.textContent = message;
  el.classList.remove("hidden","error");
  if(error) el.classList.add("error");
}

async function analyze() {
  const routine = $("routine").files[0];
  const seatPlan = $("seatPlan").files[0];
  const section = $("section").value.trim();

  if(!routine) return status("Please upload the exam routine PDF.", true);
  if(!section) return status("Please enter your section, e.g. 65_L.", true);

  const form = new FormData();
  form.append("routine_pdf", routine);
  form.append("section", section);
  if(seatPlan) form.append("seat_plan_pdf", seatPlan);

  status("Processing the PDFs and matching your section...");

  try {
    const response = await fetch(`${API}/api/analyze`, {
      method: "POST",
      body: form
    });

    const body = await response.json();

    if(!response.ok) {
      throw new Error(body.detail || "Server error");
    }

    data = body;
    renderResult();
    status(`Done. Found ${body.exam_count} exam(s) for ${body.section}.`);
  } catch(err) {
    console.error(err);
    status(
      `Could not analyze the PDFs. Make sure the backend is running at ${API}. ${err.message}`,
      true
    );
  }
}

function renderResult() {
  $("result").classList.remove("hidden");
  $("preview").classList.add("hidden");

  $("resultTitle").textContent =
    `${data.section} — ${data.exam_count} examination(s)`;

  $("match").textContent = data.seat_plan_uploaded
    ? `${data.matched_seat_count}/${data.exam_count} seat matches`
    : "Routine only";

  $("warnings").innerHTML = (data.warnings || []).map(w =>
    `<div class="warning">${escapeHtml(w)}</div>`
  ).join("");

  $("exams").innerHTML = data.exams.map((x,i) => `
    <div class="exam">
      <div>
        <div class="date">${formatDate(x.date)}</div>
        <div class="code">${escapeHtml(x.day)} · Slot ${escapeHtml(x.slot)}</div>
      </div>

      <div>
        <input data-i="${i}" data-key="course_code"
               value="${escapeAttr(x.course_code)}">
        <input data-i="${i}" data-key="course_name"
               value="${escapeAttr(x.course_name)}"
               style="margin-top:7px">
      </div>

      <input data-i="${i}" data-key="time"
             value="${escapeAttr(x.time)}">

      <div class="rooms">
        ${
          x.rooms.length
          ? x.rooms.map(r => `
              <div class="roomline">
                <span>Room ${escapeHtml(r.room)}</span>
                <strong>${r.seats} seats</strong>
              </div>
            `).join("")
          : `<span style="color:#6b7788">
               ${data.seat_plan_uploaded
                 ? "No matched seat allocation"
                 : "Seat plan not uploaded"}
             </span>`
        }
      </div>
    </div>
  `).join("");

  document.querySelectorAll("#exams input").forEach(input => {
    input.addEventListener("input", () => {
      const i = Number(input.dataset.i);
      data.exams[i][input.dataset.key] = input.value;
    });
  });
}

function generateRoutine() {
  const exams = data.exams;
  const hasSeats = data.seat_plan_uploaded;

  const session = [data.semester, data.year].filter(Boolean).join(" ") || "EXAMINATION";

  $("routineOutput").innerHTML = `
    <div class="routine">
      <div class="routine-head">
        <h3>${escapeHtml(data.section)} FINAL EXAMINATION ROUTINE</h3>
        <p>${escapeHtml(session)}</p>
      </div>

      <div class="meta">
        <div>
          <span>SECTION</span>
          <strong>${escapeHtml(data.section)}</strong>
        </div>
        <div>
          <span>EXAMINATIONS</span>
          <strong>${exams.length}</strong>
        </div>
        <div>
          <span>BATCH</span>
          <strong>${escapeHtml(data.batch)}</strong>
        </div>
      </div>

      <table>
        <thead>
          <tr>
            <th>DATE</th>
            <th>DAY</th>
            <th>COURSE</th>
            <th>TIME</th>
            ${hasSeats ? "<th>ROOM</th><th>SEATS / ROOM</th><th>TOTAL</th>" : ""}
          </tr>
        </thead>
        <tbody>
          ${exams.map(x => `
            <tr>
              <td class="center"><b>${formatDate(x.date)}</b></td>
              <td class="center">${escapeHtml(x.day)}</td>
              <td>
                <b>${escapeHtml(x.course_code)}</b> —
                ${escapeHtml(x.course_name)}
              </td>
              <td class="center">${escapeHtml(x.time)}</td>
              ${
                hasSeats
                ? `
                  <td class="rooms-cell">
                    ${x.rooms.length
                      ? x.rooms.map(r => `<div>${escapeHtml(r.room)}</div>`).join("")
                      : "—"}
                  </td>
                  <td class="center">
                    ${x.rooms.length
                      ? x.rooms.map(r => `<div>${r.seats}</div>`).join("")
                      : "—"}
                  </td>
                  <td class="center"><b>${x.total_students ?? "—"}</b></td>
                `
                : ""
              }
            </tr>
          `).join("")}
        </tbody>
      </table>

      <div class="note">
        Generated from the uploaded official examination documents.
        Verify the final routine against the official notice before use.
      </div>
    </div>
  `;

  $("preview").classList.remove("hidden");
  $("preview").scrollIntoView({behavior:"smooth"});
}

async function downloadPNG() {
  if(!window.html2canvas) {
    await new Promise((resolve,reject) => {
      const script = document.createElement("script");
      script.src =
        "https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js";
      script.onload = resolve;
      script.onerror = reject;
      document.head.appendChild(script);
    });
  }

  const canvas = await html2canvas(
    document.querySelector(".routine"),
    {scale:2, backgroundColor:"#ffffff"}
  );

  const a = document.createElement("a");
  a.download = `${data.section}_exam_routine.png`;
  a.href = canvas.toDataURL("image/png");
  a.click();
}

function formatDate(value) {
  return new Date(value + "T00:00:00")
    .toLocaleDateString("en-GB", {
      day:"2-digit",
      month:"short",
      year:"numeric"
    });
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, c => ({
    "&":"&amp;",
    "<":"&lt;",
    ">":"&gt;",
    '"':"&quot;",
    "'":"&#039;"
  }[c]));
}

function escapeAttr(value) {
  return escapeHtml(value);
}
