import io
import json
import os
import zipfile
from flask import Flask, request, send_file, Response, jsonify
from fontTools.ttLib import TTFont, TTLibError, TTCollection

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024 * 1024


def _get_name(font, *ids):
    for nid in ids:
        try:
            val = font["name"].getDebugName(nid)
            if val:
                return val.strip()
        except Exception:
            pass
    return None


def _inspect_one(data: bytes, filename: str) -> dict:
    lower = os.path.basename(filename).lower()
    kw = {"lazy": True}
    if lower.endswith(".ttc") or lower.endswith(".otc"):
        kw["fontNumber"] = 0
    font = TTFont(io.BytesIO(data), **kw)
    family = _get_name(font, 16, 1) or os.path.splitext(os.path.basename(filename))[0]
    subfamily = _get_name(font, 17, 2) or "Regular"
    if "CFF " in font or "CFF2" in font:
        outline = "CFF2" if "CFF2" in font else "CFF"
    elif "glyf" in font:
        outline = "glyf"
    else:
        outline = "unknown"
    is_variable = False
    axes = []
    try:
        is_variable = "fvar" in font
        if is_variable:
            for axis in font["fvar"].axes:
                axes.append({
                    "tag": axis.axisTag,
                    "min": float(axis.minValue),
                    "max": float(axis.maxValue),
                    "default": float(axis.defaultValue),
                })
    except Exception:
        is_variable = False
    weight_class = None
    try:
        if "OS/2" in font:
            weight_class = int(font["OS/2"].usWeightClass)
    except Exception:
        pass
    return {
        "filename": filename,
        "family": family,
        "subfamily": subfamily,
        "outline": outline,
        "is_variable": is_variable,
        "axes": axes,
        "weight_class": weight_class,
    }


def _convert_one(data: bytes, original_name: str, target_format: str = "auto"):
    font = TTFont(io.BytesIO(data))
    if "CFF " in font or "CFF2" in font:
        outline = "CFF2" if "CFF2" in font else "CFF"
        native_ext = ".otf"
    elif "glyf" in font:
        outline = "glyf"
        native_ext = ".ttf"
    else:
        raise TTLibError("no recognizable outline table (CFF/CFF2/glyf)")
    stem = os.path.splitext(os.path.basename(original_name))[0] or "font"
    if target_format == "woff":
        font.flavor = "woff"
        ext = ".woff"
    elif target_format == "woff2":
        font.flavor = "woff2"
        ext = ".woff2"
    else:
        font.flavor = None
        ext = {"otf": ".otf", "ttf": ".ttf"}.get(target_format, native_ext)
    buf = io.BytesIO()
    font.save(buf)
    return buf.getvalue(), stem + ext, outline


def _convert_ttc(data: bytes, original_name: str, target_format: str = "auto"):
    coll = TTCollection(io.BytesIO(data))
    stem = os.path.splitext(os.path.basename(original_name))[0] or "font"
    for i, font in enumerate(coll.fonts):
        if "CFF " in font or "CFF2" in font:
            outline = "CFF2" if "CFF2" in font else "CFF"
            native_ext = ".otf"
        elif "glyf" in font:
            outline = "glyf"
            native_ext = ".ttf"
        else:
            continue
        if target_format == "woff":
            font.flavor = "woff"
            ext = ".woff"
        elif target_format == "woff2":
            font.flavor = "woff2"
            ext = ".woff2"
        else:
            font.flavor = None
            ext = {"otf": ".otf", "ttf": ".ttf"}.get(target_format, native_ext)
        buf = io.BytesIO()
        font.save(buf)
        yield buf.getvalue(), f"{stem}_{i + 1}{ext}", outline


def _unique(name: str, taken: set) -> str:
    if name not in taken:
        taken.add(name)
        return name
    base, ext = os.path.splitext(name)
    i = 2
    while True:
        cand = f"{base}_{i}{ext}"
        if cand not in taken:
            taken.add(cand)
            return cand
        i += 1


def _write_converted(fs, target_format, taken, zf, report_lines, prefix=""):
    in_name = fs.filename or "upload"
    ok = fail = 0
    try:
        raw = fs.read()
        if not raw:
            raise TTLibError("empty file")
        lower = in_name.lower()
        if lower.endswith(".ttc") or lower.endswith(".otc"):
            for out_bytes, out_name, outline in _convert_ttc(raw, in_name, target_format):
                arc = _unique(prefix + out_name, taken)
                zf.writestr(arc, out_bytes)
                report_lines.append(f"OK    {in_name}  ->  {arc}   (outline: {outline})")
                ok += 1
        else:
            out_bytes, out_name, outline = _convert_one(raw, in_name, target_format)
            arc = _unique(prefix + out_name, taken)
            zf.writestr(arc, out_bytes)
            report_lines.append(f"OK    {in_name}  ->  {arc}   (outline: {outline})")
            ok += 1
    except Exception as e:
        report_lines.append(f"SKIP  {in_name}  ->  ERROR: {e}")
        fail += 1
    return ok, fail


@app.route("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html; charset=utf-8")


@app.route("/inspect", methods=["POST"])
def inspect_fonts():
    files = request.files.getlist("files")
    results = []
    for fs in files:
        fn = fs.filename or "upload"
        try:
            raw = fs.read()
            if not raw:
                raise ValueError("empty file")
            results.append(_inspect_one(raw, fn))
        except Exception as e:
            results.append({
                "filename": fn,
                "family": os.path.splitext(os.path.basename(fn))[0],
                "subfamily": "Unknown",
                "outline": "unknown",
                "is_variable": False,
                "axes": [],
                "weight_class": None,
                "error": str(e),
            })
    return jsonify(results)


@app.route("/convert", methods=["POST"])
def convert():
    files = request.files.getlist("files")
    if not files:
        return ("No files uploaded.", 400)
    target_format = request.form.get("target_format", "auto").lower()
    if target_format not in ("auto", "otf", "ttf", "woff", "woff2"):
        target_format = "auto"
    groups_json = request.form.get("groups", "")
    groups = {}
    try:
        if groups_json:
            groups = json.loads(groups_json)
    except Exception:
        pass
    zip_name = (request.form.get("zip_name") or "fonts_converted").replace("/", "_").replace("\\", "_")
    file_map = {}
    for fs in files:
        fn = fs.filename or f"upload_{len(file_map)}"
        file_map[fn] = fs
    zip_buf = io.BytesIO()
    report_lines = []
    taken = set()
    ok_count = fail_count = 0
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if groups:
            for folder_name, filenames in groups.items():
                folder = (folder_name.strip() or "Ungrouped").replace("/", "_")
                prefix = folder + "/"
                for fn in filenames:
                    fs = file_map.get(fn)
                    if fs is None:
                        report_lines.append(f"SKIP  {fn}  ->  ERROR: not found in upload")
                        fail_count += 1
                        continue
                    ok, fail = _write_converted(fs, target_format, taken, zf, report_lines, prefix)
                    ok_count += ok
                    fail_count += fail
        else:
            for fs in files:
                ok, fail = _write_converted(fs, target_format, taken, zf, report_lines)
                ok_count += ok
                fail_count += fail
        header = [
            "Fontpeddler conversion report",
            f"Inputs: {len(files)}    Converted: {ok_count}    Skipped: {fail_count}",
            f"target_format: {target_format}",
            "-" * 60,
        ]
        zf.writestr("_conversion_report.txt", "\n".join(header + report_lines) + "\n")
    zip_buf.seek(0)
    return send_file(zip_buf, mimetype="application/zip", as_attachment=True, download_name=zip_name + ".zip")


INDEX_HTML = """<!doctype html>
<html lang='en'>
<head>
<meta charset='utf-8' />
<meta name='viewport' content='width=device-width, initial-scale=1' />
<title>Fontpeddler</title>
<style>
  :root {
    --bg: #F0F0F0;
    --panel: #FFFFFF;
    --panel-2: #FAFAFA;
    --text: #3D3D3D;
    --muted: #888888;
    --accent: #4F6BFF;
    --accent-h: #3450E0;
    --border: #D8D8D8;
    --danger: #D32F2F;
    --ok: #388E3C;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body {
    background: var(--bg);
    color: var(--text);
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    min-height: 100vh;
  }
  .wrap { max-width: 820px; margin: 0 auto; padding: 48px 24px 80px; }
  h1 { font-size: 26px; font-weight: 700; letter-spacing: -.02em; margin-bottom: 6px; }
  .sub { color: var(--muted); margin-bottom: 28px; font-size: 14px; }

  /* Drop zone */
  .drop {
    background: var(--panel);
    border: 2px dashed var(--border);
    border-radius: 14px;
    padding: 44px 24px;
    text-align: center;
    cursor: pointer;
    transition: border-color .15s, background .15s;
    user-select: none;
  }
  .drop.drag { border-color: var(--accent); background: #EEF1FF; }
  .drop strong { font-size: 15px; display: block; margin-bottom: 6px; color: var(--text); }
  .drop-sub { color: var(--muted); font-size: 13px; }
  .browse-link { color: var(--accent); text-decoration: underline; text-underline-offset: 2px; }
  input[type='file'] { display: none; }

  /* Format row */
  .format-row {
    display: flex; align-items: center; gap: 14px; margin-top: 16px; flex-wrap: wrap;
  }
  .format-label { color: var(--muted); font-size: 12px; white-space: nowrap; text-transform: uppercase; letter-spacing: .04em; }
  .format-options { display: flex; gap: 6px; flex-wrap: wrap; }
  .fmt-opt {
    display: inline-flex; align-items: center; gap: 5px;
    background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
    padding: 5px 12px; cursor: pointer; font-size: 13px; color: var(--text);
    transition: border-color .12s, background .12s, color .12s;
  }
  .fmt-opt input { display: none; }
  .fmt-opt.active { border-color: var(--accent); background: #EEF1FF; color: var(--accent); font-weight: 600; }

  /* Groups */
  #groups { margin-top: 20px; display: flex; flex-direction: column; gap: 12px; }

  /* Card */
  .card { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; overflow: hidden; }
  .card-header {
    display: flex; align-items: center; justify-content: space-between; gap: 12px;
    padding: 13px 16px; border-bottom: 1px solid var(--border); background: var(--panel-2);
  }
  .card-name-wrap { display: flex; align-items: center; gap: 10px; flex: 1; min-width: 0; }
  .zip-name {
    border: 1px solid var(--border); border-radius: 7px; padding: 5px 10px;
    font: 600 14px/1 inherit; color: var(--text); background: var(--panel);
    min-width: 0; flex: 1; max-width: 280px;
  }
  .zip-name:focus { outline: none; border-color: var(--accent); }
  .count-badge {
    background: #EBEBEB; color: var(--muted); border-radius: 20px;
    padding: 2px 9px; font-size: 11px; white-space: nowrap;
  }

  /* File rows */
  .file-row {
    display: flex; align-items: center; justify-content: space-between; gap: 8px;
    padding: 9px 16px; border-top: 1px solid #F0F0F0;
  }
  .file-row:first-child { border-top: none; }
  .file-row-left { display: flex; align-items: center; gap: 8px; min-width: 0; flex: 1; }
  .file-name { font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 280px; }
  .file-row-right { display: flex; align-items: center; gap: 6px; flex-shrink: 0; }
  .file-size { color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; white-space: nowrap; }

  /* Badges */
  .badge {
    background: #EEF0FF; color: #3A52E8; border-radius: 20px;
    padding: 2px 8px; font-size: 11px; white-space: nowrap; flex-shrink: 0;
  }
  .badge-var { background: #FFF3E0; color: #E65100; }

  /* Buttons */
  .btn-primary {
    background: var(--accent); color: white; border: none; border-radius: 9px;
    padding: 10px 22px; font: 600 14px inherit; cursor: pointer; transition: background .12s; white-space: nowrap;
  }
  .btn-primary:hover { background: var(--accent-h); }
  .btn-primary:disabled { opacity: .45; cursor: not-allowed; }
  .btn-secondary {
    background: var(--panel); color: var(--text); border: 1px solid var(--border);
    border-radius: 8px; padding: 6px 14px; font: 13px inherit; cursor: pointer;
    transition: border-color .12s, color .12s; white-space: nowrap;
  }
  .btn-secondary:hover { border-color: var(--accent); color: var(--accent); }
  .icon-btn {
    background: transparent; border: none; cursor: pointer; color: var(--muted);
    font-size: 16px; width: 28px; height: 28px; display: flex; align-items: center;
    justify-content: center; border-radius: 6px; transition: color .12s, background .12s; flex-shrink: 0;
  }
  .icon-btn:hover { color: var(--text); background: #EBEBEB; }
  .remove-btn:hover { color: var(--danger); background: #FFF0F0; }
  .preview-btn:hover { color: var(--accent); background: #EEF1FF; }

  /* Bottom bar */
  .bottom-bar { margin-top: 20px; display: flex; align-items: center; justify-content: space-between; gap: 16px; }
  .status { font-size: 13px; color: var(--muted); }
  .status.ok { color: var(--ok); }
  .status.err { color: var(--danger); }
  footer { margin-top: 48px; color: var(--muted); font-size: 12px; text-align: center; }

  /* Preview panel */
  .preview-panel {
    position: fixed; top: 0; right: 0; width: 380px; height: 100vh;
    background: var(--panel); border-left: 1px solid var(--border);
    box-shadow: -6px 0 32px rgba(0,0,0,.1);
    transform: translateX(100%); transition: transform .25s cubic-bezier(.4,0,.2,1);
    z-index: 100; display: flex; flex-direction: column; overflow: hidden;
  }
  .preview-panel.open { transform: translateX(0); }
  .preview-header {
    display: flex; align-items: flex-start; justify-content: space-between;
    padding: 20px 20px 16px; border-bottom: 1px solid var(--border); flex-shrink: 0;
  }
  .preview-family-name { font-size: 16px; font-weight: 700; }
  .preview-subfamily { font-size: 12px; color: var(--muted); margin-top: 3px; }
  .preview-loading { padding: 10px 20px; color: var(--muted); font-size: 13px; display: none; }
  .preview-text-wrap {
    flex: 1; padding: 24px; overflow-y: auto;
    display: flex; align-items: center; justify-content: center;
    background: var(--panel-2); min-height: 140px;
  }
  .preview-text {
    font-size: 48px; line-height: 1.25; color: var(--text);
    text-align: center; word-break: break-word; width: 100%;
    transition: font-size .1s;
  }
  .preview-controls {
    padding: 14px 20px; border-top: 1px solid var(--border);
    display: flex; flex-direction: column; gap: 10px; flex-shrink: 0;
  }
  .ctrl-input {
    width: 100%; border: 1px solid var(--border); border-radius: 8px;
    padding: 7px 11px; font: inherit; font-size: 13px; color: var(--text); background: var(--panel);
  }
  .ctrl-input:focus { outline: none; border-color: var(--accent); }
  .slider-row { display: flex; align-items: center; gap: 10px; }
  .slider-label { font-size: 12px; color: var(--muted); white-space: nowrap; min-width: 72px; }
  .slider-row input[type='range'] { flex: 1; accent-color: var(--accent); }
  .weight-tabs { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 2px; }
  .weight-tab {
    background: var(--panel); border: 1px solid var(--border); border-radius: 7px;
    padding: 4px 10px; font: 12px inherit; cursor: pointer; color: var(--text);
    transition: border-color .12s, background .12s, color .12s;
  }
  .weight-tab:hover { border-color: var(--accent); color: var(--accent); }
  .weight-tab.active { border-color: var(--accent); background: #EEF1FF; color: var(--accent); font-weight: 600; }
  .preview-meta {
    display: flex; gap: 16px; align-items: center; padding: 10px 20px;
    border-top: 1px solid var(--border); font-size: 11px; color: var(--muted); flex-shrink: 0; flex-wrap: wrap;
  }
  .preview-meta span { color: var(--text); font-weight: 500; }
</style>
</head>
<body>

<div id='preview-panel' class='preview-panel'>
  <div class='preview-header'>
    <div>
      <div id='preview-family' class='preview-family-name'></div>
      <div id='preview-subfamily' class='preview-subfamily'></div>
    </div>
    <button id='close-preview' class='icon-btn' title='Close'>&#215;</button>
  </div>
  <div id='preview-loading' class='preview-loading'>Loading font&#8230;</div>
  <div class='preview-text-wrap'>
    <div id='preview-text' class='preview-text'>AaBbCcDd 123</div>
  </div>
  <div class='preview-controls'>
    <input id='preview-input' class='ctrl-input' type='text' value='AaBbCcDd 123' placeholder='Type preview text' />
    <div class='slider-row'>
      <span class='slider-label'>Size <span id='size-val'>48px</span></span>
      <input id='size-slider' type='range' min='8' max='120' value='48' />
    </div>
    <div id='weight-tabs' class='weight-tabs'></div>
  </div>
  <div class='preview-meta'>
    <div>Outline: <span id='meta-outline'></span></div>
    <div>File: <span id='meta-size'></span></div>
    <span id='meta-variable' class='badge badge-var' style='display:none'>Variable</span>
  </div>
</div>

<div class='wrap'>
  <h1>Fontpeddler</h1>
  <p class='sub'>Convert fonts between OTF, TTF, WOFF and WOFF2. Drop files below &mdash; they group by family automatically.</p>

  <div id='drop' class='drop'>
    <strong>Drop font files here</strong>
    <div class='drop-sub'>OTF &middot; TTF &middot; WOFF &middot; WOFF2 &middot; TTC &nbsp;&mdash;&nbsp; or <span class='browse-link'>click to browse</span></div>
    <input id='file' type='file' accept='.otf,.ttf,.woff,.woff2,.ttc,.otc' multiple />
  </div>

  <div class='format-row' id='format-row' style='display:none'>
    <span class='format-label'>Output</span>
    <div class='format-options' id='fmt-options'>
      <label class='fmt-opt active'><input type='radio' name='fmt' value='auto' checked />Auto</label>
      <label class='fmt-opt'><input type='radio' name='fmt' value='otf' />OTF</label>
      <label class='fmt-opt'><input type='radio' name='fmt' value='ttf' />TTF</label>
      <label class='fmt-opt'><input type='radio' name='fmt' value='woff' />WOFF</label>
      <label class='fmt-opt'><input type='radio' name='fmt' value='woff2' />WOFF2</label>
    </div>
  </div>

  <div id='groups'></div>

  <div class='bottom-bar' id='bottom-bar' style='display:none'>
    <div id='status' class='status'></div>
    <button id='export-all' class='btn-primary' disabled>Export All</button>
  </div>

  <footer>Runs locally via fontTools &mdash; your fonts never leave this machine.</footer>
</div>

<script>
'use strict';
const $ = id => document.getElementById(id);

const state = {
  staged: new Map(),    // filename -> {file, meta}
  groups: new Map(),    // family  -> {zipName, files:[filename]}
  preview: null,
  fontFaces: new Map(), // filename -> fontFamily string
};

const VALID = ['.otf','.ttf','.woff','.woff2','.ttc','.otc'];

function fmtSize(n) {
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1048576).toFixed(2) + ' MB';
}

function getFormat() {
  const el = document.querySelector('input[name=fmt]:checked');
  return el ? el.value : 'auto';
}

// ── Format pill active state ──────────────────────────────────────
document.querySelectorAll('.fmt-opt input').forEach(radio => {
  radio.addEventListener('change', () => {
    document.querySelectorAll('.fmt-opt').forEach(l => l.classList.remove('active'));
    radio.closest('.fmt-opt').classList.add('active');
  });
});

// ── File ingestion ────────────────────────────────────────────────
async function addFiles(fileList) {
  const newFiles = [];
  for (const f of fileList) {
    const lower = f.name.toLowerCase();
    if (!VALID.some(e => lower.endsWith(e))) continue;
    if (state.staged.has(f.name)) continue;
    state.staged.set(f.name, {file: f, meta: null});
    newFiles.push(f);
  }
  if (!newFiles.length) return;
  $('format-row').style.display = 'flex';
  $('bottom-bar').style.display = 'flex';
  await inspectFiles(newFiles);
}

async function inspectFiles(files) {
  const fd = new FormData();
  files.forEach(f => fd.append('files', f, f.name));
  try {
    const res = await fetch('/inspect', {method: 'POST', body: fd});
    const results = await res.json();
    for (const meta of results) {
      const entry = state.staged.get(meta.filename);
      if (entry) entry.meta = meta;
    }
  } catch(e) { console.error('inspect failed', e); }
  rebuildGroups();
  renderGroups();
}

// ── Grouping ──────────────────────────────────────────────────────
function rebuildGroups() {
  const oldNames = new Map([...state.groups].map(([k, v]) => [k, v.zipName]));
  const ng = new Map();
  for (const [filename, {meta}] of state.staged) {
    const fam = (meta && !meta.error) ? meta.family : filename;
    if (!ng.has(fam)) ng.set(fam, {zipName: oldNames.get(fam) || fam, files: []});
    ng.get(fam).files.push(filename);
  }
  state.groups = ng;
}

function renderGroups() {
  const el = $('groups');
  el.innerHTML = '';
  const has = state.staged.size > 0;
  el.style.display = has ? 'flex' : 'none';
  $('export-all').disabled = !has;
  if (!has) return;
  for (const [fam, group] of state.groups) el.appendChild(buildCard(fam, group));
}

function buildCard(fam, group) {
  const card = document.createElement('div');
  card.className = 'card';

  const hdr = document.createElement('div');
  hdr.className = 'card-header';

  const nw = document.createElement('div');
  nw.className = 'card-name-wrap';

  const inp = document.createElement('input');
  inp.type = 'text'; inp.className = 'zip-name'; inp.value = group.zipName;
  inp.title = 'Name for exported zip';
  inp.addEventListener('input', () => { state.groups.get(fam).zipName = inp.value.trim() || fam; });

  const badge = document.createElement('span');
  badge.className = 'count-badge';
  badge.textContent = group.files.length + (group.files.length === 1 ? ' file' : ' files');

  nw.appendChild(inp); nw.appendChild(badge);

  const expBtn = document.createElement('button');
  expBtn.className = 'btn-secondary'; expBtn.textContent = 'Export';
  expBtn.addEventListener('click', () => exportGroup(fam));

  hdr.appendChild(nw); hdr.appendChild(expBtn);

  const rows = document.createElement('div');
  for (const fn of group.files) {
    const ent = state.staged.get(fn);
    if (ent) rows.appendChild(buildRow(fn, ent.file, ent.meta, fam));
  }

  card.appendChild(hdr); card.appendChild(rows);
  return card;
}

function buildRow(filename, file, meta, fam) {
  const row = document.createElement('div');
  row.className = 'file-row';

  const left = document.createElement('div');
  left.className = 'file-row-left';

  const nameEl = document.createElement('span');
  nameEl.className = 'file-name'; nameEl.title = filename; nameEl.textContent = filename;
  left.appendChild(nameEl);

  if (meta && !meta.error) {
    const b = document.createElement('span');
    b.className = 'badge'; b.textContent = meta.subfamily; left.appendChild(b);
    if (meta.is_variable) {
      const vb = document.createElement('span');
      vb.className = 'badge badge-var'; vb.textContent = 'Variable'; left.appendChild(vb);
    }
  }

  const right = document.createElement('div');
  right.className = 'file-row-right';

  const sz = document.createElement('span');
  sz.className = 'file-size'; sz.textContent = fmtSize(file.size);

  const prev = document.createElement('button');
  prev.className = 'icon-btn preview-btn'; prev.title = 'Preview'; prev.innerHTML = '&#9654;';
  prev.addEventListener('click', () => openPreview(filename, fam));

  const rm = document.createElement('button');
  rm.className = 'icon-btn remove-btn'; rm.title = 'Remove'; rm.innerHTML = '&#215;';
  rm.addEventListener('click', () => { state.staged.delete(filename); if (state.preview === filename) closePreview(); rebuildGroups(); renderGroups(); });

  right.appendChild(sz); right.appendChild(prev); right.appendChild(rm);
  row.appendChild(left); row.appendChild(right);
  return row;
}

// ── Preview panel ─────────────────────────────────────────────────
async function openPreview(filename, fam) {
  state.preview = filename;
  const ent = state.staged.get(filename);
  if (!ent) return;
  const {file, meta} = ent;

  $('preview-panel').classList.add('open');
  $('preview-family').textContent = meta ? meta.family : filename;
  $('preview-subfamily').textContent = meta ? meta.subfamily : '';
  $('meta-outline').textContent = meta ? meta.outline : '';
  $('meta-size').textContent = fmtSize(file.size);
  $('meta-variable').style.display = (meta && meta.is_variable) ? 'inline' : 'none';

  // Load font client-side
  let ff = state.fontFaces.get(filename);
  if (!ff) {
    $('preview-loading').style.display = 'block';
    try {
      const name = 'FS_' + filename.replace(/[^a-zA-Z0-9]/g, '_') + '_' + (Date.now() % 99999);
      const face = new FontFace(name, await file.arrayBuffer());
      await face.load();
      document.fonts.add(face);
      ff = name;
      state.fontFaces.set(filename, ff);
    } catch(e) {
      $('preview-loading').textContent = 'Preview unavailable: ' + e.message;
    }
    $('preview-loading').style.display = 'none';
  }
  if (ff) $('preview-text').style.fontFamily = "'" + ff + "', sans-serif";

  // Weight tabs or variable slider
  const tabsEl = $('weight-tabs');
  tabsEl.innerHTML = '';
  const group = state.groups.get(fam);

  if (meta && meta.is_variable) {
    const wghtAxis = meta.axes.find(a => a.tag === 'wght');
    if (wghtAxis) {
      const lbl = document.createElement('div');
      lbl.className = 'slider-row';
      const valSpan = document.createElement('span');
      valSpan.textContent = Math.round(wghtAxis.default);
      const labelTxt = document.createElement('span');
      labelTxt.className = 'slider-label';
      labelTxt.appendChild(document.createTextNode('Weight '));
      labelTxt.appendChild(valSpan);
      const sl = document.createElement('input');
      sl.type = 'range'; sl.min = wghtAxis.min; sl.max = wghtAxis.max;
      sl.value = wghtAxis.default; sl.step = 1; sl.style.flex = '1'; sl.style.accentColor = 'var(--accent)';
      sl.addEventListener('input', () => {
        valSpan.textContent = sl.value;
        $('preview-text').style.fontVariationSettings = "'wght' " + sl.value;
      });
      lbl.appendChild(labelTxt); lbl.appendChild(sl);
      tabsEl.appendChild(lbl);
    }
  } else if (group && group.files.length > 1) {
    for (const fn of group.files) {
      const e = state.staged.get(fn);
      const tab = document.createElement('button');
      tab.className = 'weight-tab' + (fn === filename ? ' active' : '');
      tab.textContent = (e && e.meta) ? e.meta.subfamily : fn;
      tab.addEventListener('click', () => openPreview(fn, fam));
      tabsEl.appendChild(tab);
    }
  }

  updatePreviewText();
}

function updatePreviewText() {
  const txt = $('preview-input').value || 'AaBbCcDd 123';
  const sz = $('size-slider').value;
  $('preview-text').textContent = txt;
  $('preview-text').style.fontSize = sz + 'px';
  $('size-val').textContent = sz + 'px';
}

function closePreview() {
  $('preview-panel').classList.remove('open');
  state.preview = null;
}

// ── Export ────────────────────────────────────────────────────────
async function exportGroup(fam) {
  const group = state.groups.get(fam);
  if (!group || !group.files.length) return;
  const fd = new FormData();
  group.files.forEach(fn => { const e = state.staged.get(fn); if (e) fd.append('files', e.file, fn); });
  fd.append('target_format', getFormat());
  fd.append('zip_name', group.zipName || fam);
  await doExport(fd, (group.zipName || fam) + '.zip');
}

async function exportAll() {
  if (!state.staged.size) return;
  const fd = new FormData();
  state.staged.forEach(({file}, fn) => fd.append('files', file, fn));
  fd.append('target_format', getFormat());
  const grpObj = {};
  state.groups.forEach((g, fam) => { grpObj[g.zipName || fam] = g.files; });
  fd.append('groups', JSON.stringify(grpObj));
  fd.append('zip_name', 'fonts_converted');
  await doExport(fd, 'fonts_converted.zip');
}

async function doExport(fd, dlName) {
  setStatus('Converting…', '');
  $('export-all').disabled = true;
  try {
    const res = await fetch('/convert', {method: 'POST', body: fd});
    if (!res.ok) throw new Error(await res.text() || 'HTTP ' + res.status);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = dlName;
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
    setStatus('Done — zip downloaded.', 'ok');
  } catch(e) {
    setStatus('Failed: ' + e.message, 'err');
  } finally {
    $('export-all').disabled = !state.staged.size;
  }
}

function setStatus(msg, type) {
  const el = $('status');
  el.textContent = msg;
  el.className = 'status' + (type ? ' ' + type : '');
}

// ── Events ────────────────────────────────────────────────────────
const drop = $('drop');
const fileInput = $('file');

drop.addEventListener('click', e => { if (!e.target.closest('input[type=file]')) fileInput.click(); });
fileInput.addEventListener('change', e => { addFiles(e.target.files); fileInput.value = ''; });
['dragenter','dragover'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('drag'); }));
['dragleave','drop'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('drag'); }));
drop.addEventListener('drop', e => addFiles(e.dataTransfer.files));

$('export-all').addEventListener('click', exportAll);
$('close-preview').addEventListener('click', closePreview);
$('preview-input').addEventListener('input', updatePreviewText);
$('size-slider').addEventListener('input', updatePreviewText);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
