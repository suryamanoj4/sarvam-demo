# Pustakam

Scan a page from any book and read it in your language.

Pustakam is a small web app built on Sarvam AI. Point it at a photo of a book page in any Indian language, and it gives you back clean, translated text that can read itself out loud.

1. **Extract** — Sarvam Vision digitises the page (JPG, PNG, TIFF or PDF; several pages at once), detects the language, and returns the text in reading order.
2. **Check and edit** — the extracted text is editable in place, so you can fix anything Vision misread before translating.
3. **Translate** — Sarvam-105B translates faithfully into your language, with an optional summary and a "simple words" mode for children and elders.
4. **Listen, copy or download** — Bulbul v3 narrates either the original page or the translation; copy the text, or download it as Markdown.

The uploaded page is streamed to Sarvam in memory, never written to disk, and the app purges its upload cache after every run.

## Stack

- [Reflex](https://reflex.dev) 0.10 for the whole app (Python UI; client–server by design)
- [sarvamai](https://pypi.org/project/sarvamai/) Python SDK, plus one raw HTTP call (see notes)
- bun ≥ 1.4 (preferred) or npm for Reflex's frontend dependencies

## Setup

```bash
git clone https://github.com/suryamanoj4/sarvam-demo.git
cd sarvam-demo

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # or: uv pip install -r requirements.txt

echo "SARVAM_API_KEY=your-key-here" > .env

reflex run
```

Open http://localhost:3000. Get an API key at [dashboard.sarvam.ai](https://dashboard.sarvam.ai).

If bun is missing or older than 1.4, Reflex falls back to npm when you run `REFLEX_USE_NPM=1 reflex run` once; adding `bun_path` to `rxconfig.py` points it at an existing bun.

## Languages

- **Translate into:** English, Hindi, Telugu, Tamil, Kannada, Malayalam, Marathi, Bengali, Gujarati, Punjabi, Odia
- **Read from:** auto-detect across 22 Indian languages plus English

## Repo map

| File | What it is |
| --- | --- |
| `pustakam.py` | The whole app: pipeline helpers, Reflex state, UI |
| `rxconfig.py` | Reflex configuration |
| `chat.py` | Minimal Sarvam-105B chat completion example (`python chat.py`) |
| `.env` | Your API key (gitignored — never committed) |

## Implementation notes

A few things that were not obvious while building this, in case you are wiring the same APIs:

- Sarvam Vision digitise is a job. Upload the file, start `digitise`, poll `get_status` until a terminal state, then fetch results. The SDK's typed results parser expects `pages[].content`, but the API actually returns `pages[].blocks[]` (each with `text`, `layout_tag` and `reading_order`), so Pustakam fetches `…/results?format=json` directly.
- `digitise(file=...)` needs explicit MIME types, i.e. `(name, bytes, mime)` tuples. A bare file path gets uploaded as `text/plain` and rejected.
- Sarvam-105B is a reasoning model. Give it `reasoning_effort="low"` and enough `max_tokens` (2048+), or the reasoning budget can eat the whole response and leave the content empty.
- Bulbul v3 accepts only the base speaker names (`kavitha`, `ritu`, `simran`, …), not the language-styled ones like `kavitha_te_narration`.
- Narration is capped at 1,200 characters per run to keep the demo snappy.
- Why not call Sarvam directly from the browser? Two reasons: the API key would be exposed to every visitor, and Sarvam's presigned upload host (Azure Blob) sends no CORS headers, so browser-side uploads are blocked anyway. The Reflex server holds the key and does the uploads.

## Limitations

- Printed pages read far better than handwriting.
- Phone photos must be JPG/PNG/TIFF/PDF. Convert HEIC before uploading.
- One page per scan is the happy path; multi-page works but reads sequentially.
