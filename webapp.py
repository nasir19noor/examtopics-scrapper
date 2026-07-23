#!/usr/bin/env python3
"""
webapp.py — browser frontend for examtopics-scrapper.

Paste an ExamTopics discussion URL into the form; the page fetches it,
extracts the question + answer choices, shows them, and lets you copy or
download the result as a .txt file.

Run locally:   python webapp.py        (http://localhost:8000)
In Docker:     served by gunicorn (see Dockerfile)
"""

import os

from flask import Flask, render_template_string, request

from scraper import build_text, fetch, output_name, parse, ParseError

app = Flask(__name__)

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
    label.chk { font-size: .9rem; display: flex; align-items: center; gap: .35rem; }
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
  <form method="post" action="/">
    <input type="url" name="url" placeholder="https://www.examtopics.com/discussions/.../view/..."
           value="{{ url|e }}" required autofocus>
    <label class="chk"><input type="checkbox" name="header" {{ 'checked' if header }}> Include header</label>
    <button type="submit">Scrape</button>
  </form>

  {% if error %}
    <p class="err">⚠️ {{ error }}</p>
  {% endif %}

  {% if content %}
    <div class="row">
      <button class="secondary" onclick="copyText()">Copy</button>
      <button class="secondary" onclick="downloadText()">Download {{ filename }}</button>
      <span class="muted" id="status"></span>
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
    ctx = {"url": "", "header": False, "content": None, "error": None, "filename": ""}

    if request.method == "POST":
        url = request.form.get("url", "").strip()
        include_header = request.form.get("header") == "on"
        ctx.update(url=url, header=include_header)
        try:
            parsed = parse(fetch(url))
            ctx["content"]  = build_text(parsed, include_header)
            ctx["filename"] = output_name(url, parsed["info"])
        except ParseError as e:
            ctx["error"] = f"Couldn't parse that page: {e}"
        except Exception as e:
            ctx["error"] = f"{type(e).__name__}: {e}"

    return render_template_string(PAGE, **ctx)


@app.route("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=True)
