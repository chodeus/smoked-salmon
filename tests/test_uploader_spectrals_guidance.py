"""The lossy-master question: the measurements are printed before it, and it never answers itself."""

import os

import pytest

from salmon import cfg, interaction
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

    async def frequency(path, files, plot_paths):
        events.append("measure")
        state["plot_paths"] = plot_paths
        return state["spectra"]

    async def view(spectrals_path, ids):
        events.append("view")

    async def prompt(force_prompt_lossy_master=False, suggested="n", offer_deletion=True):
        events.append("prompt")
        state["forced"] = force_prompt_lossy_master
        state["suggested"] = suggested
        state["offer_deletion"] = offer_deletion
        return state["answer"]

    async def prompt_spectrals(*_a, **_k):
        return {}

    async def generate_ids(path, track_ids, spectrals_path, audio_info):
        events.append("spectrograms-ids")
        # As _generate_spectrals does: the chosen tracks are numbered from 1.
        return {index + 1: f"{i:02d}.flac" for index, i in enumerate(track_ids)}

    monkeypatch.setattr(sp, "create_specs_folder", lambda path, spectrals_path=None: str(tmp_path))
    monkeypatch.setattr(sp, "generate_spectrals_all", generate_all)
    monkeypatch.setattr(sp, "generate_spectrals_ids", generate_ids)
    monkeypatch.setattr(sp, "generate_frequency_plots", frequency)
    monkeypatch.setattr(sp, "get_audio_files", lambda path, *_a: ["01.flac"])
    monkeypatch.setattr(interaction, "show_spectrals", view)
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
    assert "Frequency analysis: the marks of a lossy encoder" in joined
    assert "01.flac: brick-wall at 16.6 kHz" in joined
    assert "A measurement, not a verdict" in joined


async def test_preselected_ids_are_generated_before_the_guidance_and_the_question(flow):
    events, _printed, _state = flow
    lossy, ids = await sp.check_spectrals("/album", {"01.flac": {}}, None, (1,))
    assert events == ["spectrograms-ids", "measure", "prompt"]
    assert lossy is False
    assert ids == {1: "01.flac"}


async def test_a_chosen_tracks_plot_takes_its_spectrals_number(flow, monkeypatch, tmp_path):
    _events, _printed, state = flow
    monkeypatch.setattr(sp, "get_audio_files", lambda path, *_a: ["01.flac", "02.flac"])
    await sp.check_spectrals("/album", {"01.flac": {}, "02.flac": {}}, None, (2,))
    assert state["plot_paths"] == {"02.flac": str(tmp_path / "01 Frequency.png")}


async def test_frequency_plots_are_numbered_as_the_real_spectrals_are(monkeypatch, tmp_path):
    album = tmp_path / "album"
    album.mkdir()
    for name in ("01.flac", "02.flac"):
        (album / name).write_bytes(b"")
    written: list[str] = []

    async def sox(command, **_kw):
        written.extend(os.path.basename(arg) for arg in command if arg.endswith(".png"))

    monkeypatch.setattr(sp.anyio, "run_process", sox)
    monkeypatch.setattr(cfg.upload.compression, "compress_spectrals", False)
    info = {"01.flac": {"duration": 60}, "02.flac": {"duration": 60}}
    ids = await sp.generate_spectrals_ids(str(album), (2,), str(tmp_path), info)
    plots = sp.frequency_plot_paths(ids, str(tmp_path))
    assert list(plots) == ["02.flac"]
    number = os.path.basename(plots["02.flac"]).split()[0]
    assert f"{number} Full.png" in written


async def test_the_answer_is_the_persons_not_the_measurements(flow):
    events, _printed, state = flow
    lossy, _ids = await sp.check_spectrals("/album", {"01.flac": {}}, None, None)
    assert lossy is False, "a suspect measurement must not answer the question"
    assert state["forced"] is True, "a suspect folder is asked about even under yes_all"
    assert state["suggested"] == "y", "the pre-typed answer follows the measurement"


async def test_a_clean_folder_leaves_yes_all_alone(flow):
    _events, _printed, state = flow
    state["spectra"] = [_clean("01.flac")]
    await sp.check_spectrals("/album", {"01.flac": {}}, None, None)
    assert state["forced"] is False
    assert state["suggested"] == "n"


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


async def test_yes_all_asks_for_the_lossy_comment_when_there_is_no_source_url(monkeypatch):
    """With yes_all, a "yes" to the question led to a lossy comment it refused forever without a source URL."""
    monkeypatch.setattr(sp.cfg.upload, "yes_all", True)
    refusals: list[str] = []

    def refuse(message="", **_kw):
        refusals.append(message)
        if len(refusals) > 3:
            raise AssertionError("the empty comment is refused again and again")

    async def fake_prompt(*_a, **_k):
        return "Bought on the label's site."

    monkeypatch.setattr(sp.click, "secho", refuse)
    monkeypatch.setattr(sp.click, "prompt", fake_prompt)
    comment = await sp.generate_lossy_approval_comment(None, ["01.flac"])
    assert comment == "Bought on the label's site."
    comment = await sp.generate_lossy_approval_comment("https://store.test/album", ["01.flac"])
    assert comment == ""
