# Fontpeddler

Local Flask web app that bulk-converts **WOFF** / **WOFF2** font files to **OTF** / **TTF** and returns them in a zip.

- Outline-aware: `.otf` when the font has CFF/CFF2 tables, `.ttf` when it has a `glyf` table.
- Optional `Force every file to .otf` toggle in the UI / `force_otf` form flag on the API.
- Broken files are skipped with a per-file note in `_conversion_report.txt` inside the zip.
- Filename collisions are de-duplicated (`Foo.otf`, `Foo_2.otf`, ...).
- Single-page dark UI with drag-and-drop and click-to-browse.

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Then open <http://127.0.0.1:5000/>.

## API

`POST /convert` — multipart form:

- `files`: one or more `.woff` / `.woff2` uploads (field repeated).
- `force_otf` (optional): `1` / `true` / `on` to force `.otf` extension regardless of outline type.

Returns `application/zip` containing the converted fonts plus `_conversion_report.txt`.
