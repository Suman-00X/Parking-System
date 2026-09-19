"""Generates fake audio, dictations and templates for local development, so no real patient data is needed.

Order: make audio (synth_audio, synth_lossy_audio) -> make a spoken report to go with it
(synth_dictation, synth_patient_fields) -> make a template document (synth_template_docx).
"""

from __future__ import annotations

import datetime as dt
import io
import math
import random
import uuid
from collections.abc import Sequence
from dataclasses import dataclass


def synth_audio(*, seconds: float = 30.0, sample_rate: int = 16_000, audio_format: str = "flac", snr_db: float = 20.0, silence_ratio: float = 0.2, seed: int = 0) -> bytes:
    """Generate audio with controllable SNR and silence."""
    import numpy as np
    import soundfile as sf

    rng = np.random.default_rng(seed)
    samples = int(seconds * sample_rate)
    t = np.arange(samples) / sample_rate

    # A few formants stacked, amplitude-modulated to look speech-like to the
    # frame-energy estimators in `audio_gates`.
    signal = np.zeros(samples, dtype="float64")
    for freq, amp in ((120, 0.5), (240, 0.3), (480, 0.15), (1200, 0.05)):
        signal += amp * np.sin(2 * math.pi * freq * t)
    envelope = 0.5 + 0.5 * np.sin(2 * math.pi * 3.0 * t)
    signal *= envelope

    # Model the real thing: the *voice* stops, the room does not.
    signal_power = float(np.mean(signal**2)) or 1e-9
    noise_power = signal_power / (10 ** (snr_db / 10))

    if silence_ratio > 0:
        quiet_from = int(samples * (1 - silence_ratio))
        signal[quiet_from:] = 0.0

    signal += rng.normal(0, math.sqrt(noise_power), samples)

    peak = float(np.max(np.abs(signal))) or 1.0
    signal = (signal / peak * 0.85).astype("float32")

    buffer = io.BytesIO()
    sf.write(buffer, signal, sample_rate, format=audio_format.upper())
    return buffer.getvalue()


def synth_lossy_audio(*, seconds: float = 10.0, seed: int = 0) -> bytes:
    """Bytes that look like MP3, to exercise the lossy-codec rejection."""
    rng = random.Random(seed)
    header = b"\xff\xfb\x90\x00"  # MPEG-1 Layer III frame sync
    return header + bytes(rng.randrange(256) for _ in range(int(seconds * 1000)))


# ---------------------------------------------------------------- reports ---
_MODALITIES = ("US", "CT", "XR", "MRI")
_REGIONS = ("ABDOMEN", "CHEST", "PELVIS", "HEAD", "NECK")

_FINDING_TEMPLATES = ("The liver is normal in size and echotexture. No focal lesion is seen.", "The {side} kidney measures {size} cm. No hydronephrosis or calculus.", "The gallbladder is distended with a {size} mm calculus in the neck.", "The spleen is normal in size. No focal lesion.", "The pancreas is obscured by bowel gas.", "No free fluid in the {side} paracolic gutter.", "The uterus is anteverted and measures {size} cm. Endometrium is thin.", "Both lung fields are clear. No pleural effusion or pneumothorax.")

_ASIDES = ("sorry can you close the door", "um let me just check that again", "hold on the phone is ringing")

_SELF_CORRECTIONS = (("the left kidney", "sorry the right kidney"), ("measuring four centimetres", "no make that four point five centimetres"), ("no calculus", "correction there is a small calculus"))


@dataclass(frozen=True, slots=True)
class SyntheticDictation:
    """A dictation with its gold labels already known."""

    transcript_verbatim: str
    report_text: str
    study_code_spoken: str
    template_code: str
    utterance_labels: list[dict[str, object]]


def synth_dictation(*, seed: int = 0, include_aside: bool = True) -> SyntheticDictation:
    """One dictation with disfluencies, an aside and a self-correction."""
    rng = random.Random(seed)
    modality = rng.choice(_MODALITIES)
    region = rng.choice(_REGIONS)
    template_code = f"{modality}_{region}_ROUTINE"
    study_code = f"{_spoken_modality(modality)} {region.lower()} routine"

    parts: list[str] = []
    labels: list[dict[str, object]] = []

    def add(text: str, label: str, *, superseded: bool = False) -> None:
        labels.append({"seq": len(labels), "text": text, "label": label, "is_included_downstream": label == "report_content" and not superseded, "superseded": superseded})
        parts.append(text)

    add(f"study type {study_code}", "command")

    for template in rng.sample(_FINDING_TEMPLATES, k=3):
        add(template.format(side=rng.choice(("left", "right")), size=rng.randint(2, 12)), "report_content")

    if include_aside:
        add(rng.choice(_ASIDES), "aside")

    wrong, right = rng.choice(_SELF_CORRECTIONS)
    add(wrong, "report_content", superseded=True)
    add(right, "self_correction")

    transcript = " ".join(parts)
    report_text = " ".join(entry["text"] for entry in labels if entry["is_included_downstream"])

    return SyntheticDictation(transcript_verbatim=transcript, report_text=report_text, study_code_spoken=study_code, template_code=template_code, utterance_labels=labels)


def _spoken_modality(modality: str) -> str:
    return {"US": "ultrasound", "CT": "c t", "XR": "x ray", "MRI": "m r i"}[modality]


def synth_patient_fields(seed: int = 0) -> dict[str, object]:
    """Fake identifiers. `pseudonym` is what reaches any prompt."""
    rng = random.Random(seed)
    return {"mrn": f"SYNTH-{rng.randrange(10**6):06d}", "pseudonym": f"PT-{uuid.UUID(int=rng.getrandbits(128)).hex[:10]}", "age_years": rng.randint(18, 88), "sex": rng.choice(("M", "F")), "date_of_birth": dt.date(rng.randint(1940, 2006), rng.randint(1, 12), rng.randint(1, 28))}


#: A structured reporting template as template import expects to receive one: `Label: value`
#: lines under upper-case section headings.
SYNTH_CHEST_CT: tuple[tuple[str, bool], ...] = (("CT Chest Plain", True), ("FINDINGS", True), ("Lungs: Normal in size and attenuation", False), ("Pleura: No effusion [present/absent]", False), ("Mediastinum: Largest node 3.2 cm in short axis", False), ("IMPRESSION", True), ("Summary: Unremarkable study", False))

SYNTH_USG_ABDOMEN: tuple[tuple[str, bool], ...] = (("USG Abdomen", True), ("FINDINGS", True), ("Liver: Normal in size and echotexture", False), ("Gallbladder: Unremarkable, no calculi", False), ("Kidneys: Both kidneys are normal in size", False))


def synth_template_docx(paragraphs: Sequence[tuple[str, bool]] = SYNTH_CHEST_CT) -> bytes:
    """A minimal but genuine .docx, from `(text, is_heading)` pairs."""
    import zipfile

    body: list[str] = []
    for text, is_heading in paragraphs:
        style = '<w:pPr><w:pStyle w:val="Heading1"/></w:pPr>' if is_heading else ""
        escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        body.append(f"<w:p>{style}<w:r><w:t>{escaped}</w:t></w:r></w:p>")

    document = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>{"".join(body)}</w:body></w:document>'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()
