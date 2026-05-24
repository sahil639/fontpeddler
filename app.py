import io
import os
import zipfile
from flask import Flask, request, send_file, Response

from fontTools.ttLib import TTFont, TTLibError

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024 * 1024  # 256 MB upload cap


INDEX_HTML = """<!doctype html>
<html lang=\"en\">
<head>
<meta charset=\"utf-8\" />
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
<title>Fontstealer — WOFF/WOFF2 → OTF/TTF</title>
<style>
  :root {
    --bg: #0f1115;
    --panel: #161a22;
    --panel-2: #1d2230;
    --text: #e6e8ee;
    --muted: #9aa3b2;
    --accent: #7c9cff;
    --accent-2: #5b7cff;
    --border: #262c3a;
    --danger: #ff6b6b;
    --ok: #74e0a8;
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0;
    padding: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, \"Segoe UI\", Roboto, Helvetica, Arial, sans-serif;
    min-height: 100vh;
  }
  .wrap {
    max-width: 760px;
    margin: 0 auto;
    padding: 48px 24px 80px;
  }
  h1 {
    font-size: 28px;
    margin: 0 0 8px;
    letter-spacing: -0.01em;
  }
  p.sub {
    color: var(--muted);
    margin: 0 0 32px;
    line-height: 1.5;
  }
  .drop {
    border: 2px dashed var(--border);
    border-radius: 14px;
    padding: 48px 24px;
    text-align: center;
    background: var(--panel);
    transition: border-color .15s, background .15s;
    cursor: pointer;
  }
  .drop.drag {
    border-color: var(--accent);
    background: var(--panel-2);
  }
  .drop strong { color: var(--text); }
  .drop span { color: var(--muted); }
  .browse-btn {
    display: inline-block;
    margin-top: 12px;
    color: var(--accent);
    text-decoration: underline;
    text-underline-offset: 3px;
  }
  input[type=\"file\"] { display: none; }
  .list {
    margin-top: 20px;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 12px;
    overflow: hidden;
  }
  .list:empty { display: none; }
  .row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 12px 16px;
    border-top: 1px solid var(--border);
    font-size: 14px;
  }
  .row:first-child { border-top: none; }
  .row .name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .row .size { color: var(--muted); font-variant-numeric: tabular-nums; }
  .row button {
    background: transparent;
    color: var(--muted);
    border: none;
    cursor: pointer;
    font-size: 14px;
    padding: 4px 8px;
  }
  .row button:hover { color: var(--danger); }
  .controls {
    margin-top: 20px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 16px;
    flex-wrap: wrap;
  }
  label.check {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    color: var(--muted);
    cursor: pointer;
    user-select: none;
  }
  label.check input { accent-color: var(--accent); }
  button.primary {
    background: var(--accent);
    color: #0f1115;
    border: none;
    border-radius: 10px;
    padding: 12px 22px;
    font-size: 15px;
    font-weight: 600;
    cursor: pointer;
    transition: background .15s, transform .05s;
  }
  button.primary:hover { background: var(--accent-2); color: white; }
  button.primary:disabled {
    opacity: .5;
    cursor: not-allowed;
  }
  .status {
    margin-top: 16px;
    min-height: 20px;
    font-size: 14px;
    color: var(--muted);
  }
  .status.err { color: var(--danger); }
  .status.ok { color: var(--ok); }
  footer {
    margin-top: 48px;
    color: var(--muted);
    font-size: 12px;
    text-align: center;
  }
</style>
</head>
<body>
  <div class=\"wrap\">
    <h1>Fontstealer</h1>
    <p class=\"sub\">Convert <strong>WOFF</strong> &amp; <strong>WOFF2</strong> fonts to <strong>OTF</strong>/<strong>TTF</strong>. Drag files in, click Convert, get a zip.</p>

    <div id=\"drop\" class=\"drop\">
      <div><strong>Drop .woff / .woff2 files here</strong></div>
      <div><span>or</span> <span class=\"browse-btn\">click to browse</span></div>
      <input id=\"file\" type=\"file\" accept=\".woff,.woff2\" multiple />
    </div>

    <div id=\"list\" class=\"list\"></div>

    <div class=\"controls\">
      <label class=\"check\">
        <input id=\"force\" type=\"checkbox\" />
        Force every file to .otf
      </label>
      <button id=\"go\" class=\"primary\" disabled>Convert</button>
    </div>

    <div id=\"status\" class=\"status\"></div>

    <footer>Local conversion via fontTools — files never leave your machine.</footer>
  </div>

<script>
  const drop = document.getElementById('drop');
  const fileInput = document.getElementById('file');
  const listEl = document.getElementById('list');
  const goBtn = document.getElementById('go');
  const forceEl = document.getElementById('force');
  const statusEl = document.getElementById('status');

  let staged = [];

  function fmtSize(n) {
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    return (n / (1024 * 1024)).toFixed(2) + ' MB';
  }

  function render() {
    listEl.innerHTML = '';
    staged.forEach((f, i) => {
      const row = document.createElement('div');
      row.className = 'row';
      row.innerHTML = `
        <div class=\"name\" title=\"${f.name}\">${f.name}</div>
        <div style=\"display:flex;align-items:center;gap:14px\">
          <div class=\"size\">${fmtSize(f.size)}</div>
          <button data-i=\"${i}\" title=\"Remove\">remove</button>
        </div>
      `;
      listEl.appendChild(row);
    });
    listEl.querySelectorAll('button').forEach(btn => {
      btn.addEventListener('click', e => {
        const i = parseInt(e.currentTarget.getAttribute('data-i'), 10);
        staged.splice(i, 1);
        render();
        updateGo();
      });
    });
  }

  function updateGo() {
    goBtn.disabled = staged.length === 0;
  }

  function addFiles(files) {
    for (const f of files) {
      const lower = f.name.toLowerCase();
      if (!(lower.endsWith('.woff') || lower.endsWith('.woff2'))) continue;
      if (staged.some(s => s.name === f.name && s.size === f.size)) continue;
      staged.push(f);
    }
    render();
    updateGo();
  }

  drop.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', e => {
    addFiles(e.target.files);
    fileInput.value = '';
  });

  ['dragenter', 'dragover'].forEach(ev =>
    drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('drag'); }));
  ['dragleave', 'drop'].forEach(ev =>
    drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('drag'); }));
  drop.addEventListener('drop', e => addFiles(e.dataTransfer.files));

  goBtn.addEventListener('click', async () => {
    if (staged.length === 0) return;
    goBtn.disabled = true;
    statusEl.className = 'status';
    statusEl.textContent = 'Converting ' + staged.length + ' file' + (staged.length === 1 ? '' : 's') + '…';

    const fd = new FormData();
    staged.forEach(f => fd.append('files', f, f.name));
    if (forceEl.checked) fd.append('force_otf', '1');

    try {
      const res = await fetch('/convert', { method: 'POST', body: fd });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || ('HTTP ' + res.status));
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'fonts_converted.zip';
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      statusEl.className = 'status ok';
      statusEl.textContent = 'Done — zip downloaded.';
    } catch (err) {
      statusEl.className = 'status err';
      statusEl.textContent = 'Failed: ' + err.message;
    } finally {
      goBtn.disabled = staged.length === 0;
    }
  });
</script>
</body>
</html>
"""


def _convert_one(data: bytes, original_name: str, force_otf: bool):
    """Return (output_bytes, output_filename, outline_type) or raise."""
    font = TTFont(io.BytesIO(data))
    font.flavor = None  # decompress wrapper

    if "CFF " in font or "CFF2" in font:
        outline = "CFF2" if "CFF2" in font else "CFF"
        ext = ".otf"
    elif "glyf" in font:
        outline = "glyf"
        ext = ".ttf"
    else:
        raise TTLibError("no recognizable outline table (CFF/CFF2/glyf)")

    if force_otf:
        ext = ".otf"

    stem = os.path.splitext(os.path.basename(original_name))[0] or "font"
    out_name = stem + ext

    buf = io.BytesIO()
    font.save(buf)
    return buf.getvalue(), out_name, outline


def _unique(name: str, taken: set) -> str:
    if name not in taken:
        taken.add(name)
        return name
    stem, ext = os.path.splitext(name)
    i = 2
    while True:
        cand = f"{stem}_{i}{ext}"
        if cand not in taken:
            taken.add(cand)
            return cand
        i += 1


@app.route("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html; charset=utf-8")


@app.route("/convert", methods=["POST"])
def convert():
    files = request.files.getlist("files")
    if not files:
        return ("No files uploaded.", 400)

    force_otf = request.form.get("force_otf") in ("1", "true", "on", "yes")

    zip_buf = io.BytesIO()
    report_lines = []
    taken = set()
    ok_count = 0
    fail_count = 0

    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fs in files:
            in_name = fs.filename or "upload"
            try:
                raw = fs.read()
                if not raw:
                    raise TTLibError("empty file")
                out_bytes, out_name, outline = _convert_one(raw, in_name, force_otf)
                out_name = _unique(out_name, taken)
                zf.writestr(out_name, out_bytes)
                report_lines.append(
                    f"OK    {in_name}  ->  {out_name}   (outline: {outline})"
                )
                ok_count += 1
            except Exception as e:
                report_lines.append(f"SKIP  {in_name}  ->  ERROR: {e}")
                fail_count += 1

        header = [
            "Fontstealer conversion report",
            f"Inputs: {len(files)}    Converted: {ok_count}    Skipped: {fail_count}",
            f"force_otf: {force_otf}",
            "-" * 60,
        ]
        report = "\n".join(header + report_lines) + "\n"
        zf.writestr("_conversion_report.txt", report)

    zip_buf.seek(0)
    return send_file(
        zip_buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name="fonts_converted.zip",
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
