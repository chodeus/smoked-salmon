"""Turn album check results into a go/no-go verdict before an upload starts.

Advisory only — upload() re-runs these checks itself and stays the authority. This
exists so an album can be seen green before anything is staged or uploaded.
"""

import asyncio
import os
import re
from collections.abc import Awaitable, Callable, Collection, Sequence
from typing import Literal

import msgspec

import salmon.trackers
from salmon.checks import album, provenance
from salmon.checks.do_not_upload import LISTS, Candidate, do_not_upload_reason
from salmon.checks.high_rate import sixteen_bit_notice

# Not `from salmon.checks import integrity` — the click Command of that name in
# checks/__init__.py shadows the module.
from salmon.checks.integrity import md5_unset_summary
from salmon.checks.source import detect_source
from salmon.checks.tag_rules import (
    MAX_PATH_LENGTH,
    SIXTEEN_BIT_ABOVE_48KHZ,
    collect_upload_warnings,
    in_torrent_path,
    path_limit_warning,
)
from salmon.tagger.audio_info import gather_audio_info
from salmon.tagger.pre_data import construct_artists_li, parse_format, parse_title
from salmon.tagger.tags import gather_tags
from salmon.uploader.dupe_checker import (
    describe_torrent,
    generate_dupe_check_searchstrs,
    get_search_results,
    held_in_group,
)

Verdict = Literal["ok", "warn", "block", "skip"]
# "block" cannot be overridden in the UI; "warn" needs an explicit acknowledgement.
OK: Verdict = "ok"
WARN: Verdict = "warn"
BLOCK: Verdict = "block"
SKIP: Verdict = "skip"


class Row(msgspec.Struct, frozen=True):
    id: str
    label: str
    verdict: Verdict
    detail: str


class CheckSpec(msgspec.Struct, frozen=True):
    """One album check: how to run it, how to read its result, and the sources it is for (None: every one)."""

    id: str
    label: str
    run: Callable[[str], Awaitable[dict]]
    verdict: Callable[[dict, dict], tuple[Verdict, str]]
    sources: tuple[str, ...] | None = None


def _integrity_verdict(result: dict, _ctx: dict) -> tuple[Verdict, str]:
    if not result["passed"]:
        # A missing checksum and a file that will not decode both fail the check, but
        # only the first is safe to wave through. decode_failures is the fail-closed
        # half: anything flac did not confirm as decoded lands there and still blocks.
        failures = result.get("decode_failures") or []
        md5_unset = result.get("md5_unset") or []
        if failures:
            return BLOCK, result["details"] or f"{len(failures)} file(s) failed to decode."
        if md5_unset:
            # One line, not a per-file dump: the row renders inline, so a 17-track album
            # used to arrive here as 34 lines of near-identical text. Names go to raw.
            summary = md5_unset_summary(len(md5_unset), result.get("checked") or 0)
            return WARN, (
                f"{summary} — decodes cleanly, there is just no checksum stored. Normal for WEB / "
                "web-store downloads. The upload will offer to re-encode (sanitize) the album to set "
                'the MD5; note "WEB download, re-encoded to set MD5" in the description.'
            )
        return BLOCK, result["details"] or "One or more files failed to decode."
    # Decoding is not the same as being sound: mp3val exits 0 while describing
    # the damage, so a file that plays with a complaint must not read as clean.
    concerns = result.get("concerns") or []
    if concerns:
        return WARN, f"Decodes, but {len(concerns)} warning(s) from the checker: {concerns[0]}"
    return OK, "Every file decodes cleanly."


def _mqa_verdict(result: dict, _ctx: dict) -> tuple[Verdict, str]:
    if not result["detected"]:
        return OK, "No MQA encoding detected."
    hits = [f["file"] for f in result["files"] if f["detected"]]
    return BLOCK, f"MQA detected in {len(hits)} file(s): {', '.join(hits[:3])}."


def _upconvert_verdict(result: dict, _ctx: dict) -> tuple[Verdict, str]:
    files = result["files"]
    if not files:
        return SKIP, "No FLAC files to test."
    # 16bit files are out of scope for the check, not failures — they must not warn.
    testable = [f for f in files if not f.get("not_applicable")]
    upconverted = [f["file"] for f in testable if f.get("is_upconverted")]
    if upconverted:
        return BLOCK, f"Upconverted from a lower bit depth: {', '.join(upconverted[:3])}."
    if not testable:
        return SKIP, f"Not applicable to {len(files)} file(s): {files[0]['not_applicable']}"
    errored = [f for f in testable if f.get("error")]
    if errored:
        return WARN, f"Could not test {len(errored)} of {len(testable)} file(s): {errored[0]['error']}"
    return OK, f"Genuine bit depth across {len(testable)} file(s)."


def _provenance_verdict(result: dict, _ctx: dict) -> tuple[Verdict, str]:
    """Report what the tags say about the files' origin.

    Markers alone only warn when the audio contradicts them — an 'EAC' or
    'QOBUZ' comment is ordinary, and warning on every one of them would train
    the acknowledgement checkbox to mean nothing.
    """
    if not result["files"]:
        return SKIP, "No tags could be read."
    if result["contradictions"]:
        return WARN, "; ".join(result["contradictions"][:2]) + "."
    return OK, provenance.describe(result)


def _log_line(log: dict) -> tuple[Verdict, str]:
    """One rip log's verdict as salmon up treats it: an edited log, or audio it cannot check, stops the upload."""
    name = log["file"]
    if "error" in log:
        return WARN, f"{name}: could not be read ({log['error']})."
    if log["checksum_integrity"] == "Mismatch":
        return BLOCK, f"{name}: its checksum does not match, so the log was edited."
    if log["crcs"] == "error":
        return BLOCK, f"{name}: the audio could not be checked against its CRCs ({log['crcs_detail']})."
    issues = []
    if log["checksum_integrity"] != "Match":
        issues.append("no checksum to verify it (EAC signs its logs from 1.0 beta 3), so RED marks it trumpable")
    if log["score"] is None:
        issues.append("no score")
    elif log["score"] < 100:
        issues.append(f"score {log['score']}/100")
    if log["crcs"] == "mismatch":
        issues.append("the audio does not match its CRCs")
    elif log["crcs"] != "match":
        issues.append(f"CRCs not checked: {log['crcs_detail']}")
    if issues:
        return WARN, f"{name}: {'; '.join(issues)}."
    return OK, f"{name}: score 100/100, checksum and CRCs match."


def _log_verdict(result: dict, _ctx: dict) -> tuple[Verdict, str]:
    """Each rip log's verdict; the worst decides, and one good log does not vouch for another."""
    logs = result["logs"]
    if not logs:
        return WARN, "No rip log, so the rip cannot be verified. Allowed, but it scores 0 on RED."
    lines = [_log_line(log) for log in logs]
    flagged = [detail for verdict, detail in lines if verdict != OK]
    if not flagged:
        return OK, f"Score 100/100, checksum and CRCs match in {len(logs)} log(s)."
    passing = len(logs) - len(flagged)
    verdict = BLOCK if any(line_verdict == BLOCK for line_verdict, _ in lines) else WARN
    return verdict, " ".join(flagged) + (f" {passing} other log(s) pass." if passing else "")


def _tags_verdict(result: dict, _ctx: dict) -> tuple[Verdict, str]:
    if result["dual_id3"]:
        names = ", ".join(result["dual_id3"][:3])
        return WARN, (
            f"{len(result['dual_id3'])} MP3(s) with a filled-in ID3v1 tag beside a blank ID3v2 tag, which RED and OPS "
            f"can trump: {names}."
        )
    if result["errors"]:
        first = result["errors"][0]
        return (
            WARN,
            f"Could not read the ID3v2 tag of {len(result['errors'])} MP3(s): {first['file']} ({first['error']}).",
        )
    if not result["mp3s"]:
        return SKIP, "No MP3 file."
    return OK, f"No filled-in ID3v1 tag beside a blank ID3v2 one in {result['mp3s']} MP3(s)."


CHECKS: tuple[CheckSpec, ...] = (
    CheckSpec("provenance", "Provenance", album.run_provenance_check, _provenance_verdict),
    CheckSpec("integrity", "File integrity", album.run_integrity_check, _integrity_verdict),
    CheckSpec("upconvert", "Upconvert", album.run_upconvert_check, _upconvert_verdict),
    CheckSpec("mqa", "MQA", album.run_mqa_check, _mqa_verdict),
    CheckSpec("tags", "MP3 tags", album.run_tag_check, _tags_verdict),
    # salmon up checks logs for a CD only; an unknown source is checked too.
    CheckSpec("log", "Rip log", album.run_log_check, _log_verdict, sources=("CD",)),
)
CHECK_IDS: tuple[str, ...] = tuple(spec.id for spec in CHECKS)
# The trackers whose rules a check with none named applies; their Do-Not-Upload lists ship with salmon.
RULE_TRACKERS: tuple[str, ...] = ("RED", "OPS")


def source_row(guess: dict, chosen: str | None) -> Row:
    """Verdict on the media source, comparing what was detected to what was picked."""
    detected, confidence = guess["source"], guess["confidence"]
    why = " ".join(guess["reasons"])
    if not chosen:
        if confidence == "unknown":
            return Row("source", "Source", BLOCK, f"Could not be determined. {why}")
        return Row("source", "Source", BLOCK, f"Looks like {detected} ({confidence}). {why} Pick a source to continue.")
    if detected and detected != chosen:
        return Row("source", "Source", WARN, f"You picked {chosen}, but the files look like {detected}. {why}")
    if confidence == "unknown":
        return Row("source", "Source", WARN, f"{chosen} cannot be verified from the files. {why}")
    return Row("source", "Source", OK, f"{chosen} — {why}")


def _edition_year(torrent: dict, group: dict) -> int | None:
    """An original release has no remaster year of its own; it carries the group's."""
    return torrent.get("remasterYear") or group.get("groupYear")


def _editions(group: dict) -> str:
    """A matched group's editions as "year / catalogue number: formats", so a different release is visible."""
    editions: dict[str, list[str]] = {}
    for t in group.get("torrents") or []:
        edition = (_edition_year(t, group), t.get("remasterCatalogueNumber"))
        key = " / ".join(str(part) for part in edition if part) or "edition unknown"
        fmt = " ".join(str(part) for part in (t.get("media"), t.get("format"), t.get("encoding")) if part)
        editions.setdefault(key, []).append(fmt)
    return "; ".join(f"{key}: {', '.join(fmts)}" for key, fmts in editions.items())


def dupe_row(tracker: str, results: list[dict], release: dict | None = None) -> Row:
    """A tracker's matches weighed as salmon up weighs them: a group already holding this edition's format warns."""
    label = f"Duplicate ({tracker})"
    if not results:
        return Row(f"dupe:{tracker}", label, OK, "No existing release found.")
    names = []
    for r in results[:2]:
        name = str(r.get("groupName") or r.get("groupId"))
        editions = _editions(r)
        names.append(f"{name} ({editions})" if editions else name)
    found = f"{len(results)} possible match(es): {', '.join(names)}."
    wanted = [str((release or {}).get(key) or "") for key in ("source", "format", "encoding")]
    if not all(wanted):
        return Row(
            f"dupe:{tracker}",
            label,
            WARN,
            f"{found} This release's source or encoding is not known from the files, so check them by hand.",
        )
    held = [describe_torrent(torrent, group) for group in results for torrent in held_in_group(group, release)]
    if held:
        return Row(
            f"dupe:{tracker}",
            label,
            WARN,
            f"{tracker} already has this edition's {' '.join(wanted)}: {'; '.join(held[:2])}.",
        )
    return Row(f"dupe:{tracker}", label, OK, f"{found} None holds this edition's {' '.join(wanted)}.")


def dupe_matches(base_url: str, results: list[dict]) -> list[dict]:
    """Trim browse results to the fields the UI lists, so every match can be inspected.

    An allowlist, not a passthrough: the raw result carries download URLs that
    embed the torrent pass.
    """
    return [
        {
            "groupId": r.get("groupId"),
            "groupName": r.get("groupName"),
            "artist": r.get("artist"),
            "groupYear": r.get("groupYear"),
            "releaseType": r.get("releaseType"),
            "url": f"{base_url}/torrents.php?id={r.get('groupId')}",
            "torrents": [
                {
                    "torrentId": t.get("torrentId"),
                    "format": t.get("format"),
                    "encoding": t.get("encoding"),
                    "media": t.get("media"),
                    "hasLog": t.get("hasLog"),
                    "logScore": t.get("logScore"),
                    "remasterTitle": t.get("remasterTitle"),
                    "remasterYear": _edition_year(t, r),
                    "remasterRecordLabel": t.get("remasterRecordLabel"),
                    "remasterCatalogueNumber": t.get("remasterCatalogueNumber"),
                    "seeders": t.get("seeders"),
                }
                for t in r.get("torrents", [])
            ],
        }
        for r in results
    ]


def sixteen_bit_row(tracker: str, track_data: dict) -> Row | None:
    """The tracker's rule on 16bit files above 48 kHz, when the files break it: blocks for a refusal."""
    rule = SIXTEEN_BIT_ABOVE_48KHZ.get(tracker, "")
    if not (notice := sixteen_bit_notice(tracker, rule, track_data)):
        return None
    return Row(
        f"sixteen-bit:{tracker}", f"16bit above 48 kHz ({tracker})", BLOCK if rule == "refused" else WARN, notice
    )


def rules_row(tracker: str, folder_name: str, track_data: dict, other_files: Sequence[str] = ()) -> Row:
    """Path-length and sample-rate rules, for the audio and, by path, every other file the torrent holds."""
    label = f"{tracker} rules"
    long_paths = [
        warning for name in other_files if (warning := path_limit_warning(tracker, in_torrent_path(folder_name, name)))
    ]
    if not track_data:
        if long_paths:
            return Row(f"rules:{tracker}", label, WARN, " ".join(long_paths[:2]))
        return Row(f"rules:{tracker}", label, SKIP, "The audio could not be read, so these rules were not checked.")
    warnings = [*collect_upload_warnings(tracker, folder_name, track_data), *long_paths]
    if not warnings:
        limit = MAX_PATH_LENGTH.get(tracker)
        paths = f"Paths are within {tracker}'s {limit}-character limit" if limit else f"{tracker} sets no path limit"
        return Row(f"rules:{tracker}", label, OK, f"{paths}, and sample rates are standard.")
    return Row(f"rules:{tracker}", label, WARN, " ".join(warnings[:2]))


def do_not_upload_row(tracker: str, reason: str | None) -> Row:
    label = f"Do-Not-Upload ({tracker})"
    if reason:
        return Row(f"do-not-upload:{tracker}", label, BLOCK, reason)
    return Row(f"do-not-upload:{tracker}", label, OK, f"Not on {tracker}'s Do-Not-Upload list.")


def _release_identity(path: str) -> dict:
    """Artists, title and catalogue number from the tags, for dupe searching.

    Returns {} when the tags cannot be read — a duplicate search is not worth
    failing the whole pre-flight over.
    """
    try:
        tags = gather_tags(path)
        if not tags:
            return {}
        first = next(iter(tags.values()))
        title, edition = parse_title(first.album) if first.album else (None, None)
        year = re.search(r"\d{4}", str(first.date or ""))
        return {
            "artists": construct_artists_li(tags),
            "title": title,
            "edition_title": edition,
            "year": year[0] if year else None,
            "label": first.label,
            "catno": first.catno,
        }
    except Exception:
        return {}


def _other_files(path: str, audio: Collection[str]) -> list[str]:
    """Every file of the album but its audio, relative to it: a log or a cover counts against the path limit too."""
    return sorted(
        relative
        for root, _dirs, files in os.walk(path)
        for name in files
        if (relative := os.path.relpath(os.path.join(root, name), path)) not in audio
    )


def _audio_info(path: str) -> dict:
    """Per-track audio properties, or {} when a file will not parse.

    Mirrors _release_identity: one unreadable file must not cost the whole
    pre-flight, which is the only reason any of the other rows get to run.
    """
    try:
        return gather_audio_info(path, True)
    except Exception:
        return {}


def _release(identity: dict, track_data: dict, source: str | None) -> dict:
    """The release as salmon up's rls_data names its edition: source, format and encoding beside the tags."""
    try:
        release_format = parse_format(next(iter(track_data))) if track_data else None
    except KeyError:
        release_format = None
    precisions = {info.get("precision") for info in track_data.values()}
    encoding = None
    if release_format == "FLAC":
        encoding = "24bit Lossless" if 24 in precisions else "Lossless" if 16 in precisions else None
    return {**identity, "source": source, "format": release_format, "encoding": encoding}


async def _tracker_rows(tracker: str, release: dict, search: bool = True) -> tuple[list[Row], dict]:
    """Verdict rows for one tracker, plus the matches behind them for the UI to list; without search, no request."""
    rows: list[Row] = []
    raw: dict[str, dict] = {}
    if tracker in LISTS:
        # An unreadable list gives a reason too: it refuses everything, so the row blocks.
        reason = do_not_upload_reason(tracker, Candidate.from_metadata(release))
        rows.append(do_not_upload_row(tracker, reason))
        if reason:
            # As an upload does: a listed release is not searched for there.
            rows.append(Row(f"dupe:{tracker}", f"Duplicate ({tracker})", SKIP, "Not searched: the list forbids it."))
            return rows, raw
    if not search:
        return rows, raw
    searchstrs = generate_dupe_check_searchstrs(release["artists"], release["title"], release["catno"])
    try:
        site = salmon.trackers.get_class(tracker)()
    except Exception as e:
        rows.append(Row(f"dupe:{tracker}", f"Duplicate ({tracker})", WARN, f"Could not search {tracker}: {e}"))
        return rows, raw
    try:
        results = await get_search_results(site, searchstrs) if searchstrs else []
        row = dupe_row(tracker, results, release)
        matches = dupe_matches(site.base_url, results)
    except (KeyError, TypeError, AttributeError) as e:
        # An answer without the fields a search gives, such as a null group: say so rather than stop the checks.
        rows.append(
            Row(f"dupe:{tracker}", f"Duplicate ({tracker})", WARN, f"{tracker} gave an unexpected answer ({e!r}).")
        )
    except Exception as e:
        rows.append(Row(f"dupe:{tracker}", f"Duplicate ({tracker})", WARN, f"Could not search {tracker}: {e}"))
    else:
        rows.append(row)
        raw[f"dupe:{tracker}"] = {"searchstrs": searchstrs, "matches": matches}
    finally:
        await site.close()
    return rows, raw


async def run_checks(
    path: str,
    checks: list[str] | None = None,
    source: str | None = None,
    trackers: list[str] | None = None,
    search: bool = True,
) -> dict:
    """Run the selected album checks and return verdict rows plus the raw results.

    checks defaults to all of them; trackers adds their rules and, for RED and OPS, a Do-Not-Upload row, and with
    search a duplicate search on each. Pass no trackers to check the files alone.
    """
    selected = CHECK_IDS if checks is None else tuple(checks)
    guess = await asyncio.to_thread(detect_source, path)
    ctx = {"source": source or (guess["source"] if guess["confidence"] == "confirmed" else None)}
    rows: list[Row] = [source_row(guess, source)]
    raw: dict[str, dict] = {"source": guess}

    for spec in CHECKS:
        if spec.id not in selected:
            rows.append(Row(spec.id, spec.label, SKIP, "Not selected."))
            continue
        if spec.sources and ctx["source"] and ctx["source"] not in spec.sources:
            rows.append(Row(spec.id, spec.label, SKIP, f"Not applicable to a {ctx['source']} release."))
            continue
        raw[spec.id] = await spec.run(path)
        verdict, detail = spec.verdict(raw[spec.id], ctx)
        rows.append(Row(spec.id, spec.label, verdict, detail))

    if trackers:
        identity = await asyncio.to_thread(_release_identity, path)
        track_data = await asyncio.to_thread(_audio_info, path)
        # normpath first: basename("/album/") is "", which would drop the folder
        # out of the path-length sum and under-report it.
        folder = os.path.basename(os.path.normpath(path))
        other_files = await asyncio.to_thread(_other_files, path, track_data)
        # Path length and sample rate depend on the files, not on readable tags,
        # so they are checked even when the duplicate search cannot run.
        for tracker in trackers:
            rows.append(rules_row(tracker, folder, track_data, other_files))
            if row := sixteen_bit_row(tracker, track_data):
                rows.append(row)
        if identity.get("title"):
            release = _release(identity, track_data, ctx["source"])
            for tracker in trackers:
                tracker_rows, tracker_raw = await _tracker_rows(tracker, release, search)
                rows.extend(tracker_rows)
                raw.update(tracker_raw)
        else:
            if search:
                rows.append(
                    Row("dupe", "Duplicate", WARN, "Tags could not be read, so no duplicate search could be run.")
                )
            rows.extend(
                Row(f"do-not-upload:{tracker}", f"Do-Not-Upload ({tracker})", SKIP, "Not checked: no album title.")
                for tracker in trackers
                if tracker in LISTS
            )

    return {
        "rows": [msgspec.to_builtins(r) for r in rows],
        "raw": raw,
        "blocking": [r.id for r in rows if r.verdict == BLOCK],
        "warnings": [r.id for r in rows if r.verdict == WARN],
    }
