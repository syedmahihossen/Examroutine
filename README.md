# 🎓 DIU ExamRoutine

An automated, zero-latency exam routine and seat plan finder designed specifically for Daffodil International University (DIU) CSE students. 

Instead of manually scrolling through massive, unformatted PDF documents, students can simply enter their section (e.g., `65_L`) and instantly receive a personalized, clearly formatted schedule complete with room numbers and seat allocations.

## ✨ Features

* **⚡ Zero-Latency Search:** Routine data is pre-processed and cached, delivering personalized schedules to students in milliseconds.
* **🪑 Automated Seat Plan Mapping:** Cross-references the official routine with seat plan documents to automatically assign rooms and seat ranges to specific sections.
* **🌙 Premium Dark UI:** A polished, modern, and professional dark-themed user interface optimized for readability.
* **📥 Export Options:** Students can easily download their personalized routines as PNG or PDF files for offline viewing.

---

## 🏗️ System Architecture & Data Flow

ExamRoutine utilizes a hybrid, event-driven architecture designed for instant user experiences and minimal server cost. By decoupling the scraping engine from the frontend, the application ensures that users never have to wait for a backend server to wake up or parse PDFs in real-time.

1. **Automated Trigger (GitHub Actions):** A cron job runs a workflow every 30 minutes, acting as a background alarm clock.
2. **Smart Scraper (Render - Python/FastAPI):** 
   * Wakes up and checks the official DIU Notice Board.
   * Compares the latest PDF URL against a saved Firebase bookmark. If the URL hasn't changed, the server immediately stops to conserve resources.
   * If a new PDF is detected, it downloads the documents.
3. **Data Extraction (Regex):** The backend parses the PDF, dynamically utilizes Regular Expressions (Regex) to identify every single active section (e.g., `65_L`, `64_M`), and maps their individual exam schedules and seat plans.
4. **Real-time Cache (Firebase RTDB):** The parsed, structured JSON for every discovered section is pushed to Firebase in a massive batch, acting as a high-speed database cache.
5. **Instant Frontend (Cloudflare Pages):** When a student searches for their section, the static frontend queries Firebase directly. The data loads in milliseconds without ever touching the Python backend. If a typo occurs, the UI gracefully handles the error.

---

## 🛠️ Tech Stack

* **Frontend:** Vanilla JavaScript, HTML5, CSS3 (Hosted on Cloudflare Pages)
* **Backend Engine:** Python, FastAPI, Requests, Regex (Hosted on Render)
* **Database/Cache:** Firebase Realtime Database (Secured via REST Auth)
* **Automation:** GitHub Actions (Cron scheduling)

---



👨‍💻 Author
Syed Mahi Hosen

Computer Science and Engineering (CSE)

Daffodil International University (DIU)
