"""Pustakam - scan any book page, read it in your language.

Pipeline: Sarvam Vision (digitise) -> Sarvam-105B (fix OCR + translate [+ summary]) -> Bulbul v3 (narration).
Run: reflex run
"""

import asyncio
import base64
import json
import mimetypes
import os
import shutil
import time
from pathlib import Path

import httpx
import reflex as rx
from sarvamai import SarvamAI

# --- load .env (stdlib only) ---
env_path = Path(__file__).with_name(".env")
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

client = SarvamAI(api_subscription_key=os.environ["SARVAM_API_KEY"])

TARGETS = {
    "en-IN": {"name": "English", "speaker": "ishita"},
    "hi-IN": {"name": "Hindi", "speaker": "ritu"},
    "te-IN": {"name": "Telugu", "speaker": "kavitha"},
    "ta-IN": {"name": "Tamil", "speaker": "vijay"},
    "kn-IN": {"name": "Kannada", "speaker": "shruti"},
    "mr-IN": {"name": "Marathi", "speaker": "rupali"},
    "bn-IN": {"name": "Bengali", "speaker": "roopa"},
    "gu-IN": {"name": "Gujarati", "speaker": "priya"},
    "pa-IN": {"name": "Punjabi", "speaker": "simran"},
    "ml-IN": {"name": "Malayalam", "speaker": "kavya"},
    "od-IN": {"name": "Odia", "speaker": "suhani"},
}

SOURCE_ITEMS = [
    ("Auto (Indic default)", "auto"), ("English", "en-IN"), ("Hindi", "hi-IN"),
    ("Telugu", "te-IN"), ("Tamil", "ta-IN"), ("Kannada", "kn-IN"),
    ("Malayalam", "ml-IN"), ("Marathi", "mr-IN"), ("Bengali", "bn-IN"),
    ("Gujarati", "gu-IN"), ("Punjabi", "pa-IN"), ("Odia", "od-IN"),
]
TARGET_ITEMS = [
    (v["name"] + ("" if v["speaker"] else " (no audio)"), c) for c, v in TARGETS.items()
]
SOURCE_CODE = {label: code for label, code in SOURCE_ITEMS}
TARGET_CODE = {label: code for label, code in TARGET_ITEMS}
LANG_NAME = {code: label for label, code in SOURCE_ITEMS if code != "auto"}

TERMINAL = {"completed", "partially_completed", "failed", "rejected"}


def _digitise(entries: list[tuple[str, bytes]], source_lang: str) -> str:
    file_tuples = [
        (Path(name).name, data, mimetypes.guess_type(name)[0] or "image/jpeg")
        for name, data in entries
    ]
    kwargs = {"file": file_tuples, "content_type": "printed"}
    if source_lang != "auto":
        kwargs["language"] = source_lang
    job = client.doc_ai.digitise(**kwargs)
    status = None
    for _ in range(90):
        status = client.doc_ai.get_status(job.job_id).status
        if status in TERMINAL:
            break
        time.sleep(2)
    if status not in ("completed", "partially_completed"):
        raise RuntimeError(f"Vision job ended with status: {status}")
    r = httpx.get(
        f"https://api.sarvam.ai/doc-ai/v1/job/{job.job_id}/results",
        params={"format": "json"},
        headers={"api-subscription-key": os.environ["SARVAM_API_KEY"]},
        timeout=120,
    )
    r.raise_for_status()
    payload = r.json()
    pages = []
    doc_language = ""
    for doc in payload.get("documents") or []:
        if not doc_language and doc.get("language"):
            doc_language = doc["language"]
        for pg in doc.get("pages") or []:
            blocks = sorted(pg.get("blocks") or [], key=lambda b: b.get("reading_order", 0))
            page_text = "\n\n".join(
                (b.get("text") or "").strip() for b in blocks if (b.get("text") or "").strip()
            )
            if page_text:
                pages.append(page_text)
    text = "\n\n".join(pages).strip()
    if not text:
        raise RuntimeError("Vision returned no text")
    return text, doc_language, len(pages)


def _translate(text: str, target_name: str, plain: bool = False) -> str:
    system = (
        "You are an expert translator and copy editor for Indian-language books. "
        "You receive OCR text extracted from a scanned book page. "
        "First silently fix OCR errors (broken words, wrong characters, missing matras, split words) "
        f"using context - never change meaning, never add or omit content. "
        f"Then translate the cleaned text faithfully into {target_name}. "
        "Preserve paragraph breaks and headings. Do NOT keep hard line breaks from the scan - "
        "reflow them into natural sentences and paragraphs. "
    )
    if plain:
        system += (
            "Use simple, everyday words that a child or an elder can follow: short sentences, "
            "common vocabulary, and keep the full meaning. "
        )
    r = client.chat.completions(
        model="sarvam-105b",
        messages=[{"role": "system", "content": system}, {"role": "user", "content": text}],
        temperature=0.2,
        max_tokens=4000,
        reasoning_effort="low",
    )
    return (r.choices[0].message.content or "").strip()


def _summarize(text: str, target_name: str) -> str:
    r = client.chat.completions(
        model="sarvam-105b",
        messages=[
            {
                "role": "system",
                "content": f"Summarize the text in {target_name} as up to 5 short bullet points. Output only the summary.",
            },
            {"role": "user", "content": text},
        ],
        temperature=0.3,
        max_tokens=2048,
        reasoning_effort="low",
    )
    return (r.choices[0].message.content or "").strip()


def _speak(text: str, target: str) -> str:
    speaker = TARGETS[target]["speaker"]
    assert speaker
    r = client.text_to_speech.convert(
        text=text[:1200],
        language_code=target,
        speaker=speaker,
        model="bulbul:v3",
        output_audio_codec="mp3",
    )
    return "data:audio/mpeg;base64," + r.audios[0]


class State(rx.State):
    source_lang: str = "Auto (Indic default)"
    target: str = "English"
    summarize: bool = False
    extracting: bool = False
    translating: bool = False
    speaking: bool = False
    extracted_text: str = ""
    translated_text: str = ""
    summary_text: str = ""
    audio_data_uri: str = ""
    preview_uri: str = ""
    detected_lang: str = ""
    detected_name: str = ""
    plain: bool = False
    status1: str = ""
    status2: str = ""
    status3: str = ""

    @rx.event
    def set_source_lang(self, value: str):
        self.source_lang = value

    @rx.event
    def set_target(self, value: str):
        self.target = value

    @rx.event
    def set_summarize(self, value: bool):
        self.summarize = value

    @rx.event
    def set_plain(self, value: bool):
        self.plain = value

    @rx.event
    def set_extracted(self, value: str):
        self.extracted_text = value

    @rx.event
    async def extract(self, files: list[rx.UploadFile]):
        if not files:
            self.status1 = "Choose a photo of a page first."
            return
        self.extracting = True
        self.extracted_text = self.translated_text = self.summary_text = ""
        self.audio_data_uri = ""
        self.preview_uri = ""
        self.detected_lang = ""
        self.detected_name = ""
        self.status1 = "Reading the page with Sarvam Vision. This can take up to a minute."
        try:
            entries = [(getattr(f, "name", "") or "page.jpg", await f.read()) for f in files]
            first_name, first_data = entries[0]
            first_mime = mimetypes.guess_type(first_name)[0] or ""
            if first_mime.startswith("image/") and len(first_data) <= 3_000_000:
                self.preview_uri = f"data:{first_mime};base64," + base64.b64encode(first_data).decode()
            text, lang, npages = await asyncio.to_thread(_digitise, entries, SOURCE_CODE[self.source_lang])
            self.extracted_text = text
            self.detected_lang = lang
            self.detected_name = LANG_NAME.get(lang, lang)
            pages_bit = "1 page" if npages <= 1 else f"{npages} pages"
            self.status1 = f"Read {pages_bit} and {len(text)} characters. Check the text, then choose a language below."
        except Exception as e:
            self.status1 = f"Vision could not read this page. {e}"
        finally:
            self.extracting = False
            shutil.rmtree("uploaded_files", ignore_errors=True)

    @rx.event
    async def translate(self):
        if not self.extracted_text:
            return
        self.translating = True
        self.status2 = "Cleaning up the extraction and translating with Sarvam-105B."
        try:
            code = TARGET_CODE[self.target]
            name = TARGETS[code]["name"]
            self.translated_text = await asyncio.to_thread(_translate, self.extracted_text, name, self.plain)
            if self.summarize:
                self.summary_text = await asyncio.to_thread(_summarize, self.translated_text, name)
            else:
                self.summary_text = ""
            self.status2 = "Translation ready. Listen to it, or download the text."
        except Exception as e:
            self.status2 = f"Translation failed. {e}"
        finally:
            self.translating = False

    @rx.event
    async def speak(self):
        code = TARGET_CODE[self.target]
        if not self.translated_text or not TARGETS[code]["speaker"]:
            return
        self.speaking = True
        self.status3 = "Recording the narration with Bulbul."
        try:
            self.audio_data_uri = await asyncio.to_thread(_speak, self.translated_text, code)
            self.status3 = "Narration ready."
        except Exception as e:
            self.status3 = f"Narration failed. {e}"
        finally:
            self.speaking = False

    @rx.event
    async def speak_original(self):
        if not self.extracted_text or self.detected_lang not in TARGETS:
            return
        self.speaking = True
        self.status3 = "Recording the original text with Bulbul."
        try:
            self.audio_data_uri = await asyncio.to_thread(_speak, self.extracted_text, self.detected_lang)
            self.status3 = "Original recording ready."
        except Exception as e:
            self.status3 = f"Narration failed. {e}"
        finally:
            self.speaking = False

    @rx.event
    def copy_translated(self):
        if not self.translated_text:
            return
        self.status3 = "Copied the translation to the clipboard."
        return rx.call_script(f"navigator.clipboard.writeText({json.dumps(self.translated_text)})")

    @rx.var
    def original_available(self) -> bool:
        return bool(self.extracted_text) and self.detected_lang in TARGETS

    @rx.var
    def download_data(self) -> str:
        parts = [self.translated_text]
        if self.summary_text:
            parts.append("---\nSummary:\n" + self.summary_text)
        return "\n\n".join(parts)

    @rx.var
    def download_filename(self) -> str:
        return f"pustakam-{TARGET_CODE[self.target]}.md"


def panel_label(text: str) -> rx.Component:
    return rx.text(text, size="2", color="#6B6459")


def reading_panel(value) -> rx.Component:
    return rx.box(
        rx.scroll_area(rx.text(value, white_space="pre-wrap"), max_height="320px", width="100%"),
        background="#FFFFFF",
        border="1px solid #E6DFD0",
        border_radius="8px",
        padding="20px",
        width="100%",
        style={
            "font_family": "Georgia, 'Times New Roman', serif",
            "font_size": "15.5px",
            "line_height": "1.65",
            "color": "#26221D",
        },
    )


def step_header(number: str, title: str) -> rx.Component:
    return rx.hstack(
        rx.text(
            number,
            style={
                "font_family": "Georgia, serif",
                "font_size": "22px",
                "color": "#3451B2",
                "line_height": "1",
            },
        ),
        rx.heading(title, size="5", style={"font_family": "Georgia, serif", "font_weight": "500"}),
        spacing="3",
        align="baseline",
    )


def index() -> rx.Component:
    return rx.theme(
        rx.box(
            rx.container(
                rx.vstack(
                    rx.vstack(
                        rx.heading(
                            "పుస్తకం",
                            size="8",
                            style={
                                "font_family": "Georgia, 'Noto Serif Telugu', serif",
                                "font_weight": "500",
                                "color": "#26221D",
                            },
                        ),
                        rx.text(
                            "Scan a page from any book and read it in your language.",
                            size="4",
                            color="#6B6459",
                        ),
                        spacing="2",
                        align="start",
                    ),
                    rx.box(height="1px", background="#E6DFD0", width="100%"),
                    rx.callout(
                        "Lay the page flat and fill the frame with it. Printed pages read more reliably than handwriting.",
                        icon="lightbulb",
                        color_scheme="gray",
                        size="1",
                    ),
                    rx.card(
                        rx.vstack(
                            step_header("1", "Scan a page"),
                            rx.vstack(
                                panel_label("The page is written in"),
                                rx.select(
                                    [label for label, _ in SOURCE_ITEMS],
                                    value=State.source_lang,
                                    on_change=State.set_source_lang,
                                    width="260px",
                                ),
                                spacing="2",
                                align="start",
                            ),
                            rx.upload(
                                rx.vstack(
                                    rx.icon("scan-line", size=22, color="var(--gray-9)"),
                                    rx.text("Drop photos of the page here, or click to choose"),
                                    rx.text(
                                        "JPG, PNG, TIFF or PDF. Several pages are fine.",
                                        size="1",
                                        color="#6B6459",
                                    ),
                                    rx.selected_files("page_upload"),
                                    spacing="2",
                                    align="center",
                                ),
                                id="page_upload",
                                multiple=True,
                                accept={"image/*": [".png", ".jpg", ".jpeg", ".tif", ".tiff"], "application/pdf": [".pdf"]},
                                border="1px dashed var(--gray-a6)",
                                border_radius="8px",
                                padding="24px",
                                width="100%",
                                cursor="pointer",
                            ),
                            rx.hstack(
                                rx.button(
                                    rx.icon("scan-line", size=16),
                                    "Extract text",
                                    on_click=State.extract(rx.upload_files(upload_id="page_upload")),
                                    disabled=State.extracting,
                                    size="3",
                                ),
                                rx.spinner(loading=State.extracting),
                                spacing="3",
                                align="center",
                            ),
                            rx.text(State.status1, size="2", color="#6B6459"),
                            rx.cond(
                                State.extracted_text != "",
                                rx.vstack(
                                    rx.hstack(
                                        panel_label("Extracted by Sarvam Vision"),
                                        rx.cond(
                                            State.detected_name != "",
                                            rx.badge(State.detected_name, variant="soft", color_scheme="gray"),
                                        ),
                                        rx.cond(
                                            State.original_available,
                                            rx.button(
                                                rx.icon("volume-2", size=14),
                                                "Hear original",
                                                on_click=State.speak_original,
                                                disabled=State.speaking,
                                                variant="soft",
                                                size="1",
                                            ),
                                        ),
                                        spacing="3",
                                        align="center",
                                        wrap="wrap",
                                    ),
                                    rx.flex(
                                        rx.cond(
                                            State.preview_uri != "",
                                            rx.image(
                                                src=State.preview_uri,
                                                max_height="300px",
                                                border="1px solid #E6DFD0",
                                                border_radius="8px",
                                            ),
                                        ),
                                        rx.box(
                                            rx.text_area(
                                                value=State.extracted_text,
                                                on_change=State.set_extracted,
                                                min_height="200px",
                                                width="100%",
                                                background="#FFFFFF",
                                                color="#26221D",
                                                border="1px solid #E6DFD0",
                                                border_radius="8px",
                                                padding="16px",
                                                style={
                                                    "font_family": "Georgia, 'Times New Roman', serif",
                                                    "font_size": "15.5px",
                                                    "line_height": "1.65",
                                                },
                                            ),
                                            flex="1",
                                            min_width="280px",
                                        ),
                                        wrap="wrap",
                                        spacing="4",
                                        align="start",
                                        width="100%",
                                    ),
                                    rx.text(
                                        "Check the text and fix anything Vision misread before translating.",
                                        size="1",
                                        color="#6B6459",
                                    ),
                                    spacing="2",
                                    align="start",
                                    width="100%",
                                ),
                            ),
                            spacing="4",
                            align="start",
                            width="100%",
                        ),
                        width="100%",
                        style={
                            "background": "#FFFFFF",
                            "border": "1px solid #E6DFD0",
                        },
                    ),
                    rx.card(
                        rx.vstack(
                            step_header("2", "Translate and listen"),
                            rx.vstack(
                                panel_label("Translate it into"),
                                rx.select(
                                    [label for label, _ in TARGET_ITEMS],
                                    value=State.target,
                                    on_change=State.set_target,
                                    width="220px",
                                ),
                                spacing="2",
                                align="start",
                            ),
                            rx.hstack(
                                rx.checkbox(
                                    "Add a summary",
                                    checked=State.summarize,
                                    on_change=State.set_summarize,
                                    size="3",
                                ),
                                rx.checkbox(
                                    "Explain in simple words",
                                    checked=State.plain,
                                    on_change=State.set_plain,
                                    size="3",
                                ),
                                rx.button(
                                    rx.icon("languages", size=16),
                                    "Translate",
                                    on_click=State.translate,
                                    disabled=State.translating | (State.extracted_text == ""),
                                    size="3",
                                ),
                                rx.spinner(loading=State.translating),
                                spacing="5",
                                align="center",
                                wrap="wrap",
                            ),
                            rx.text(State.status2, size="2", color="#6B6459"),
                            rx.cond(
                                State.translated_text != "",
                                rx.vstack(
                                    rx.hstack(
                                        rx.button(
                                            rx.icon("volume-2", size=16),
                                            "Read aloud",
                                            on_click=State.speak,
                                            disabled=State.speaking,
                                            variant="soft",
                                            size="3",
                                        ),
                                        rx.button(
                                            rx.icon("copy", size=16),
                                            "Copy text",
                                            on_click=State.copy_translated,
                                            variant="soft",
                                            size="3",
                                        ),
                                        rx.button(
                                            rx.icon("download", size=16),
                                            "Download text",
                                            on_click=rx.download(
                                                data=State.download_data,
                                                filename=State.download_filename,
                                                mime_type="text/markdown",
                                            ),
                                            variant="soft",
                                            size="3",
                                        ),
                                        rx.spinner(loading=State.speaking),
                                        spacing="3",
                                        align="center",
                                        wrap="wrap",
                                    ),
                                    rx.text(State.status3, size="2", color="#6B6459"),
                                    rx.cond(
                                        State.audio_data_uri != "",
                                        rx.el.audio(
                                            src=State.audio_data_uri,
                                            controls=True,
                                            style={"width": "100%"},
                                        ),
                                    ),
                                    rx.vstack(
                                        panel_label("Translated by Sarvam-105B"),
                                        reading_panel(State.translated_text),
                                        spacing="2",
                                        align="start",
                                        width="100%",
                                    ),
                                    rx.cond(
                                        State.summary_text != "",
                                        rx.vstack(
                                            panel_label("In short"),
                                            reading_panel(State.summary_text),
                                            spacing="2",
                                            align="start",
                                            width="100%",
                                        ),
                                    ),
                                    spacing="4",
                                    align="start",
                                    width="100%",
                                ),
                            ),
                            spacing="4",
                            align="start",
                            width="100%",
                        ),
                        width="100%",
                        style={
                            "background": "#FFFFFF",
                            "border": "1px solid #E6DFD0",
                        },
                    ),
                    rx.box(height="1px", background="#E6DFD0", width="100%"),
                    rx.text(
                        "Pages are read by Sarvam Vision, translated by Sarvam-105B, and narrated by Bulbul v3.",
                        size="1",
                        color="#6B6459",
                    ),
                    spacing="5",
                    align="start",
                    width="100%",
                ),
                max_width="880px",
                padding_top="8",
                padding_bottom="10",
            ),
            background="#F7F3EB",
            min_height="100vh",
            width="100%",
        ),
        appearance="light",
        accent_color="indigo",
        gray_color="sand",
        radius="medium",
    )


app = rx.App()
app.add_page(
    index,
    title="Pustakam — read any book in your language",
    description="Scan a page from any book and read it in your language.",
)
