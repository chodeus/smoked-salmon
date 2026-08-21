"""Frequency analysis: the measured cutoff, and what it is allowed to claim."""

import json
import math
import wave

import numpy as np
import pytest

from salmon.uploader import frequency as fq


def _write_lowpassed_noise(path, cutoff_hz, sample_rate=44100, seconds=3):
    """A noise burst with everything above cutoff_hz removed — a known answer."""
    rng = np.random.default_rng(0)
    samples = rng.normal(0, 0.2, sample_rate * seconds)
    spectrum = np.fft.rfft(samples)
    spectrum[np.fft.rfftfreq(len(samples), 1 / sample_rate) > cutoff_hz] = 0
    filtered = np.fft.irfft(spectrum, len(samples))
    pcm = np.clip(filtered * 32767, -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())


def test_cutoff_is_measured_where_the_energy_actually_stops(tmp_path):
    src = tmp_path / "lowpassed.wav"
    _write_lowpassed_noise(src, 10_000)
    freqs, db, sample_rate, windows = fq.average_spectrum(str(src))
    assert sample_rate == 44100
    assert windows > 0
    assert 9_000 < fq.find_cutoff(freqs, db) < 11_000


def test_full_bandwidth_audio_measures_up_near_nyquist(tmp_path):
    src = tmp_path / "full.wav"
    _write_lowpassed_noise(src, 22_000)
    freqs, db, _rate, _windows = fq.average_spectrum(str(src))
    assert fq.find_cutoff(freqs, db) > 20_000


def test_a_plot_is_written_for_the_curve(tmp_path):
    src = tmp_path / "full.wav"
    _write_lowpassed_noise(src, 15_000)
    freqs, db, rate, _windows = fq.average_spectrum(str(src))
    out = tmp_path / "plot.png"
    fq.render_plot(freqs, db, rate, str(out), "title")
    assert out.stat().st_size > 0


def _result(name, cutoff, drop=0.0, rate=44100, error=None):
    return fq.SpectrumResult(
        file=name, image="x.png", sample_rate=rate, cutoff_hz=cutoff, windows=10, drop_db=drop, error=error
    )


def test_full_spectrum_tracks_are_not_flagged():
    verdict = fq.assess([_result("01.flac", 21_800), _result("02.flac", 21_900)])
    assert verdict["level"] == "ok"


def test_a_cliff_asks_for_a_look_without_calling_it_a_transcode():
    verdict = fq.assess([_result("01.flac", 16_000, drop=60), _result("02.flac", 16_100, drop=58)])
    assert verdict["level"] == "look"
    joined = " ".join(verdict["notes"]).lower()
    assert "16.0 khz" in joined
    assert "lossy encoder" in joined
    assert "transcode" not in joined  # a measurement, never a verdict


def test_an_early_but_gentle_roll_off_is_not_flagged():
    # Plenty of honest masters fade out below 20 kHz; only the cliff shape matters.
    verdict = fq.assess([_result("01.flac", 18_200, drop=1), _result("02.flac", 18_300, drop=2)])
    assert verdict["level"] == "ok"
    assert "mastering choice" in " ".join(verdict["notes"])


def test_the_drop_separates_a_filter_from_a_fade(tmp_path):
    src = tmp_path / "lowpassed.wav"
    _write_lowpassed_noise(src, 16_000)
    freqs, db, _rate, _windows = fq.average_spectrum(str(src))
    cutoff = fq.find_cutoff(freqs, db)
    assert fq.cutoff_drop(freqs, db, cutoff) > fq._CLIFF_DB


def test_tracks_that_disagree_are_the_stronger_signal():
    verdict = fq.assess([_result("01.flac", 16_000, drop=60), _result("02.flac", 20_000, drop=50)])
    assert verdict["level"] == "suspect"
    assert any("different frequencies" in n for n in verdict["notes"])


def test_a_file_that_could_not_be_analysed_is_ignored_rather_than_skewing_the_spread():
    verdict = fq.assess([_result("01.flac", 21_800), _result("02.flac", 0.0, error="boom")])
    assert verdict["level"] == "ok"


def test_nothing_analysable_says_so():
    verdict = fq.assess([_result("01.flac", 0.0, error="boom")])
    assert verdict["notes"] == ["No file could be analysed."]


def test_a_file_too_short_to_average_reports_no_measurement(tmp_path):
    """A flat all-zero spectrum sits within the floor of its own maximum, so an
    unguarded find_cutoff would report Nyquist for audio never measured."""
    src = tmp_path / "tiny.wav"
    with wave.open(str(src), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(np.zeros(128, dtype="<i2").tobytes())

    result = fq._analyse_one(str(tmp_path), "tiny.wav", str(tmp_path), 0)

    assert result.windows == 0
    assert result.cutoff_hz == 0.0
    assert result.error
    assert fq.assess([result])["level"] == "ok"


def test_a_silent_track_does_not_fake_a_disagreement():
    verdict = fq.assess([_result("01.flac", 21_800), _result("02.flac", 0.0)])
    assert verdict["level"] == "ok"


def test_silence_measures_no_cutoff_rather_than_the_whole_spectrum(tmp_path):
    """A flat spectrum sits within any floor of its own maximum, so silence
    would otherwise be reported as carrying energy to Nyquist."""
    src = tmp_path / "silent.wav"
    with wave.open(str(src), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(np.zeros(fq.FFT_SIZE * 4, dtype="<i2").tobytes())

    freqs, db, _rate, windows = fq.average_spectrum(str(src))

    assert windows > 0, "the file is long enough to average; this is not the no-windows case"
    assert fq.find_cutoff(freqs, db) == 0.0

    result = fq._analyse_one(str(tmp_path), "silent.wav", str(tmp_path), 0)
    assert fq.assess([result])["notes"] == ["No track carried enough signal to measure."]


# ---------------------------------------------------------------------------
# Degenerate input: a measurement that cannot be made must not become a claim
# ---------------------------------------------------------------------------


def _nan_spectrum():
    freqs = np.fft.rfftfreq(fq.FFT_SIZE, 1 / 44100)
    return freqs, np.full(len(freqs), np.nan)


def test_a_corrupt_spectrum_never_yields_a_non_finite_drop():
    """NaN serialises to a literal the browser's JSON.parse rejects, which would
    take the whole job payload down rather than just this row."""
    freqs, db = _nan_spectrum()
    drop = fq.cutoff_drop(freqs, db, 0.0)
    assert math.isfinite(drop)
    json.dumps({"drop_db": drop})  # raises nothing a browser would choke on


def test_a_corrupt_spectrum_measures_no_cutoff():
    freqs, db = _nan_spectrum()
    assert fq.find_cutoff(freqs, db) == 0.0


def test_a_failed_plot_costs_only_its_own_file(tmp_path, monkeypatch):
    src = tmp_path / "full.wav"
    _write_lowpassed_noise(src, 15_000)

    def boom(*_args, **_kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(fq, "render_plot", boom)

    result = fq._analyse_one(str(tmp_path), "full.wav", str(tmp_path), 0)

    assert result.error == "no space left on device"
    assert result.image == ""
    assert not (tmp_path / "01 Frequency.png").exists(), "a half-written plot would still be listed and posted"


def test_plotting_an_impossible_sample_rate_fails_clearly():
    freqs, db = _nan_spectrum()
    with pytest.raises(ValueError, match="0 Hz"):
        fq.render_plot(freqs, db, 0, "unused.png", "title")


def test_a_result_without_a_sample_rate_is_not_measured_against_nyquist():
    unusable = fq.SpectrumResult(file="x.flac", image="i", sample_rate=0, cutoff_hz=100.0, windows=1)
    assert fq.assess([unusable])["notes"] == ["No file could be analysed."]
