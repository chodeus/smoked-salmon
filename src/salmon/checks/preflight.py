"""Pre-upload verification: run every quality check and return a go/no-go verdict.

Advisory only — upload() re-runs these checks itself and stays the authority. This
exists so an album can be seen green before anything is staged or uploaded.
"""

import asyncio
import os

import asyncclick as click
import cambia
import msgspec

import salmon.trackers
from salmon.checks.blacklist import red_blacklist_reason
from salmon.checks.integrity import check_integrity
from salmon.checks.mqa import check_mqa
from salmon.checks.source import detect_source
from salmon.checks.upconverts import check_upconvert
from salmon.common.files import get_audio_files
from salmon.tagger.pre_data import construct_artists_li, parse_title
from salmon.tagger.tags import gather_tags
from salmon.uploader.dupe_checker import generate_dupe_check_searchstrs, get_search_results

# "block" cannot be overridden in the UI; "warn" needs an explicit tick.
OK, WARN, BLOCK, SKIP = "ok", "warn", "block", "skip"


def run_log_check(path: str) -> dict:
    logs = []
    for root, _dirs, files in os.walk(path):
        for f in sorted(files):
            if f.lower().endswith(".log"):
                logpath = os.path.join(root, f)
                try:
                    output = cambia.parse_log_file(logpath)
                    score = int(output.evaluation_combined[0].combined_score)
                    integrity = output.parsed.parsed_logs[0].checksum.integrity
                    logs.append(
                        {
                            "file": os.path.relpath(logpath, path),
                            "score": score,
                            "checksum_integrity": str(integrity).rsplit(".", 1)[-1].rstrip(">"),
                        }
                    )
                except Exception as e:
                    logs.append({"file": os.path.relpath(logpath, path), "error": str(e)})
    return {"logs": logs}


async def run_mqa_check(path: str) -> dict:
    results = []
    detected = False
    for f in get_audio_files(path, True):
        found = await check_mqa(os.path.join(path, f))
        detected = detected or found
        results.append({"file": f, "detected": found})
    return {"detected": detected, "files": results}


async def run_upconvert_check(path: str) -> dict:
    results = []
    for f in get_audio_files(path, True):
        if not f.lower().endswith(".flac"):
            continue
        try:
            result = await check_upconvert(os.path.join(path, f))
            results.append(msgspec.to_builtins(result) | {"file": f})
        except Exception as e:
            results.append({"file": f, "error": str(e)})
    return {"files": results}


def _row(row_id: str, label: str, verdict: str, detail: str) -> dict:
    return {"id": row_id, "label": label, "verdict": verdict, "detail": detail}


def source_row(guess: dict, chosen: str | None) -> dict:
    """Verdict on the media source, comparing what was detected to what was picked."""
    detected, confidence = guess["source"], guess["confidence"]
    why = " ".join(guess["reasons"])
    if not chosen:
        if confidence == "unknown":
            return _row("source", "Source", BLOCK, f"Could not be determined. {why}")
        return _row(
            "source", "Source", BLOCK, f"Looks like {detected} ({confidence}). {why} Pick a source to continue."
        )
    if detected and detected != chosen:
        return _row("source", "Source", WARN, f"You picked {chosen}, but the files look like {detected}. {why}")
    if confidence == "unknown":
        return _row("source", "Source", WARN, f"{chosen} cannot be verified from the files. {why}")
    return _row("source", "Source", OK, f"{chosen} — {why}")


def integrity_row(result: dict) -> dict:
    if result["passed"]:
        return _row("integrity", "File integrity", OK, "Every file decodes cleanly.")
    return _row("integrity", "File integrity", BLOCK, result["details"] or "One or more files failed to decode.")


def mqa_row(result: dict) -> dict:
    if not result["detected"]:
        return _row("mqa", "MQA", OK, "No MQA encoding detected.")
    hits = [f["file"] for f in result["files"] if f["detected"]]
    return _row("mqa", "MQA", BLOCK, f"MQA detected in {len(hits)} file(s): {', '.join(hits[:3])}.")


def upconvert_row(result: dict) -> dict:
    files = result["files"]
    if not files:
        return _row("upconvert", "Upconvert", SKIP, "No FLAC files to test.")
    upconverted = [f["file"] for f in files if f.get("is_upconverted")]
    if upconverted:
        return _row(
            "upconvert", "Upconvert", BLOCK, f"Upconverted from a lower bit depth: {', '.join(upconverted[:3])}."
        )
    errored = [f["file"] for f in files if f.get("error")]
    if errored:
        return _row("upconvert", "Upconvert", WARN, f"Could not test {len(errored)} file(s).")
    return _row("upconvert", "Upconvert", OK, f"Genuine bit depth across {len(files)} file(s).")


def log_row(result: dict, source: str | None) -> dict:
    """Rip-log verdict. Only CD rips are expected to carry one."""
    logs = result["logs"]
    if source and source != "CD":
        return _row("log", "Rip log", SKIP, f"Not applicable to a {source} release.")
    if not logs:
        return _row(
            "log", "Rip log", WARN, "No rip log, so the rip cannot be verified. Allowed, but it scores 0 on RED."
        )
    broken = [x for x in logs if "error" in x]
    scored = [x for x in logs if "score" in x]
    if not scored:
        return _row("log", "Rip log", WARN, f"Could not parse {len(broken)} log(s).")
    worst = min(scored, key=lambda x: x["score"])
    mismatched = [x for x in scored if x["checksum_integrity"] != "Match"]
    if mismatched:
        return _row("log", "Rip log", WARN, f"Log checksum does not match the audio ({mismatched[0]['file']}).")
    if worst["score"] < 100:
        return _row("log", "Rip log", WARN, f"Score {worst['score']}/100 ({worst['file']}).")
    return _row("log", "Rip log", OK, f"Score 100/100 across {len(scored)} log(s).")


def dupe_row(tracker: str, results: list[dict]) -> dict:
    row_id = f"dupe:{tracker}"
    label = f"Duplicate ({tracker})"
    if not results:
        return _row(row_id, label, OK, "No existing release found.")
    names = [str(r.get("groupName") or r.get("groupId")) for r in results[:2]]
    return _row(
        row_id, label, WARN, f"{len(results)} possible match(es): {', '.join(names)}. Confirm it is not a duplicate."
    )


def blacklist_row(tracker: str, reason: str | None) -> dict:
    row_id, label = f"blacklist:{tracker}", f"{tracker} blacklist"
    if reason:
        return _row(row_id, label, BLOCK, reason)
    return _row(row_id, label, OK, "Not on the Do-Not-Upload list.")


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


async def _tracker_rows(tracker: str, identity: dict) -> list[dict]:
    rows = []
    searchstrs = generate_dupe_check_searchstrs(identity["artists"], identity["title"], identity["catno"])
    try:
        results = await get_search_results(salmon.trackers.get_class(tracker)(), searchstrs) if searchstrs else []
        rows.append(dupe_row(tracker, results))
    except Exception as e:
        rows.append(_row(f"dupe:{tracker}", f"Duplicate ({tracker})", WARN, f"Could not search {tracker}: {e}"))
    if tracker == "RED":
        rows.append(
            blacklist_row(tracker, red_blacklist_reason(identity["artists"], identity["title"], identity["label"]))
        )
    return rows


async def run_preflight(
    path: str,
    source: str | None = None,
    trackers: list[str] | None = None,
    skips: dict[str, bool] | None = None,
) -> dict:
    """Run every pre-upload check and return the verdict rows plus the raw results."""
    skips = skips or {}
    guess = await asyncio.to_thread(detect_source, path)
    effective_source = source or (guess["source"] if guess["confidence"] == "confirmed" else None)
    rows = [source_row(guess, source)]
    raw: dict[str, dict] = {"source": guess}

    if skips.get("integrity"):
        rows.append(_row("integrity", "File integrity", SKIP, "Skipped by request."))
    else:
        passed, details = await check_integrity(path)
        raw["integrity"] = {"passed": passed, "details": click.unstyle(details)}
        rows.append(integrity_row(raw["integrity"]))

    if skips.get("upconvert"):
        rows.append(_row("upconvert", "Upconvert", SKIP, "Skipped by request."))
    else:
        raw["upconvert"] = await run_upconvert_check(path)
        rows.append(upconvert_row(raw["upconvert"]))

    if skips.get("mqa"):
        rows.append(_row("mqa", "MQA", SKIP, "Skipped by request."))
    else:
        raw["mqa"] = await run_mqa_check(path)
        rows.append(mqa_row(raw["mqa"]))

    if skips.get("log"):
        rows.append(_row("log", "Rip log", SKIP, "Skipped by request."))
    else:
        raw["log"] = await asyncio.to_thread(run_log_check, path)
        rows.append(log_row(raw["log"], effective_source))

    identity = await asyncio.to_thread(_release_identity, path)
    if identity.get("title"):
        for tracker in trackers or []:
            rows.extend(await _tracker_rows(tracker, identity))
    elif trackers:
        rows.append(_row("dupe", "Duplicate", WARN, "Tags could not be read, so no duplicate search could be run."))

    return {
        "rows": rows,
        "raw": raw,
        "identity": {k: v for k, v in identity.items() if k != "artists"},
        "blocking": [r["id"] for r in rows if r["verdict"] == BLOCK],
        "warnings": [r["id"] for r in rows if r["verdict"] == WARN],
    }
