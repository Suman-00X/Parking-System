"""Synthetic demo dictations carry their words in a WAV comment; the stub engine hears them, and other audio gets the fixed sentence."""

from __future__ import annotations

import pytest

from radreport.adapters.asr.base import ASRConfig
from radreport.adapters.asr.whisper_local import StubASREngine, spoken_text_in
from radreport.devtools.synthetic import synth_audio, with_spoken_text


async def test_the_stub_hears_what_the_file_carries() -> None:
    wav = with_spoken_text(synth_audio(seconds=2.0, audio_format="wav"), "study type c t chest. no pneumothorax.")
    assert spoken_text_in(wav) == "study type c t chest. no pneumothorax."
    result = await StubASREngine().transcribe(wav, ASRConfig())
    assert result.text.startswith("study type c t chest") and len(result.words) == 7
    plain = await StubASREngine().transcribe(synth_audio(seconds=2.0, audio_format="flac"), ASRConfig())
    assert plain.text == "study type ultrasound abdomen routine"
    assert spoken_text_in(b"not audio") is None
    with pytest.raises(ValueError):
        with_spoken_text(b"fLaC....", "x")
