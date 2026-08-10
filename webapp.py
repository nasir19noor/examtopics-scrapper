#!/usr/bin/env python3
"""
webapp.py — browser frontend for examtopics-scrapper.

Two modes:

* **Single** — paste one discussion URL, get the question + choices back.
* **Bulk**   — paste one URL from an exam plus a range like ``1-100``; every
  matching question is collected and returned as a zip of ``1.txt``,
  ``2.txt``, … See finder.py for how the exam's pages are located.

Bulk runs as a background job with a polling progress bar, because it takes
tens of seconds and a synchronous request that long is exactly the hang this
app used to suffer from.

Run locally:   python webapp.py        (http://localhost:8000)
In Docker:     served by gunicorn (see Dockerfile)
"""

import io
import os
import threading
import time
import uuid
import zipfile
from collections import OrderedDict

from flask import (Flask, abort, jsonify, render_template_string, request,
                   send_file)

import examcademy
import finder
from scraper import (
    build_text,
    fetch,
    output_name,
    parse,
    FetchTimeout,
    ParseError,
    RateLimited,
)

app = Flask(__name__)

# ─── Bulk job registry ─────────────────────────────────────────────
# Jobs live in-process. That is fine for the single-container deployment
# this app targets, but it does mean a job is only visible to the worker
# that started it — so run gunicorn with threads, not multiple workers,
# if you rely on bulk mode (see Dockerfile).

JOB_MAX = int(os.environ.get("BULK_JOB_MAX", "8"))
_JOBS: "OrderedDict[str, dict]" = OrderedDict()
_JOBS_LOCK = threading.Lock()


def _register(job: dict) -> None:
    with _JOBS_LOCK:
        _JOBS[job["id"]] = job
        while len(_JOBS) > JOB_MAX:
            old_id, old = _JOBS.popitem(last=False)
            old["stop"].set()


def _get_job(job_id: str) -> dict:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if job is None:
        abort(404, "Unknown or expired job.")
    return job

PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ExamTopics Scrapper</title>
  <style>
    :root { color-scheme: light dark; }
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
           max-width: 860px; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; }
    h1 { font-size: 1.4rem; }
    form { display: flex; flex-wrap: wrap; gap: .6rem; align-items: center; margin-bottom: 1rem; }
    input[type=url] { flex: 1 1 420px; padding: .6rem .7rem; font-size: 1rem;
                      border: 1px solid #999; border-radius: 6px; }
    button { padding: .6rem 1rem; font-size: 1rem; border: 0; border-radius: 6px;
             background: #2563eb; color: #fff; cursor: pointer; }
    button.secondary { background: #555; }
    button[disabled] { background: #94a3b8; cursor: progress; }
    label.chk { font-size: .9rem; display: flex; align-items: center; gap: .35rem; }
    #working { display: none; align-items: center; gap: .5rem; color: #777; font-size: .9rem; }
    #working.on { display: flex; }
    .spin { width: .9rem; height: .9rem; border: 2px solid #cbd5e1; border-top-color: #2563eb;
            border-radius: 50%; animation: sp .7s linear infinite; }
    @keyframes sp { to { transform: rotate(360deg); } }
    .tabs { display: flex; gap: .4rem; border-bottom: 1px solid #d4d4d8; margin-bottom: 1rem; }
    .tabs button { background: none; color: inherit; border: 0; border-bottom: 2px solid transparent;
                   border-radius: 0; padding: .5rem .9rem; font-size: .95rem; opacity: .65; }
    .tabs button.active { border-bottom-color: #2563eb; opacity: 1; font-weight: 600; }
    .pane { display: none; } .pane.active { display: block; }
    .bar { height: 8px; background: #e4e4e7; border-radius: 999px; overflow: hidden; margin: .5rem 0; }
    .bar > i { display: block; height: 100%; width: 0; background: #2563eb; transition: width .3s; }
    .hint { color: #777; font-size: .82rem; margin: -.2rem 0 .8rem; }
    #bulkbox { display: none; margin-top: 1rem; }
    .pill { display: inline-block; background: #e4e4e7; border-radius: 999px;
            padding: .05rem .5rem; font-size: .78rem; margin: .1rem .15rem 0 0; }
    .pill.miss { background: #fde2e2; color: #8a1f1f; }
    .err { background: #fde2e2; color: #8a1f1f; padding: .7rem 1rem; border-radius: 6px; }
    textarea { width: 100%; min-height: 360px; font-family: ui-monospace, Menlo, Consolas, monospace;
               font-size: .9rem; padding: .8rem; border: 1px solid #999; border-radius: 6px;
               box-sizing: border-box; white-space: pre; }
    .row { display: flex; gap: .6rem; margin: .6rem 0; }
    .muted { color: #777; font-size: .85rem; }
  </style>
</head>
<body>
  <h1>📄 ExamTopics Scrapper</h1>

  <div class="tabs">
    <button id="tab-single" class="active" onclick="showTab('single')">Single URL</button>
    <button id="tab-bulk" onclick="showTab('bulk')">Bulk by question #</button>
    <button id="tab-cademy" onclick="showTab('cademy')">Examcademy</button>
  </div>

  <div id="pane-single" class="pane active">
  <form method="post" action="/" id="form">
    <input type="url" name="url" placeholder="https://www.examtopics.com/discussions/.../view/..."
           value="{{ url|e }}" required autofocus>
    <label class="chk"><input type="checkbox" name="header" {{ 'checked' if header }}> Include header</label>
    <button type="submit" id="go">Scrape</button>
    <span id="working"><span class="spin"></span><span id="timer">Scraping…</span></span>
  </form>
  </div>

  <div id="pane-bulk" class="pane">
    <form id="bulkform" onsubmit="return startBulk(event)">
      <input type="url" name="url" id="bulk-url" style="flex:1 1 100%"
             placeholder="Any one discussion URL from the exam, e.g. .../view/384242-exam-aws-certified-generative-ai-developer-professional-aip/"
             value="{{ url|e }}" required>
      <div id="bulk-seeds" style="flex:1 1 100%; display:flex; flex-direction:column; gap:.5rem"></div>
      <button type="button" class="secondary" id="bulk-addseed"
              onclick="addSeed()" style="flex:0 0 auto">+ Add another batch URL</button>
      <input type="text" name="numbers" id="bulk-numbers" value="1-117"
             style="flex:0 1 200px" placeholder="1-117">
      <label class="chk"><input type="checkbox" id="bulk-header" checked> Include header</label>
      <label class="chk"><input type="checkbox" id="bulk-hunt" checked> Hunt other id bands</label>
      <button type="submit" id="bulk-go">Collect</button>
      <button type="button" class="secondary" id="bulk-cancel"
              style="display:none" onclick="cancelBulk()">Cancel</button>
    </form>
    <p class="hint">
      Paste <em>any single</em> question URL from the exam — its slug
      (<code>exam-aws-…-aip</code>) identifies the whole exam, and unlike a
      search keyword it can't drift onto a similarly-named exam.
      Results come back as a zip of <code>1.txt</code>, <code>2.txt</code>, …
      <br>
      Questions are added to ExamTopics in batches, and each batch lands in a
      different range of discussion ids. AIP-C01 currently has <b>three</b>:
      questions 1-85 near id 384102, 86-97 near 402483, and 98-117 near 421752.
      <b>Hunt other id bands</b> finds them automatically but takes several
      minutes — adding one URL per batch above skips the hunt entirely and
      collects all 117 in about 90 seconds.
    </p>

    <div id="bulkbox">
      <div class="bar"><i id="bulk-bar"></i></div>
      <p class="muted" id="bulk-msg">Starting…</p>
      <p id="bulk-found"></p>
      <p><a id="bulk-dl" style="display:none"><button>⬇ Download zip</button></a></p>
    </div>
  </div>

  <div id="pane-cademy" class="pane">
    <form id="ecform" onsubmit="return startCademy(event)">
      <input type="url" id="ec-url" style="flex:1 1 100%" required
             placeholder="https://examcademy.com/exams/amazon/aws-certified-generative-ai-developer-professional-aip-c01/1">
      <input type="text" id="ec-numbers" value="1-25" style="flex:0 1 200px" placeholder="1-25">
      <label class="chk"><input type="checkbox" id="ec-header" checked> Include header</label>
      <label class="chk"><input type="checkbox" id="ec-answers" checked> Answers + explanations</label>
      <button type="submit" id="ec-go">Collect</button>
      <button type="button" class="secondary" onclick="checkAuth()">Check sign-in</button>
      <span id="ec-working"><span class="spin"></span><span id="ec-timer">Working…</span></span>
    </form>
    <p class="hint">
      Examcademy serves 25 questions per page and, unlike the ExamTopics
      discussion pages, includes the <b>correct answer and an explanation</b>.
      <br>
      Only page 1 (questions 1-25) is public. The site signs in through Auth0
      — email, <b>Google</b> or Microsoft — but this tool can't run that flow
      for you. Sign in with your Google account in the browser as normal, then
      open DevTools → Network → click any document request → copy the whole
      <code>Cookie</code> request header for <code>examcademy.com</code> and
      paste it below. <b>Check sign-in</b> tells you straight away whether it
      was accepted.
    </p>
    <input type="text" id="ec-cookie" style="width:100%; padding:.6rem .7rem;
           border:1px solid #999; border-radius:6px; box-sizing:border-box"
           placeholder="Cookie header from a signed-in examcademy session (or leave blank to use EXAMCADEMY_COOKIE from .env)">
    <div id="ecbox" style="display:none; margin-top:1rem">
      <p class="muted" id="ec-msg"></p>
      <p id="ec-notes" class="err" style="display:none"></p>
      <p><a id="ec-dl" style="display:none"><button>⬇ Download zip</button></a></p>
    </div>
  </div>

  <script>
    function showTab(which) {
      for (const t of ['single', 'bulk', 'cademy']) {
        document.getElementById('pane-' + t).classList.toggle('active', t === which);
        document.getElementById('tab-' + t).classList.toggle('active', t === which);
      }
    }

    // Without this the browser just spins with no feedback, so even a normal
    // 2-3s fetch reads as a hang. Also blocks double-submits, which are what
    // trip ExamTopics' rate limiter in the first place.
    document.getElementById('form').addEventListener('submit', () => {
      const go = document.getElementById('go');
      go.disabled = true;
      document.getElementById('working').classList.add('on');
      const t0 = Date.now();
      setInterval(() => {
        const s = ((Date.now() - t0) / 1000).toFixed(1);
        document.getElementById('timer').textContent = `Scraping… ${s}s`;
      }, 100);
    });

    // ── Bulk mode: start a job, then poll it. Long work must never sit
    //    inside one request — that is what made this app feel hung before.
    let bulkJob = null, bulkPoll = null;

    // One optional seed field per extra batch. The count isn't fixed: an exam
    // grows a new id band every time ExamTopics adds questions.
    function addSeed(value) {
      const wrap = document.getElementById('bulk-seeds');
      const n = wrap.children.length + 2;      // batch 1 is the main url field
      const row = document.createElement('div');
      row.style.cssText = 'display:flex; gap:.5rem; align-items:center';
      const inp = document.createElement('input');
      inp.type = 'url';
      inp.className = 'seed';
      inp.style.cssText = 'flex:1 1 auto; padding:.6rem .7rem; font-size:1rem;' +
                          'border:1px solid #999; border-radius:6px';
      inp.placeholder = `Batch ${n}: any URL from that batch (e.g. .../view/402483-…)`;
      if (value) inp.value = value;
      const del = document.createElement('button');
      del.type = 'button'; del.className = 'secondary'; del.textContent = '×';
      del.title = 'Remove';
      del.onclick = () => { row.remove(); renumberSeeds(); };
      row.append(inp, del);
      wrap.append(row);
      inp.focus();
    }

    function renumberSeeds() {
      [...document.querySelectorAll('#bulk-seeds .seed')].forEach((inp, i) => {
        inp.placeholder = `Batch ${i + 2}: any URL from that batch (e.g. .../view/402483-…)`;
      });
    }

    addSeed();   // start with one spare field visible

    async function startBulk(ev) {
      ev.preventDefault();
      const body = new URLSearchParams({
        url: document.getElementById('bulk-url').value,
        numbers: document.getElementById('bulk-numbers').value,
      });
      if (document.getElementById('bulk-header').checked) body.set('header', 'on');
      if (document.getElementById('bulk-hunt').checked) body.set('hunt', 'on');
      // Repeated 'seed' keys — the server reads them with getlist().
      for (const inp of document.querySelectorAll('#bulk-seeds .seed')) {
        if (inp.value.trim()) body.append('seed', inp.value.trim());
      }

      const box = document.getElementById('bulkbox');
      box.style.display = 'block';
      document.getElementById('bulk-dl').style.display = 'none';
      document.getElementById('bulk-found').textContent = '';
      document.getElementById('bulk-bar').style.width = '0';
      document.getElementById('bulk-msg').textContent = 'Starting…';

      const r = await fetch('/bulk', {method: 'POST', body});
      const j = await r.json();
      if (!r.ok) {
        document.getElementById('bulk-msg').textContent = '⚠️ ' + j.error;
        return false;
      }
      bulkJob = j.job;
      document.getElementById('bulk-go').disabled = true;
      document.getElementById('bulk-cancel').style.display = '';
      bulkPoll = setInterval(pollBulk, 700);
      return false;
    }

    async function pollBulk() {
      if (!bulkJob) return;
      const s = await (await fetch(`/bulk/${bulkJob}/status`)).json();

      // Two phases share one bar: sweeping ids, then the questions found.
      const pct = s.wanted ? Math.min(100, 100 * s.found.length / s.wanted)
                           : (s.total_probe ? 100 * s.probed / s.total_probe : 0);
      document.getElementById('bulk-bar').style.width = pct + '%';
      const phase = s.phase === 'coarse' ? 'hunting other id bands' : s.phase;
      document.getElementById('bulk-msg').textContent =
        `${s.message}  ·  [${phase}] swept ${s.probed} ids, ${s.matched} on this exam  ·  ${s.elapsed}s`;

      if (s.found.length) {
        document.getElementById('bulk-found').innerHTML =
          s.found.map(n => `<span class="pill">${n}</span>`).join('') +
          s.missing.map(n => `<span class="pill miss">${n}</span>`).join('');
      }

      if (s.done) {
        clearInterval(bulkPoll);
        document.getElementById('bulk-go').disabled = false;
        document.getElementById('bulk-cancel').style.display = 'none';
        document.getElementById('bulk-bar').style.width = '100%';
        if (s.error) {
          document.getElementById('bulk-msg').textContent = '⚠️ ' + s.error;
        }
        if (s.found.length) {
          const a = document.getElementById('bulk-dl');
          a.href = `/bulk/${bulkJob}/download`;
          a.style.display = '';
        }
      }
    }

    async function cancelBulk() {
      if (bulkJob) await fetch(`/bulk/${bulkJob}/cancel`, {method: 'POST'});
    }

    // ── Examcademy: at most 5 page fetches, so a plain request is fine here.
    let ecJob = null;

    async function checkAuth() {
      const body = new URLSearchParams({
        url: document.getElementById('ec-url').value,
        cookie: document.getElementById('ec-cookie').value.trim(),
      });
      document.getElementById('ecbox').style.display = 'block';
      document.getElementById('ec-msg').textContent = 'Checking…';
      const r = await fetch('/examcademy/check', {method: 'POST', body});
      const j = await r.json();
      document.getElementById('ec-msg').textContent =
        r.ok ? `Session: ${j.state} — ${j.hint}` : '⚠️ ' + j.error;
    }

    async function startCademy(ev) {
      ev.preventDefault();
      const body = new URLSearchParams({
        url: document.getElementById('ec-url').value,
        numbers: document.getElementById('ec-numbers').value,
        cookie: document.getElementById('ec-cookie').value.trim(),
      });
      if (document.getElementById('ec-header').checked) body.set('header', 'on');
      if (document.getElementById('ec-answers').checked) body.set('answers', 'on');

      const go = document.getElementById('ec-go');
      go.disabled = true;
      document.getElementById('ec-working').classList.add('on');
      document.getElementById('ecbox').style.display = 'block';
      document.getElementById('ec-msg').textContent = 'Fetching…';
      document.getElementById('ec-notes').style.display = 'none';
      document.getElementById('ec-dl').style.display = 'none';

      const r = await fetch('/examcademy', {method: 'POST', body});
      const j = await r.json();
      go.disabled = false;
      document.getElementById('ec-working').classList.remove('on');

      if (!r.ok) {
        document.getElementById('ec-msg').textContent = '⚠️ ' + j.error;
        return false;
      }
      ecJob = j.job;
      document.getElementById('ec-msg').textContent =
        `Collected ${j.found.length} of ${j.wanted} questions` +
        (j.missing.length ? ` · missing ${j.missing.length}` : '');
      if (j.notes.length) {
        const n = document.getElementById('ec-notes');
        n.style.display = 'block';
        n.textContent = j.notes.join('  ');
      }
      if (j.found.length) {
        const a = document.getElementById('ec-dl');
        a.href = `/examcademy/${ecJob}/download`;
        a.style.display = '';
      }
      return false;
    }
  </script>

  {% if error %}
    <p class="err">⚠️ {{ error }}</p>
  {% endif %}

  {% if content %}
    <div class="row">
      <button class="secondary" onclick="copyText()">Copy</button>
      <button class="secondary" onclick="downloadText()">Download {{ filename }}</button>
      <span class="muted" id="status">{% if elapsed %}fetched in {{ elapsed }}{% endif %}</span>
    </div>
    <textarea id="content" readonly>{{ content }}</textarea>
    <script>
      const FILENAME = {{ filename|tojson }};
      function copyText() {
        const t = document.getElementById('content');
        navigator.clipboard.writeText(t.value).then(() => {
          document.getElementById('status').textContent = 'Copied!';
        });
      }
      function downloadText() {
        const blob = new Blob([document.getElementById('content').value], {type: 'text/plain'});
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = FILENAME;
        a.click();
        URL.revokeObjectURL(a.href);
      }
    </script>
  {% endif %}
</body>
</html>"""


@app.route("/", methods=["GET", "POST"])
def index():
    ctx = {"url": "", "header": False, "content": None, "error": None,
           "filename": "", "elapsed": ""}

    if request.method == "POST":
        url = request.form.get("url", "").strip()
        include_header = request.form.get("header") == "on"
        ctx.update(url=url, header=include_header)
        started = time.monotonic()
        try:
            parsed = parse(fetch(url))
            ctx["content"]  = build_text(parsed, include_header)
            ctx["filename"] = output_name(url, parsed["info"])
            ctx["elapsed"]  = f"{time.monotonic() - started:.1f}s"
        except RateLimited as e:
            ctx["error"] = str(e)
        except FetchTimeout as e:
            ctx["error"] = str(e)
        except ParseError as e:
            ctx["error"] = f"Couldn't parse that page: {e}"
        except Exception as e:
            ctx["error"] = f"{type(e).__name__}: {e}"

    return render_template_string(PAGE, **ctx)


@app.post("/bulk")
def bulk_start():
    """Kick off a background collection and hand back a job id to poll."""
    seed  = (request.form.get("url") or "").strip()
    spec  = (request.form.get("numbers") or "").strip()
    header = request.form.get("header") == "on"
    hunt   = request.form.get("hunt") == "on"
    # One seed per known id batch; the UI adds fields on demand.
    extra  = [s.strip() for s in request.form.getlist("seed") if s.strip()]
    try:
        finder.parse_seed(seed)              # validate before spawning a thread
        for s in extra:
            finder.parse_seed(s)
        numbers = finder.parse_range(spec)
    except finder.FinderError as e:
        return jsonify(error=str(e)), 400

    window  = int(os.environ.get("BULK_WINDOW",  "400"))
    workers = int(os.environ.get("BULK_WORKERS", "8"))
    reach   = int(os.environ.get("BULK_REACH",   "30000")) if hunt else 0

    job = {
        "id": uuid.uuid4().hex[:12],
        "progress": finder.Progress(),
        "stop": threading.Event(),
        "started": time.time(),
        "header": header,
    }
    _register(job)

    def run():
        try:
            finder.collect(seed, numbers, include_header=header, window=window,
                           workers=workers, stop=job["stop"],
                           progress=job["progress"], extra_seeds=extra,
                           reach=reach)
        except Exception as e:                # noqa: BLE001 - surface anything
            p = job["progress"]
            with p.lock:
                p.error = f"{type(e).__name__}: {e}"
                p.done = True

    threading.Thread(target=run, daemon=True).start()
    return jsonify(job=job["id"])


@app.get("/bulk/<job_id>/status")
def bulk_status(job_id):
    job = _get_job(job_id)
    if "progress" not in job:
        abort(404, "Not a bulk job.")
    snap = job["progress"].snapshot()
    snap["elapsed"] = round(time.time() - job["started"], 1)
    return jsonify(snap)


@app.post("/bulk/<job_id>/cancel")
def bulk_cancel(job_id):
    _get_job(job_id)["stop"].set()
    return jsonify(ok=True)


def _zip_of(results: dict, missing: list, prefix: str = "examtopics"):
    """Zip questions as 1.txt, 2.txt, … plus an all-in-one file."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n in sorted(results):
            z.writestr(f"{n}.txt", results[n])
        combined = "\n\n".join(
            f"{'=' * 60}\nQuestion {n}\n{'=' * 60}\n\n{results[n].rstrip()}"
            for n in sorted(results)
        )
        z.writestr("all-questions.txt", combined + "\n")
        if missing:
            z.writestr("missing.txt", "Question numbers not collected:\n"
                       + ", ".join(map(str, missing)) + "\n")
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"{prefix}-{len(results)}-questions.zip")


@app.get("/bulk/<job_id>/download")
def bulk_download(job_id):
    job = _get_job(job_id)
    prog = job["progress"]
    with prog.lock:
        results = dict(prog.results)
        missing = list(prog.missing)
    if not results:
        abort(404, "Nothing collected for this job.")
    return _zip_of(results, missing)


@app.post("/examcademy")
def examcademy_collect():
    """Collect examcademy questions. At most 5 page fetches, so run inline."""
    url    = (request.form.get("url") or "").strip()
    spec   = (request.form.get("numbers") or "").strip()
    cookie = (request.form.get("cookie") or "").strip() or examcademy.cookie_from_env()
    header  = request.form.get("header") == "on"
    answers = request.form.get("answers") == "on"
    try:
        examcademy.parse_exam_url(url)
        numbers = finder.parse_range(spec)
    except (examcademy.ExamcademyError, finder.FinderError) as e:
        return jsonify(error=str(e)), 400

    try:
        results, notes = examcademy.collect(
            url, numbers, include_header=header, include_answer=answers,
            cookie=cookie)
    except Exception as e:                           # noqa: BLE001
        return jsonify(error=f"{type(e).__name__}: {e}"), 502

    job = {"id": uuid.uuid4().hex[:12], "results": results,
           "missing": sorted(set(numbers) - set(results)),
           "stop": threading.Event(), "started": time.time()}
    _register(job)
    return jsonify(job=job["id"], found=sorted(results), wanted=len(numbers),
                   missing=job["missing"], notes=notes)


@app.post("/examcademy/check")
def examcademy_check():
    """Report whether a supplied cookie actually signs us in."""
    url    = (request.form.get("url") or "").strip()
    cookie = (request.form.get("cookie") or "").strip() or examcademy.cookie_from_env()
    try:
        provider, exam, _ = examcademy.parse_exam_url(url)
    except examcademy.ExamcademyError as e:
        return jsonify(error=str(e)), 400

    headers = dict(examcademy.scraper.HEADERS)
    if cookie:
        headers["Cookie"] = cookie
    try:
        html = examcademy.scraper.fetch(
            examcademy.page_url(provider, exam, 1), headers=headers)
    except Exception as e:                           # noqa: BLE001
        return jsonify(error=f"{type(e).__name__}: {e}"), 502

    state = examcademy.session_state(html)
    hint = ("gated pages should be reachable." if state == "authenticated"
            else "only questions 1-25 are reachable. "
                 + ("Paste a Cookie header from a signed-in session."
                    if not cookie else
                    "That cookie was not accepted — it may have expired, or been "
                    "copied for auth.examcademy.com instead of examcademy.com."))
    return jsonify(state=state, hint=hint)


@app.get("/examcademy/<job_id>/download")
def examcademy_download(job_id):
    job = _get_job(job_id)
    results = job.get("results") or {}
    if not results:
        abort(404, "Nothing collected for this job.")
    return _zip_of(results, job.get("missing") or [], prefix="examcademy")


@app.route("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=True)
