"""Average-spectrum plots — the view where a lossy cutoff is a cliff, not a guess.

A spectrogram shows every moment and asks you to spot a pattern; averaging the
whole track into one curve turns a 16 kHz lowpass into an unmissable drop. Uses
PyAV, numpy and Pillow, all already dependencies — no ffmpeg binary needed.
"""

import asyncio
import os

import av
import msgspec
import numpy as np
from PIL import Image, ImageDraw

from salmon.common.progress import report_progress

FFT_SIZE = 8192
MAX_WINDOWS = 6000  # ~9 min at 44.1 kHz with 50% overlap; plenty for an average
_WIDTH, _HEIGHT = 1400, 620
_PAD_L, _PAD_R, _PAD_T, _PAD_B = 70, 20, 34, 46
_DB_MIN, _DB_MAX = -140.0, 0.0
# A cutoff is only meaningful relative to the track's own level.
CUTOFF_FLOOR_DB = 60.0
# Digital silence averages to the epsilon floor (-300 dB). Any real audio, even
# a single LSB of dither, lands far above this.
SILENCE_DB = -250.0

_BG = (16, 16, 20)
_GRID = (48, 48, 56)
_AXIS = (90, 90, 100)
_LABEL = (150, 150, 160)
_CURVE = (255, 170, 60)


class SpectrumResult(msgspec.Struct, frozen=True):
    """One track's averaged spectrum, reduced to what the UI reports."""

    file: str
    image: str
    sample_rate: int
    cutoff_hz: float
    windows: int
    drop_db: float = 0.0
    error: str | None = None


def average_spectrum(path: str) -> tuple[np.ndarray, np.ndarray, int, int]:
    """Welch-average the whole track into one magnitude curve.

    Returns (frequencies, dB, sample_rate, windows_averaged).
    """
    container = av.open(path)
    try:
        stream = container.streams.audio[0]
        sample_rate = stream.rate or 44100
        # Downmix: a cutoff that exists in one channel only still shows here.
        resampler = av.AudioResampler(format="fltp", layout="mono", rate=sample_rate)
        window = np.hanning(FFT_SIZE).astype(np.float32)
        acc = np.zeros(FFT_SIZE // 2 + 1, dtype=np.float64)
        windows = 0
        buf = np.zeros(0, dtype=np.float32)

        for frame in container.decode(stream):
            for resampled in resampler.resample(frame):
                buf = np.concatenate([buf, resampled.to_ndarray().reshape(-1)])
                while len(buf) >= FFT_SIZE and windows < MAX_WINDOWS:
                    acc += np.abs(np.fft.rfft(buf[:FFT_SIZE] * window)) ** 2
                    windows += 1
                    buf = buf[FFT_SIZE // 2 :]  # 50% overlap
            if windows >= MAX_WINDOWS:
                break
        # The resampler buffers; without a flush its last partial frame is lost.
        for resampled in resampler.resample(None):
            buf = np.concatenate([buf, resampled.to_ndarray().reshape(-1)])
        while len(buf) >= FFT_SIZE and windows < MAX_WINDOWS:
            acc += np.abs(np.fft.rfft(buf[:FFT_SIZE] * window)) ** 2
            windows += 1
            buf = buf[FFT_SIZE // 2 :]
    finally:
        container.close()

    power = acc / max(windows, 1)
    # Hann coherent gain is 0.5, so a full-scale sine lands at ~0 dBFS.
    db = 10 * np.log10(power / (FFT_SIZE * 0.5) ** 2 + 1e-30)
    return np.fft.rfftfreq(FFT_SIZE, 1 / sample_rate), db, sample_rate, windows


def find_cutoff(freqs: np.ndarray, db: np.ndarray) -> float:
    """Highest frequency still within CUTOFF_FLOOR_DB of the track's peak.

    Silence has to be caught before that comparison: a flat spectrum sits within
    any floor of its own maximum, so a silent track would otherwise measure as
    carrying energy all the way to Nyquist.
    """
    if db.max() <= SILENCE_DB:
        return 0.0
    above = np.nonzero(db > db.max() - CUTOFF_FLOOR_DB)[0]
    return float(freqs[above[-1]]) if len(above) else 0.0


def cutoff_drop(freqs: np.ndarray, db: np.ndarray, cutoff_hz: float, span_hz: float = 1000.0) -> float:
    """How hard the level falls across the kHz above the cutoff.

    This is the measurement that separates the two innocent-looking cases: a
    lossy encoder drops off a cliff, a mastering fade slopes away.
    """
    below = db[(freqs >= cutoff_hz - span_hz) & (freqs <= cutoff_hz)]
    above = db[(freqs > cutoff_hz) & (freqs <= cutoff_hz + span_hz)]
    if not len(below) or not len(above):
        return 0.0
    return float(below.mean() - above.mean())


def render_plot(freqs: np.ndarray, db: np.ndarray, sample_rate: int, out_path: str, title: str) -> None:
    """Draw the curve with a labelled kHz/dB grid."""
    img = Image.new("RGB", (_WIDTH, _HEIGHT), _BG)
    draw = ImageDraw.Draw(img)
    plot_w = _WIDTH - _PAD_L - _PAD_R
    plot_h = _HEIGHT - _PAD_T - _PAD_B
    nyquist = sample_rate / 2

    def x_of(hz: float) -> float:
        return _PAD_L + plot_w * (hz / nyquist)

    def y_of(value: float) -> float:
        clamped = min(max(value, _DB_MIN), _DB_MAX)
        return _PAD_T + plot_h * (1 - (clamped - _DB_MIN) / (_DB_MAX - _DB_MIN))

    draw.text((_PAD_L, 10), title, fill=_LABEL)
    for khz in range(0, int(nyquist / 1000) + 1, 2):
        x = x_of(khz * 1000)
        draw.line([(x, _PAD_T), (x, _PAD_T + plot_h)], fill=_GRID)
        draw.text((x - 8, _PAD_T + plot_h + 6), f"{khz}k", fill=_LABEL)
    for value in range(int(_DB_MIN), int(_DB_MAX) + 1, 20):
        y = y_of(value)
        draw.line([(_PAD_L, y), (_PAD_L + plot_w, y)], fill=_GRID)
        draw.text((8, y - 6), f"{value} dB", fill=_LABEL)
    draw.rectangle([_PAD_L, _PAD_T, _PAD_L + plot_w, _PAD_T + plot_h], outline=_AXIS)

    draw.line([(x_of(f), y_of(v)) for f, v in zip(freqs, db, strict=True)], fill=_CURVE, width=2)
    img.save(out_path)


def _analyse_one(album_path: str, filename: str, out_dir: str, index: int) -> SpectrumResult:
    try:
        freqs, db, sample_rate, windows = average_spectrum(os.path.join(album_path, filename))
    except Exception as e:
        # One unreadable file must not cost the spectrograms that did generate.
        return SpectrumResult(file=filename, image="", sample_rate=0, cutoff_hz=0.0, windows=0, error=str(e))
    if not windows:
        # An all-zero spectrum is flat, so every bin sits within the floor of the
        # maximum and find_cutoff would report Nyquist for audio never measured.
        return SpectrumResult(
            file=filename,
            image="",
            sample_rate=sample_rate,
            cutoff_hz=0.0,
            windows=0,
            error="too little audio to average a spectrum",
        )
    image = f"{index + 1:02d} Frequency.png"
    render_plot(freqs, db, sample_rate, os.path.join(out_dir, image), filename)
    cutoff = find_cutoff(freqs, db)
    return SpectrumResult(
        file=filename,
        image=image,
        sample_rate=sample_rate,
        cutoff_hz=cutoff,
        windows=windows,
        drop_db=cutoff_drop(freqs, db, cutoff),
    )


# Below this much of Nyquist, the roll-off is worth a second look.
_LOOK_RATIO = 0.91
# A fall this steep over 1 kHz is a filter, not a fade. Plenty of honest masters
# roll off early, so the cutoff alone must not raise a flag.
_CLIFF_DB = 25.0
# Two tracks off the same master do not stop at frequencies this far apart.
_DISAGREEMENT_HZ = 1500.0


def assess(results: list[SpectrumResult]) -> dict:
    """Say what the numbers show, never whether a file is a transcode.

    Automated transcode verdicts are not trusted by the trackers and should not
    be offered here; a measured cutoff and a disagreement between tracks are
    facts, and they are what a human wants pointed at.
    """
    if not results:
        return {"level": "ok", "notes": []}

    analysed = [r for r in results if not r.error]
    if not analysed:
        return {"level": "ok", "notes": ["No file could be analysed."]}

    notes: list[str] = []
    level = "ok"
    # A silent track measures 0 Hz; counting it would fake a disagreement.
    cutoffs = [r.cutoff_hz for r in analysed if r.cutoff_hz > 0]
    if not cutoffs:
        return {"level": "ok", "notes": ["No track carried enough signal to measure."]}
    spread = max(cutoffs) - min(cutoffs)

    for result in analysed:
        nyquist = result.sample_rate / 2
        if not result.cutoff_hz or result.cutoff_hz >= nyquist * _LOOK_RATIO:
            continue
        if result.drop_db >= _CLIFF_DB:
            level = "look"
            notes.append(
                f"{result.file}: level falls {result.drop_db:.0f} dB in the kHz above "
                f"{result.cutoff_hz / 1000:.1f} kHz. That is the shape a lossy encoder leaves, "
                f"so read the curve before trusting the file."
            )
        else:
            notes.append(
                f"{result.file}: rolls off from {result.cutoff_hz / 1000:.1f} kHz, gently "
                f"({result.drop_db:.0f} dB across the next kHz) — that is a mastering choice, not a filter."
            )

    if spread > _DISAGREEMENT_HZ:
        level = "suspect"
        notes.append(
            f"Tracks stop at different frequencies ({min(cutoffs) / 1000:.1f}–{max(cutoffs) / 1000:.1f} kHz). "
            f"One master does not do that, so this folder was probably assembled from more than one source."
        )

    if not notes:
        notes.append(f"Every track carries energy to {min(cutoffs) / 1000:.1f} kHz or above.")
    if level == "ok" and len(notes) > 1:
        notes.insert(0, "Nothing here has the shape of a lossy cutoff.")
    return {"level": level, "notes": notes}


async def generate_frequency_plots(album_path: str, files: list[str], out_dir: str) -> list[SpectrumResult]:
    """One averaged-spectrum plot per file, written into an existing folder."""
    results: list[SpectrumResult] = []
    for index, filename in enumerate(files):
        results.append(await asyncio.to_thread(_analyse_one, album_path, filename, out_dir, index))
        report_progress(index + 1, len(files), f"Frequency analysis ({filename})")
    return results
