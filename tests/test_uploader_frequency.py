"""Frequency analysis: the two marks of a lossy encoder, and what a folder is allowed to be told."""

import json
import math
import wave

import av
import numpy as np
import pytest

from salmon.uploader import frequency as fq

RATE = 44100


def _write_wav(path, samples, rate=RATE):
    pcm = np.clip(np.asarray(samples) * 32767, -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


def _noise(seconds, level=0.2, tilt_db_per_octave=0.0, seed=0):
    """White noise, optionally tilted downwards like a dark master."""
    rng = np.random.default_rng(seed)
    samples = rng.normal(0, level, RATE * seconds)
    if not tilt_db_per_octave:
        return samples
    spectrum = np.fft.rfft(samples)
    freqs = np.fft.rfftfreq(len(samples), 1 / RATE)
    gain = 10 ** (tilt_db_per_octave * np.log2(np.maximum(freqs, 100) / 100) / 20)
    return np.fft.irfft(spectrum * gain, len(samples))


def _lowpass(samples, cutoff_hz):
    spectrum = np.fft.rfft(samples)
    spectrum[np.fft.rfftfreq(len(samples), 1 / RATE) > cutoff_hz] = 0
    return np.fft.irfft(spectrum, len(samples))


def _band(samples, lo, hi):
    spectrum = np.fft.rfft(samples)
    freqs = np.fft.rfftfreq(len(samples), 1 / RATE)
    spectrum[(freqs < lo) | (freqs > hi)] = 0
    return np.fft.irfft(spectrum, len(samples))


def _gated(seconds=6, floor=None, seed=1):
    """Loud mids all the way through; highs that switch on and off every 90 ms.

    With floor=None the off state is digital silence, as a decoded lossy file
    has it. A floor level makes the off state quiet noise instead, which is
    what a clean production falls to.
    """
    mids = _band(_noise(seconds, 0.3, seed=seed), 2000, 8000)
    highs = _band(_noise(seconds, 0.05, seed=seed + 1), 16_000, 21_000)
    gate = (np.arange(RATE * seconds) // (RATE * 90 // 1000)) % 2 == 0
    if floor is not None:
        quiet = _band(_noise(seconds, floor, seed=seed + 2), 16_000, 21_000)
        return mids + np.where(gate, highs, quiet)
    return mids + highs * gate


def _analyse(tmp_path, name, samples):
    _write_wav(tmp_path / name, samples)
    return fq._analyse_one(str(tmp_path), name, str(tmp_path), 0)


def _result(
    name,
    reach=21_800.0,
    cutoff=0.0,
    floor_hz=0.0,
    drop=0.0,
    slope=0.0,
    gating=0.0,
    abrupt=0.0,
    digital=False,
    rate=RATE,
    error=None,
):
    return fq.SpectrumResult(
        file=name,
        image="x.png",
        sample_rate=rate,
        windows=10,
        reach_hz=reach,
        cutoff_hz=cutoff,
        knee_hz=cutoff - 150 if cutoff else 0.0,
        floor_hz=floor_hz or (cutoff + 150 if cutoff else 0.0),
        drop_db=drop,
        slope_db_per_khz=slope,
        gating=gating,
        gated_band="19–20 kHz" if gating else "",
        gating_abrupt=abrupt,
        digital_floor=digital,
        error=error,
    )


# ---------------------------------------------------------------------------
# The wall: measured against the floor above it, not the track's peak
# ---------------------------------------------------------------------------


def test_a_wall_is_measured_where_the_energy_meets_the_floor(tmp_path):
    result = _analyse(tmp_path, "lowpassed.wav", _lowpass(_noise(3), 16_500))
    assert 16_300 < result.cutoff_hz < 16_900
    assert result.drop_db > 30
    assert result.slope_db_per_khz > 50
    assert fq.has_lossy_wall(result)


def test_a_dark_master_with_a_wall_still_measures_the_wall_not_the_slope(tmp_path):
    """The old cutoff was 'peak minus 60 dB', which on a dark track landed on the
    slope well below the wall and called a 320 kbps lowpass a mastering choice."""
    result = _analyse(tmp_path, "dark.wav", _lowpass(_noise(3, tilt_db_per_octave=-6), 20_200))
    assert 19_950 < result.cutoff_hz < 20_500
    assert result.slope_db_per_khz > 50


def test_full_bandwidth_audio_has_no_wall_and_reaches_nyquist(tmp_path):
    result = _analyse(tmp_path, "full.wav", _noise(3))
    assert result.cutoff_hz == 0.0
    assert result.reach_hz > 20_000
    assert fq.classify(result) == "clean"


def test_a_gentle_roll_off_is_not_a_wall(tmp_path):
    result = _analyse(tmp_path, "gentle.wav", _noise(3, tilt_db_per_octave=-12))
    assert not fq.has_lossy_wall(result)
    assert fq.classify(result) == "clean"


def test_a_wall_above_the_encoder_range_is_read_as_sample_rate_conversion():
    result = _result("srx.flac", reach=21_000, cutoff=21_100, drop=35, slope=55)
    assert not fq.has_lossy_wall(result)
    assert fq.classify(result) == "clean"
    assert "sample-rate conversion" in fq.describe(result)


def test_the_encoder_whose_lowpass_floors_out_there_is_named():
    assert "128 kbps" in fq.encoder_hint(16_800)
    assert "320 kbps" in fq.encoder_hint(20_300)
    assert fq.encoder_hint(21_000) == ""
    assert fq.encoder_hint(fq._LOSSY_WALL_HZ[0]), "the accepted range must start on a named setting"


# ---------------------------------------------------------------------------
# The gate: highs that flip between content and the bit-depth floor
# ---------------------------------------------------------------------------


def test_gated_highs_over_digital_silence_are_the_mark_of_an_encoder(tmp_path):
    result = _analyse(tmp_path, "gated.wav", _gated())
    assert result.digital_floor
    assert result.gating >= fq._GATED_STRONG
    assert result.gating_abrupt >= fq._GATED_STRONG_ABRUPT
    assert fq.classify(result) == "lossy"


def test_the_same_gating_over_a_noise_floor_is_not(tmp_path):
    """A clean production can fall to its own floor; only digital silence counts."""
    result = _analyse(tmp_path, "natural.wav", _gated(floor=0.002))
    assert not result.digital_floor
    assert fq.classify(result) == "clean"


def test_highs_that_stay_on_do_not_gate(tmp_path):
    mids = _band(_noise(6, 0.3), 2000, 8000)
    highs = _band(_noise(6, 0.05, seed=2), 16_000, 21_000)
    result = _analyse(tmp_path, "steady.wav", mids + highs)
    assert result.gating < fq._GATED_WITH_WALL
    assert fq.classify(result) == "clean"


# ---------------------------------------------------------------------------
# A real encoder: the known answer the analysis exists for
# ---------------------------------------------------------------------------


def _mp3_transcode(tmp_path, samples, bit_rate):
    src = tmp_path / "src.wav"
    _write_wav(src, samples)
    lossy = tmp_path / f"t{bit_rate}.mp3"
    inp = av.open(str(src))
    out = av.open(str(lossy), "w")
    stream = out.add_stream("libmp3lame", rate=RATE)
    stream.bit_rate = bit_rate
    resampler = av.AudioResampler(format="s16p", layout="stereo", rate=RATE)
    for frame in inp.decode(inp.streams.audio[0]):
        for resampled in resampler.resample(frame):
            out.mux(stream.encode(resampled))
    for resampled in resampler.resample(None):
        out.mux(stream.encode(resampled))
    out.mux(stream.encode(None))
    out.close()
    inp.close()
    decoded, _rate = fq.decode_mono(str(lossy))
    name = f"transcode{bit_rate}.wav"
    _write_wav(tmp_path / name, decoded)
    return name


def _has_mp3_encoder() -> bool:
    # codecs_available lists names regardless of direction; probe the encoder itself.
    try:
        av.Codec("libmp3lame", "w")
    except Exception:
        return False
    return True


_needs_lame = pytest.mark.skipif(not _has_mp3_encoder(), reason="PyAV build has no MP3 encoder")


@_needs_lame
@pytest.mark.parametrize(("bit_rate", "hint"), [(128_000, "128 kbps"), (320_000, "320 kbps")])
def test_a_decoded_mp3_shows_the_wall_where_lame_puts_it_and_the_original_does_not(tmp_path, bit_rate, hint):
    """The shipped analyser ignored any cutoff above 91% of Nyquist, so a 320 kbps
    lowpass at 20.2 kHz passed without a note; on a dark master its 60-dB-below-peak
    cutoff landed on the slope and called the 192 and 256 kbps walls a mastering choice."""
    original = _noise(8, tilt_db_per_octave=-4)
    clean = _analyse(tmp_path, "original.wav", original)
    assert fq.classify(clean) == "clean"
    transcode = fq._analyse_one(str(tmp_path), _mp3_transcode(tmp_path, original, bit_rate), str(tmp_path), 1)
    assert fq.has_lossy_wall(transcode), transcode
    assert hint in fq.describe(transcode)
    assert fq.classify(transcode) in ("look", "lossy")


# ---------------------------------------------------------------------------
# What a folder is told
# ---------------------------------------------------------------------------


def test_clean_tracks_that_stop_at_different_frequencies_are_not_read_as_mixed_sources():
    """A compilation from one store stops at 17 kHz on one track and 22 kHz on the
    next; the old assessment called that 'assembled from more than one source'."""
    verdict = fq.assess(
        [_result("01.flac", reach=17_100), _result("02.flac", reach=22_100), _result("03.flac", reach=19_000)]
    )
    assert verdict["level"] == "ok"
    joined = " ".join(verdict["notes"]).lower()
    assert "source" not in joined.replace("where the files came from", "")
    assert "normal for compilations" in joined


def test_both_marks_make_the_folder_suspect_and_name_the_setting():
    lossy = _result("01.flac", reach=18_800, cutoff=18_800, drop=40, slope=150, gating=0.3, abrupt=0.8, digital=True)
    verdict = fq.assess([lossy, _result("02.flac")])
    assert verdict["level"] == "suspect"
    joined = " ".join(verdict["notes"])
    assert "192 kbps" in joined
    assert "1 of 2 tracks carries" in joined
    assert "mixed sources" in joined


def test_every_track_lossy_says_transcode_or_lossy_master():
    lossy = _result("01.flac", reach=16_600, cutoff=16_600, drop=40, slope=150, gating=0.2, abrupt=0.6, digital=True)
    verdict = fq.assess([lossy, lossy])
    assert verdict["level"] == "suspect"
    assert any("Every track" in n and "lossy master" in n for n in verdict["notes"])


def test_a_wall_alone_asks_for_a_look_and_does_not_call_it_lossy():
    wall_only = _result("01.flac", reach=20_100, cutoff=20_150, drop=44, slope=140)
    verdict = fq.assess([wall_only])
    assert verdict["level"] == "look"
    joined = " ".join(verdict["notes"])
    assert "320 kbps" in joined and "not gated" in joined
    assert "both marks" not in joined


def test_gating_alone_needs_to_be_abrupt_before_it_asks_for_a_look():
    slow = _result("01.flac", gating=0.2, abrupt=0.1, digital=True)
    abrupt = _result("02.flac", gating=0.2, abrupt=0.5, digital=True)
    assert fq.classify(slow) == "clean"
    assert fq.classify(abrupt) == "look"


def test_strong_abrupt_gating_is_lossy_even_without_a_wall():
    result = _result("01.flac", gating=0.4, abrupt=0.7, digital=True)
    assert fq.classify(result) == "lossy"
    assert "no lowpass in the encoder range" in fq.describe(result)


def test_per_track_notes_are_capped():
    lossy = [
        _result(f"{i:02d}.flac", reach=16_600, cutoff=16_600, drop=40, slope=150, gating=0.2, abrupt=0.6, digital=True)
        for i in range(12)
    ]
    notes = fq.assess(lossy)["notes"]
    assert sum(".flac:" in n for n in notes) == fq._MAX_TRACK_NOTES
    assert any("4 more tracks" in n for n in notes)


def test_a_file_that_could_not_be_analysed_is_ignored_rather_than_skewing_the_folder():
    verdict = fq.assess([_result("01.flac"), _result("02.flac", reach=0.0, rate=0, error="boom")])
    assert verdict["level"] == "ok"


def test_nothing_analysable_says_so():
    assert fq.assess([_result("01.flac", reach=0.0, rate=0, error="boom")])["notes"] == ["No file could be analysed."]


def test_a_silent_track_does_not_fake_anything():
    verdict = fq.assess([_result("01.flac"), _result("02.flac", reach=0.0)])
    assert verdict["level"] == "ok"
    assert fq.assess([_result("02.flac", reach=0.0)])["notes"] == ["No track carried enough signal to measure."]


# ---------------------------------------------------------------------------
# Degenerate input: a measurement that cannot be made must not become a claim
# ---------------------------------------------------------------------------


def test_a_file_too_short_to_average_reports_no_measurement(tmp_path):
    result = _analyse(tmp_path, "tiny.wav", np.zeros(128))
    assert result.windows == 0
    assert result.error
    assert fq.assess([result])["level"] == "ok"


def test_silence_measures_no_reach_rather_than_the_whole_spectrum(tmp_path):
    """A flat spectrum sits within any margin of its own top, so silence would
    otherwise be reported as carrying energy to Nyquist."""
    result = _analyse(tmp_path, "silent.wav", np.zeros(fq.FFT_SIZE * 4))
    assert result.windows > 0
    assert result.reach_hz == 0.0
    assert fq.classify(result) == "silent"


def test_a_corrupt_spectrum_never_yields_a_non_finite_measurement():
    """NaN serialises to a literal the browser's JSON.parse rejects."""
    freqs = np.fft.rfftfreq(fq.FFT_SIZE, 1 / RATE)
    wall = fq.measure_wall(freqs, np.full(len(freqs), np.nan))
    assert not wall.found and wall.reach_hz == 0.0
    assert all(math.isfinite(v) for v in wall)
    json.dumps({"drop_db": fq._finite(float("nan"))}, allow_nan=False)


def test_a_failed_plot_costs_only_its_own_file(tmp_path, monkeypatch):
    def boom(_freqs, _db, _rate, out_path, *_args, **_kwargs):
        # A half-written file is what a real failure leaves; the cleanup has to remove it.
        with open(out_path, "wb") as partial:
            partial.write(b"\x89PNG\r\n\x1a\n")
        raise OSError("no space left on device")

    monkeypatch.setattr(fq, "render_plot", boom)
    result = _analyse(tmp_path, "full.wav", _noise(3))
    assert result.error == "no space left on device"
    assert result.image == ""
    assert not (tmp_path / "01 Frequency.png").exists(), "a half-written plot would still be listed and posted"


def test_a_plot_is_written_with_the_wall_marked(tmp_path):
    samples = _lowpass(_noise(3), 16_500)
    freqs, db, rate = fq.average_spectrum(samples.astype(np.float32), RATE), None, RATE
    freqs, db, _windows = freqs
    out = tmp_path / "plot.png"
    fq.render_plot(freqs, db, rate, str(out), "title", fq.measure_wall(freqs, db))
    assert out.stat().st_size > 0


def test_plotting_an_impossible_sample_rate_fails_clearly():
    freqs = np.fft.rfftfreq(fq.FFT_SIZE, 1 / RATE)
    with pytest.raises(ValueError, match="0 Hz"):
        fq.render_plot(freqs, np.zeros(len(freqs)), 0, "unused.png", "title")


def test_a_result_without_a_sample_rate_is_not_measured():
    unusable = fq.SpectrumResult(file="x.flac", image="i", sample_rate=0, windows=1, reach_hz=100.0)
    assert fq.assess([unusable])["notes"] == ["No file could be analysed."]
