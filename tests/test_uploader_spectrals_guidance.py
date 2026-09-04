"""The lossy-master question: the measurements are printed before it, and it never answers itself."""

import pytest

from salmon import cfg
from salmon.uploader import spectrals as sp
from salmon.uploader.frequency import SpectrumResult


def _lossy(name):
    return SpectrumResult(
        file=name,
        image="",
        sample_rate=44100,
        windows=10,
        reach_hz=16_600,
        cutoff_hz=16_600,
        knee_hz=16_450,
        floor_hz=16_750,
        drop_db=40,
        slope_db_per_khz=150,
        gating=0.3,
        gated_band="16–17 kHz",
        gating_abrupt=0.7,
        digital_floor=True,
    )


def _clean(name):
    return SpectrumResult(file=name, image="", sample_rate=44100, windows=10, reach_hz=21_000)


@pytest.fixture
def flow(monkeypatch, tmp_path):
    """check_spectrals with every slow or interactive step replaced by a recorder."""
    events: list[str] = []
    printed: list[str] = []
    state = {"spectra": [_lossy("01.flac")], "forced": None, "answer": False}

    async def generate_all(path, spectrals_path, audio_info):
        events.append("spectrograms-all")
        return {1: "01.flac"}

    async def frequency(path, files, out_dir):
        events.append("measure")
        return state["spectra"]

    async def view(spectrals_path, ids):
        events.append("view")

    async def prompt(force_prompt_lossy_master=False):
        events.append("prompt")
        state["forced"] = force_prompt_lossy_master
        return state["answer"]

    async def prompt_spectrals(*_a, **_k):
        return {}

    async def generate_ids(path, track_ids, spectrals_path, audio_info):
        events.append("spectrograms-ids")
        return {i: f"{i:02d}.flac" for i in track_ids}

    monkeypatch.setattr(sp, "create_specs_folder", lambda path, spectrals_path=None: str(tmp_path))
    monkeypatch.setattr(sp, "generate_spectrals_all", generate_all)
    monkeypatch.setattr(sp, "generate_spectrals_ids", generate_ids)
    monkeypatch.setattr(sp, "generate_frequency_plots", frequency)
    monkeypatch.setattr(sp, "get_audio_files", lambda path, *_a: ["01.flac"])
    monkeypatch.setattr(sp, "view_spectrals", view)
    monkeypatch.setattr(sp, "prompt_lossy_master", prompt)
    monkeypatch.setattr(sp, "prompt_spectrals", prompt_spectrals)
    monkeypatch.setattr(sp.click, "secho", lambda message="", **_kw: printed.append(str(message)))
    return events, printed, state


async def test_the_measurements_are_printed_before_the_question(flow):
    events, printed, _state = flow
    await sp.check_spectrals("/album", {"01.flac": {}}, None, None)
    assert events[:2] == ["spectrograms-all", "measure"]
    assert events.index("measure") < events.index("view") < events.index("prompt")
    joined = "\n".join(printed)
    assert "Frequency analysis: suspect" in joined
    assert "01.flac: brick-wall at 16.6 kHz" in joined
    assert "A measurement, not a verdict" in joined


async def test_preselected_ids_are_generated_before_the_guidance_and_the_question(flow):
    events, _printed, _state = flow
    lossy, ids = await sp.check_spectrals("/album", {"01.flac": {}}, None, (1,))
    assert events == ["spectrograms-ids", "measure", "prompt"]
    assert lossy is False
    assert ids == {1: "01.flac"}


async def test_the_answer_is_the_persons_not_the_measurements(flow):
    events, _printed, state = flow
    lossy, _ids = await sp.check_spectrals("/album", {"01.flac": {}}, None, None)
    assert lossy is False, "a suspect measurement must not answer the question"
    assert state["forced"] is True, "a suspect folder is asked about even under yes_all"


async def test_a_clean_folder_leaves_yes_all_alone(flow):
    _events, _printed, state = flow
    state["spectra"] = [_clean("01.flac")]
    await sp.check_spectrals("/album", {"01.flac": {}}, None, None)
    assert state["forced"] is False


async def test_no_lossy_check_means_no_measurement_and_no_question(flow):
    events, _printed, _state = flow
    lossy, _ids = await sp.check_spectrals("/album", {"01.flac": {}}, None, None, check_lma=False)
    assert lossy is None
    assert "measure" not in events
    assert "prompt" not in events


async def test_a_pre_answered_question_skips_the_measurement(flow):
    events, _printed, _state = flow
    lossy, _ids = await sp.check_spectrals("/album", {"01.flac": {}}, True, None)
    assert lossy is True
    assert "measure" not in events


async def test_a_failed_measurement_does_not_cost_the_upload(flow, monkeypatch):
    events, printed, _state = flow

    async def boom(path, files, out_dir):
        raise RuntimeError("decoder exploded")

    monkeypatch.setattr(sp, "generate_frequency_plots", boom)
    lossy, _ids = await sp.check_spectrals("/album", {"01.flac": {}}, None, None)
    assert lossy is False
    assert "prompt" in events
    assert any("Frequency analysis skipped" in line for line in printed)


async def test_yes_all_asks_the_question_only_when_told_to(monkeypatch):
    asked: list[str] = []

    async def fake_prompt(*_a, **_k):
        asked.append("asked")
        return "y"

    monkeypatch.setattr(sp.click, "prompt", fake_prompt)
    monkeypatch.setattr(sp, "flush_stdin", lambda: None)
    original = cfg.upload.yes_all
    cfg.upload.yes_all = True
    try:
        answer = await sp.prompt_lossy_master()
        assert answer is False, "yes_all answers no without asking"
        assert asked == []
        forced_answer = await sp.prompt_lossy_master(force_prompt_lossy_master=True)
        assert forced_answer is True
        assert asked == ["asked"]
    finally:
        cfg.upload.yes_all = original
