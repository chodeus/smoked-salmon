"""Turn album check results into a go/no-go verdict before an upload starts.

Advisory only — upload() re-runs these checks itself and stays the authority. This
exists so an album can be seen green before anything is staged or uploaded.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal

import msgspec

import salmon.trackers
from salmon.checks import album, provenance
from salmon.checks.blacklist import red_blacklist_reason
from salmon.checks.source import detect_source
from salmon.tagger.pre_data import construct_artists_li, parse_title
from salmon.tagger.tags import gather_tags
from salmon.uploader.dupe_checker import generate_dupe_check_searchstrs, get_search_results

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
    """One album check: how to run it, and how to read its result."""

    id: str
    label: str
    run: Callable[[str], Awaitable[dict]]
    verdict: Callable[[dict, dict], tuple[Verdict, str]]


def _integrity_verdict(result: dict, _ctx: dict) -> tuple[Verdict, str]:
    if result["passed"]:
        return OK, "Every file decodes cleanly."
    return BLOCK, result["details"] or "One or more files failed to decode."


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


def _log_verdict(result: dict, ctx: dict) -> tuple[Verdict, str]:
    """Only CD rips are expected to carry a log."""
    source = ctx.get("source")
    logs = result["logs"]
    if source and source != "CD":
        return SKIP, f"Not applicable to a {source} release."
    if not logs:
        return WARN, "No rip log, so the rip cannot be verified. Allowed, but it scores 0 on RED."
    broken = [x for x in logs if "error" in x]
    scored = [x for x in logs if "score" in x]
    # One good log does not vouch for a sibling that would not parse.
    if broken:
        return WARN, f"Could not parse {len(broken)} of {len(logs)} log(s)."
    worst = min(scored, key=lambda x: x["score"])
    mismatched = [x for x in scored if x["checksum_integrity"] != "Match"]
    if mismatched:
        return WARN, f"Log checksum does not match the audio ({mismatched[0]['file']})."
    if worst["score"] < 100:
        return WARN, f"Score {worst['score']}/100 ({worst['file']})."
    return OK, f"Score 100/100 across {len(scored)} log(s)."


CHECKS: tuple[CheckSpec, ...] = (
    CheckSpec("provenance", "Provenance", album.run_provenance_check, _provenance_verdict),
    CheckSpec("integrity", "File integrity", album.run_integrity_check, _integrity_verdict),
    CheckSpec("upconvert", "Upconvert", album.run_upconvert_check, _upconvert_verdict),
    CheckSpec("mqa", "MQA", album.run_mqa_check, _mqa_verdict),
    CheckSpec("log", "Rip log", album.run_log_check, _log_verdict),
)
CHECK_IDS: tuple[str, ...] = tuple(spec.id for spec in CHECKS)


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


def dupe_row(tracker: str, results: list[dict]) -> Row:
    label = f"Duplicate ({tracker})"
    if not results:
        return Row(f"dupe:{tracker}", label, OK, "No existing release found.")
    names = [str(r.get("groupName") or r.get("groupId")) for r in results[:2]]
    detail = f"{len(results)} possible match(es): {', '.join(names)}. Confirm it is not a duplicate."
    return Row(f"dupe:{tracker}", label, WARN, detail)


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
                    "remasterYear": t.get("remasterYear"),
                    "remasterRecordLabel": t.get("remasterRecordLabel"),
                    "seeders": t.get("seeders"),
                }
                for t in r.get("torrents", [])
            ],
        }
        for r in results
    ]


def blacklist_row(tracker: str, reason: str | None) -> Row:
    label = f"{tracker} blacklist"
    if reason:
        return Row(f"blacklist:{tracker}", label, BLOCK, reason)
    return Row(f"blacklist:{tracker}", label, OK, "Not on the Do-Not-Upload list.")


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
        title, _edition = parse_title(first.album) if first.album else (None, None)
        return {"artists": construct_artists_li(tags), "title": title, "label": first.label, "catno": first.catno}
    except Exception:
        return {}


async def _tracker_rows(tracker: str, identity: dict) -> tuple[list[Row], dict]:
    """Verdict rows for one tracker, plus the matches behind them for the UI to list."""
    rows: list[Row] = []
    raw: dict[str, dict] = {}
    searchstrs = generate_dupe_check_searchstrs(identity["artists"], identity["title"], identity["catno"])
    try:
        site = salmon.trackers.get_class(tracker)()
        results = await get_search_results(site, searchstrs) if searchstrs else []
    except Exception as e:
        rows.append(Row(f"dupe:{tracker}", f"Duplicate ({tracker})", WARN, f"Could not search {tracker}: {e}"))
    else:
        rows.append(dupe_row(tracker, results))
        raw[f"dupe:{tracker}"] = {"searchstrs": searchstrs, "matches": dupe_matches(site.base_url, results)}
    if tracker == "RED":
        try:
            reason = red_blacklist_reason(identity["artists"], identity["title"], identity["label"])
        except Exception as e:
            # Fail closed: an unreadable blacklist must not silently clear a release.
            rows.append(Row("blacklist:RED", "RED blacklist", BLOCK, f"Could not check the blacklist: {e}"))
        else:
            rows.append(blacklist_row(tracker, reason))
    return rows, raw


async def run_checks(
    path: str,
    checks: list[str] | None = None,
    source: str | None = None,
    trackers: list[str] | None = None,
) -> dict:
    """Run the selected album checks and return verdict rows plus the raw results.

    checks defaults to all of them; trackers adds a duplicate search per site and,
    for RED, a blacklist row. Pass no trackers to check the files alone.
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
        raw[spec.id] = await spec.run(path)
        verdict, detail = spec.verdict(raw[spec.id], ctx)
        rows.append(Row(spec.id, spec.label, verdict, detail))

    if trackers:
        identity = await asyncio.to_thread(_release_identity, path)
        if identity.get("title"):
            for tracker in trackers:
                tracker_rows, tracker_raw = await _tracker_rows(tracker, identity)
                rows.extend(tracker_rows)
                raw.update(tracker_raw)
        else:
            rows.append(Row("dupe", "Duplicate", WARN, "Tags could not be read, so no duplicate search could be run."))

    return {
        "rows": [msgspec.to_builtins(r) for r in rows],
        "raw": raw,
        "blocking": [r.id for r in rows if r.verdict == BLOCK],
        "warnings": [r.id for r in rows if r.verdict == WARN],
    }
