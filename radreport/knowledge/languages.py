"""Recognises radiology terms said or typed in Hindi (Devanagari or Latin script), French or Spanish, and gives the English term the lexicon knows.

Order: a lab turns languages on in its settings (enabled_languages: languages.hi, languages.fr,
languages.es, and languages.hi_latin for Hindi typed in Latin letters) -> the bundled dictionaries
for those languages are loaded once (glossary, from data/languages/<code>.csv) -> text is scanned for
the longest known phrase at each position (find_foreign) -> each becomes a span with its English
term (translate), and with an online translator configured (languages.online_translation and
GOOGLE_TRANSLATE_API_KEY) unknown Devanagari words are sent to it, one word at a time, never the
whole sentence. Adding a language is a CSV and a SUPPORTED entry.
"""

from __future__ import annotations

import csv
import os
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from sqlalchemy.orm import Session

from radreport.core import system_config
from radreport.core.logging import get_logger

log = get_logger(__name__)

DATA = Path(__file__).resolve().parent / "data" / "languages"


@dataclass(frozen=True, slots=True)
class Language:
    code: str
    name: str
    script: str


SUPPORTED: dict[str, Language] = {"hi": Language("hi", "Hindi", "Devanagari"), "fr": Language("fr", "French", "Latin"), "es": Language("es", "Spanish", "Latin")}
#: Latin-script forms that are also English words; never matched, so English text is never "translated".
ENGLISH_COLLISIONS = frozenset({"pet", "sir", "rate", "dil", "masse", "normal", "no", "calcul"})
#: Devanagari letters and signs, without the danda (U+0964/5), which ends a sentence.
_DEVANAGARI_WORD = r"[\u0900-\u0963\u0966-\u097F]+"
_TOKEN = re.compile(_DEVANAGARI_WORD + r"|[A-Za-zÀ-ÖØ-öø-ÿœŒ'’-]+")
#: Hindi grammar words; never worth a lookup.
HINDI_STOPWORDS = frozenset("है हैं था थे की का के में से और दे रही रहा रहे दिखाई पर को भी यह वह एक तथा या कुछ लगभग".split())
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
MAX_WORDS = 5


def normalise(text: str) -> str:
    """Lower case, accents off, Devanagari nasal and nukta marks folded, so spelling variants meet."""
    text = unicodedata.normalize("NFC", text).lower().replace("œ", "oe").replace("’", "'")
    # Chandrabindu and anusvara are written interchangeably; the nukta is often dropped when typing.
    text = text.replace("ँ", "ं").replace("़", "")
    decomposed = unicodedata.normalize("NFD", text)
    folded = "".join(ch for ch in decomposed if not (unicodedata.category(ch) == "Mn" and not _DEVANAGARI.match(ch)))
    return unicodedata.normalize("NFC", folded)


def has_devanagari(text: str) -> bool:
    return bool(_DEVANAGARI.search(text))


@dataclass(frozen=True, slots=True)
class Entry:
    english: str
    language: str
    script: str
    """"native" or "latin"."""


@lru_cache(maxsize=32)
def glossary(languages: tuple[str, ...], latin_hindi: bool = False) -> dict[str, Entry]:
    """Normalised phrase -> its English term, for the languages given."""
    out: dict[str, Entry] = {}
    for code in languages:
        if code not in SUPPORTED:
            continue
        with (DATA / f"{code}.csv").open(encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                english = row["english"].strip()
                for native in (row["native"] or "").split("|"):
                    key = normalise(native.strip())
                    if key and key not in ENGLISH_COLLISIONS:
                        out.setdefault(key, Entry(english, code, "native"))
                # Latin-script spellings of a Devanagari language are opt-in: they read like English words to a scanner.
                if SUPPORTED[code].script != "Latin" and not latin_hindi:
                    continue
                for latin in (row.get("latin") or "").split("|"):
                    key = normalise(latin.strip())
                    if key and key not in ENGLISH_COLLISIONS:
                        out.setdefault(key, Entry(english, code, "latin"))
    return out


@dataclass(frozen=True, slots=True)
class ForeignSpan:
    start: int
    end: int
    surface: str
    english: str
    language: str
    how: str = "dictionary"
    """"dictionary", or "online" for a word the online translator gave."""


def find_foreign(text: str, terms: dict[str, Entry]) -> list[ForeignSpan]:
    """The longest dictionary phrase at each position, left to right, never overlapping."""
    if not terms:
        return []
    tokens = [(m.start(), m.end(), normalise(m.group(0))) for m in _TOKEN.finditer(text)]
    spans: list[ForeignSpan] = []
    i = 0
    while i < len(tokens):
        for size in range(min(MAX_WORDS, len(tokens) - i), 0, -1):
            phrase = " ".join(t[2] for t in tokens[i : i + size])
            entry = terms.get(phrase)
            if entry is not None:
                start, end = tokens[i][0], tokens[i + size - 1][1]
                spans.append(ForeignSpan(start, end, text[start:end], entry.english, entry.language))
                i += size
                break
        else:
            i += 1
    return spans


class Translator(Protocol):
    """An online translation service, asked about single words the dictionaries do not know."""

    def translate(self, words: list[str], *, source: str, target: str = "en") -> list[str]: ...


@dataclass(slots=True)
class GoogleTranslator:
    """Google Cloud Translation v2 over HTTPS; the key comes from GOOGLE_TRANSLATE_API_KEY."""

    api_key: str
    endpoint: str = "https://translation.googleapis.com/language/translate/v2"
    timeout_seconds: float = 5.0
    transport: object | None = None

    def translate(self, words: list[str], *, source: str, target: str = "en") -> list[str]:
        import html

        import httpx

        with httpx.Client(timeout=self.timeout_seconds, transport=self.transport) as client:  # type: ignore[arg-type]
            response = client.post(self.endpoint, params={"key": self.api_key}, json={"q": words, "source": source, "target": target, "format": "text"})
            response.raise_for_status()
        return [html.unescape(t["translatedText"]) for t in response.json()["data"]["translations"]]


@dataclass(frozen=True, slots=True)
class LabLanguages:
    languages: tuple[str, ...] = ()
    latin_hindi: bool = False
    online: bool = False

    @property
    def any(self) -> bool:
        return bool(self.languages)


def enabled_languages(session: Session, tenant_id: uuid.UUID) -> LabLanguages:
    """What this lab has switched on."""

    def on(name: str) -> bool:
        return bool(int(system_config.resolve(session, name, tenant_id=tenant_id).value))

    return LabLanguages(languages=tuple(code for code in SUPPORTED if on(f"languages.{code}")), latin_hindi=on("languages.hi_latin"), online=on("languages.online_translation"))


def translator_from_env() -> Translator | None:
    key = os.environ.get("GOOGLE_TRANSLATE_API_KEY")
    return GoogleTranslator(api_key=key) if key else None


@dataclass(slots=True)
class Translation:
    spans: list[ForeignSpan] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    """Devanagari words neither the dictionaries nor the translator knew."""

    def english_text(self, text: str) -> str:
        """The text with each foreign span replaced by its English term."""
        out, cursor = [], 0
        for span in sorted(self.spans, key=lambda s: s.start):
            out.append(text[cursor : span.start])
            out.append(span.english)
            cursor = span.end
        out.append(text[cursor:])
        return "".join(out)


def translate(text: str, lab: LabLanguages, *, translator: Translator | None = None) -> Translation:
    """Known foreign terms in the text, with their English; unknown Devanagari words go to the translator when the lab allows it."""
    result = Translation(spans=find_foreign(text, glossary(lab.languages, lab.latin_hindi)))
    if "hi" not in lab.languages or not has_devanagari(text):
        return result
    covered = [(s.start, s.end) for s in result.spans]
    leftovers = [m for m in re.finditer(_DEVANAGARI_WORD, text) if not any(a <= m.start() < b for a, b in covered) and m.group(0) not in HINDI_STOPWORDS]
    if not leftovers:
        return result
    if not (lab.online and translator):
        result.unknown = [m.group(0) for m in leftovers]
        return result
    try:
        english = translator.translate([m.group(0) for m in leftovers], source="hi")
    except Exception as exc:  # noqa: BLE001 - the dictionaries' answer stands when the service is down
        log.warning("online_translation_failed", error=str(exc)[:200])
        result.unknown = [m.group(0) for m in leftovers]
        return result
    for match, word in zip(leftovers, english, strict=False):
        result.spans.append(ForeignSpan(match.start(), match.end(), match.group(0), word.strip().lower(), "hi", how="online"))
    result.spans.sort(key=lambda s: s.start)
    return result
