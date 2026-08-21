"""Provenance: encoder/source markers, and claims the audio contradicts."""

from types import SimpleNamespace

from salmon.checks import preflight as pf
from salmon.checks import provenance as pv


class FakeTags(dict):
    """Stands in for a VCFLACDict: a dict of lists, plus a vendor string."""

    def __init__(self, vendor=None, **fields):
        super().__init__({k.replace("_", " "): [v] for k, v in fields.items()})
        self.vendor = vendor


def tagfile(vendor=None, bitdepth: int | None = 16, **fields):
    info = SimpleNamespace(bits_per_sample=bitdepth)
    return SimpleNamespace(mut=SimpleNamespace(tags=FakeTags(vendor, **fields), info=info))


def test_markers_and_vendor_are_read_off_the_tags():
    entry = pv._file_provenance("01.flac", tagfile(vendor="reference libFLAC 1.2.1", comment="hd24bit.com"))
    assert entry["vendor"] == "reference libFLAC 1.2.1"
    assert entry["markers"] == {"comment": "hd24bit.com"}
    assert entry["bitdepth"] == 16


def test_a_file_without_tags_reads_as_empty_rather_than_raising():
    entry = pv._file_provenance("01.flac", SimpleNamespace(mut=None))
    assert entry["markers"] == {}
    assert entry["vendor"] is None


def test_a_bit_depth_claim_the_audio_contradicts_is_reported():
    files = [pv._file_provenance("01.flac", tagfile(comment="24bit remaster", bitdepth=16))]
    found = pv._contradictions(files)
    assert len(found) == 1
    assert "claims 24bit" in found[0]
    assert "is 16bit" in found[0]


def test_a_matching_claim_is_not_a_contradiction():
    files = [pv._file_provenance("01.flac", tagfile(comment="24bit master", bitdepth=24))]
    assert pv._contradictions(files) == []


def test_a_file_with_no_readable_depth_cannot_contradict_anything():
    files = [pv._file_provenance("01.flac", tagfile(comment="24bit", bitdepth=None))]
    assert pv._contradictions(files) == []


def test_urls_are_picked_out_of_marker_text():
    assert pv._URL_RE.findall("ripped from hd24bit.com by hand") == ["hd24bit.com"]
    assert pv._URL_RE.findall("https://example.org/x") == ["https://example.org/x"]
    assert pv._URL_RE.findall("EAC FLAC -8") == []


def test_verdict_warns_only_when_the_audio_contradicts_a_claim():
    contradicted = {"files": [{}], "vendors": [], "markers": [], "urls": [], "contradictions": ["01.flac: bad"]}
    assert pf._provenance_verdict(contradicted, {})[0] == pf.WARN


def test_ordinary_markers_report_without_demanding_an_acknowledgement():
    # An 'EAC' comment is normal; warning on it would make the ack checkbox meaningless.
    ordinary = {
        "files": [{}],
        "vendors": ["reference libFLAC 1.3.2"],
        "markers": ["comment: EAC FLAC -8"],
        "urls": [],
        "contradictions": [],
    }
    verdict, detail = pf._provenance_verdict(ordinary, {})
    assert verdict == pf.OK
    assert "libFLAC 1.3.2" in detail
    assert "EAC" in detail


def test_unreadable_tags_are_skipped_not_failed():
    empty = {"files": [], "vendors": [], "markers": [], "urls": [], "contradictions": []}
    assert pf._provenance_verdict(empty, {})[0] == pf.SKIP


def test_a_depth_inside_a_domain_is_a_name_not_a_claim():
    # "hd24bit.com" is who ripped it. Reading that as a 24bit claim warned on
    # every file from that ripper, which teaches you to ignore the row.
    files = [pv._file_provenance("01.flac", tagfile(comment="hd24bit.com", bitdepth=16))]
    assert pv._contradictions(files) == []


def test_a_real_claim_beside_a_domain_is_still_caught():
    files = [pv._file_provenance("01.flac", tagfile(comment="24bit master from hd24bit.com", bitdepth=16))]
    assert len(pv._contradictions(files)) == 1


def test_a_domain_with_a_path_is_consumed_whole():
    """Stripping only the domain left "/24bit" behind, which read as a claim."""
    for marker in ("hd24bit.com/24bit", "hd24bit.com:8080/24bit", "from hd24bit.com/releases/24bit-master"):
        files = [pv._file_provenance("01.flac", tagfile(comment=marker, bitdepth=16))]
        assert pv._contradictions(files) == [], marker


def test_consuming_the_url_does_not_swallow_the_text_after_it():
    # A comma is not part of a path, so a claim following a domain still counts.
    files = [pv._file_provenance("01.flac", tagfile(comment="hd24bit.com, 24bit master", bitdepth=16))]
    assert len(pv._contradictions(files)) == 1
