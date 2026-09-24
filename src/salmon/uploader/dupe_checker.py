import asyncio
import re
from difflib import SequenceMatcher
from typing import TYPE_CHECKING
from urllib import parse

import asyncclick as click

from salmon import cfg
from salmon.common import RE_FEAT, make_searchstrs
from salmon.common.strings import comparable
from salmon.errors import AbortAndDeleteFolder, RequestError
from salmon.uploader.upload import generate_catno

if TYPE_CHECKING:
    from salmon.trackers.base import BaseGazelleApi


async def dupe_check_recent_torrents(gazelle_site: "BaseGazelleApi", searchstrs: list[str]) -> list[tuple]:
    """Check site log for recent uploads similar to ours.

    Args:
        gazelle_site: The tracker API instance.
        searchstrs: Search strings to match against.

    Returns:
        List of matching upload tuples (id, artist, title).
    """
    recent_uploads = await gazelle_site.get_uploads_from_log()
    # Each upload in this list is best guess at (id,artist,title) from log
    hits = []
    seen = []
    for upload in recent_uploads:
        # We don't care about different torrents from the same release.
        torrent_str = upload[1] + upload[2]
        if torrent_str in seen:
            continue
        seen.append(torrent_str)
        artist = upload[1]
        title = upload[2]
        artist = [[artist, "main"]]
        possible_comparisons = generate_dupe_check_searchstrs(artist, title)
        ratio = 0
        for searchstr in searchstrs:
            for comparison_string in possible_comparisons:
                new_ratio = SequenceMatcher(None, searchstr, comparison_string).ratio()
                ratio = max(ratio, new_ratio)
        # Default tolerance is 0.5
        if ratio > cfg.upload.log_dupe_tolerance:
            hits.append(upload)
    return hits


def print_recent_upload_results(gazelle_site: "BaseGazelleApi", recent_uploads: list[tuple], searchstr: str) -> None:
    """Prints any recent uploads.
    Currently hard limited to 5.
    Realistically we are probably only interested in 1.
    These results can't be used for group selection because the log doesn't give us a group id"""
    if recent_uploads:
        click.secho(
            f"\nFound similar recent uploads in the {gazelle_site.site_string} log: ",
            fg="red",
            nl=False,
        )
        click.secho(f" (searchstrs: {searchstr})", bold=True)
        for u in recent_uploads[:5]:
            click.secho(
                f"{u[1]} - {u[2]} | {gazelle_site.base_url}/torrents.php?torrentid={u[0]}",
                fg="cyan",
            )


async def _prompt_for_recent_upload_results(
    gazelle_site: "BaseGazelleApi",
    recent_uploads: list[tuple],
    searchstr: str,
    offer_deletion: bool,
) -> int | None:
    """Print recent uploads and prompt user to choose a group ID.

    Args:
        gazelle_site: The tracker API instance.
        recent_uploads: List of recent upload tuples.
        searchstr: Search string used.
        offer_deletion: Whether to offer folder deletion option.

    Returns:
        Group ID or None for new group.
    """
    shown = recent_uploads[:5]
    # First, print the recent uploads if any
    if shown:
        click.secho(
            f"\nFound similar recent uploads in the {gazelle_site.site_string} log: ",
            fg="red",
            nl=False,
        )
        click.secho(f" (searchstrs: {searchstr})", bold=True)
        for u_index, u in enumerate(shown):
            click.echo(f" {u_index + 1:02d} >> ", nl=False)  # torrent_id
            click.secho(f"{u[1]} - {u[2]} ", fg="cyan", nl=False)  # artist - title
            click.echo(f"| {gazelle_site.base_url}/torrents.php?torrentid={u[0]}")

    # Now prompt for user action
    while True:
        pick = "Type an upload's number from the list above (1 is the first), or p" if recent_uploads else "P"
        prompt_text = (
            "\nWould you like to upload to an existing group?\n"
            f"{pick}aste a group URL, or [N]ew group / [a]bort {'/ [d]elete music folder ' if offer_deletion else ''}"
        )

        group_id = await click.prompt(
            click.style(prompt_text, fg="magenta"),
            default="",
        )

        # Handle numeric input (selecting from recent uploads or direct group ID)
        if group_id.strip().isdigit():
            group_id_num = int(group_id)

            if group_id_num == 0:
                if not recent_uploads:
                    continue
                group_id_num = 1  # If the user types 0 give them the first choice.

            # Only the uploads listed above can be picked by number.
            if shown and 1 <= group_id_num <= len(shown):
                torrent_id = shown[group_id_num - 1][0]
                # Need to convert torrent ID to group ID
                try:
                    result_group_id = await gazelle_site.get_redirect_torrentgroupid(torrent_id)
                    if result_group_id is not None:
                        return result_group_id
                    click.echo("Could not get group ID from torrent ID.")
                    continue
                except Exception:
                    click.echo("Could not get group ID from torrent ID.")
                    continue
            else:
                # Direct group ID input
                click.echo(f"Interpreting {group_id_num} as a group ID")
                return group_id_num

        # Handle URL input
        elif group_id.strip().lower().startswith(gazelle_site.base_url + "/torrents.php"):
            parsed_query = parse.parse_qs(parse.urlparse(group_id).query)
            if "id" in parsed_query:
                group_id = parsed_query["id"][0]
                return int(group_id)
            elif "torrentid" in parsed_query:
                torrent_id = parsed_query["torrentid"][0]
                result_group_id = await gazelle_site.get_redirect_torrentgroupid(torrent_id)
                if result_group_id is not None:
                    return result_group_id
                click.echo("Could not get group ID from torrent ID.")
                continue
            else:
                click.echo("Could not find group ID in URL.")
                continue

        # Handle action commands
        elif group_id.lower().startswith("a"):
            raise click.Abort
        elif group_id.lower().startswith("d") and offer_deletion:
            raise AbortAndDeleteFolder
        elif group_id.lower().startswith("n") or not group_id.strip():
            click.echo("Uploading to a new torrent group.")
            return None


def suggest_group(results: list[dict] | None, release: dict | None) -> str:
    """Pre-typed dupe answer: the listed result whose artist, title and year all match, else a new group."""
    if not results or not release:
        return "N"
    wanted_artists = {comparable(name) for name, _importance in release.get("artists") or []} - {""}
    title = comparable(release.get("title"))
    if not title:
        return "N"
    year = str(release.get("year") or release.get("group_year") or "")
    for index, result in enumerate(results, 1):
        if result.get("groupId") is None:
            continue
        if (
            comparable(result.get("groupName")) == title
            and str(result.get("groupYear") or "") == year
            and comparable(result.get("artist")) in wanted_artists
        ):
            return str(index)
    return "N"


async def check_existing_group(
    gazelle_site: "BaseGazelleApi",
    searchstrs: list[str],
    offer_deletion: bool = True,
    release: dict | None = None,
) -> int | None:
    """Check for existing group and prompt user for selection.

    Args:
        gazelle_site: The tracker API instance.
        searchstrs: Search strings for dupe checking.
        offer_deletion: Whether to offer folder deletion option.
        release: Release data (artists, title, year) used to pre-type a matching result.

    Returns:
        Group ID or None for new group.
    """
    results = await get_search_results(gazelle_site, searchstrs)
    if not results and cfg.upload.requests.check_recent_uploads:
        recent_uploads = await dupe_check_recent_torrents(gazelle_site, searchstrs)
        group_id = await _prompt_for_recent_upload_results(
            gazelle_site, recent_uploads, " / ".join(searchstrs), offer_deletion
        )
    else:
        print_search_results(gazelle_site, results, " / ".join(searchstrs))
        group_id = await _prompt_for_group_id(
            gazelle_site, results, offer_deletion, default=suggest_group(results, release)
        )
    if group_id is not None:
        confirmation = await _confirm_group_id(gazelle_site, group_id, results, release)
        if confirmation is True:
            return group_id
        return None
    return group_id


async def get_search_results(gazelle_site: "BaseGazelleApi", searchstrs: list[str]) -> list[dict]:
    """Search for existing releases on tracker.

    Args:
        gazelle_site: The tracker API instance.
        searchstrs: Search strings to query.

    Returns:
        List of matching release dicts.
    """
    results: list[dict] = []
    tasks = [gazelle_site.api_call("browse", {"searchstr": searchstr}) for searchstr in searchstrs]
    for releases in await asyncio.gather(*tasks):
        for release in releases["results"]:
            if release not in results:
                results.append(release)
    return results


def generate_dupe_check_searchstrs(artists, album, catno=None):
    searchstrs = []
    album = _sanitize_album_for_dupe_check(album)
    searchstrs += make_searchstrs(artists, album, normalize=True)
    if album is not None and re.search(r"vol[^u]", album.lower()):
        extra_alb_search = re.sub(r"vol[^ ]+", "volume", album, flags=re.IGNORECASE)
        searchstrs += make_searchstrs(artists, extra_alb_search, normalize=True)
    if album is not None and "untitled" in album.lower():  # Filthy catno untitled rlses
        searchstrs += make_searchstrs(artists, catno or "", normalize=True)
    if album is not None and "/" in album:  # Filthy singles
        searchstrs += make_searchstrs(artists, album.split("/")[0], normalize=True)
    elif catno and album is not None and catno.lower() in album.lower():
        searchstrs += make_searchstrs(artists, "untitled", normalize=True)
    return filter_unnecessary_searchstrs(searchstrs)


def _sanitize_album_for_dupe_check(album):
    if not album:  # Handle None or empty string
        return ""
    album = RE_FEAT.sub("", album)
    album = re.sub(
        r"[\(\[][^\)\]]*\b(Edition|Version|Deluxe|Original|Reissue|Remaster|Vol|Mix|Edit)"
        r"[^\)\]]*[\)\]]",
        "",
        album,
        flags=re.IGNORECASE,
    )
    album = re.sub(r"[\(\[][^\)\]]*Remixes[^\)\]]*[\)\]]", "remixes", album, flags=re.IGNORECASE)
    album = re.sub(r"[\(\[][^\)\]]*Remix[^\)\]]*[\)\]]", "remix", album, flags=re.IGNORECASE)
    return album


def filter_unnecessary_searchstrs(searchstrs):
    past_strs = []
    new_strs = []
    for stri in sorted(searchstrs, key=len):
        word_set = set(stri.split())
        for prev_word_set in past_strs:
            if all(p in word_set for p in prev_word_set):
                break
        else:
            new_strs.append(stri)
            past_strs.append(word_set)
    return new_strs


def print_search_results(gazelle_site: "BaseGazelleApi", results: list[dict], searchstr: str) -> None:
    """Print all the site search results."""
    if not results:
        click.secho(
            f"\nNo groups found on {gazelle_site.site_string} matching this release.",
            fg="green",
            nl=False,
        )
    else:
        click.secho(
            f"\nResults matching this release were found on {gazelle_site.site_string}: ",
            fg="red",
            nl=False,
        )
        click.secho(f" (searchstrs: {searchstr})", bold=True)
        for r_index, r in enumerate(results):
            # Read every field first so a malformed result is skipped whole, not printed
            # as a partial row before a missing field raises.
            try:
                group_id, artist, name = r["groupId"], r["artist"], r["groupName"]
                year, release_type, tags = r["groupYear"], r["releaseType"], r["tags"]
                tags_text = ", ".join(tags)  # inside the try: None/non-str tags skip the whole row
            except (KeyError, TypeError):
                continue
            url = f"{gazelle_site.base_url}/torrents.php?id={group_id}"
            click.echo(f" {r_index + 1:02d} >> {group_id} | ", nl=False)  # 1-based; user can't pick 0
            click.secho(f"{artist} - {name} ", fg="cyan", nl=False)
            click.secho(f"({year}) [{release_type}] ", fg="yellow", nl=False)
            click.echo(f"[Tags: {tags_text}] | {url}")


async def _prompt_for_group_id(
    gazelle_site: "BaseGazelleApi",
    results: list[dict],
    offer_deletion: bool,
    default: str = "N",
) -> int | None:
    """Prompt user to choose a group ID.

    Args:
        gazelle_site: The tracker API instance.
        results: Search results to choose from.
        offer_deletion: Whether to offer folder deletion option.
        default: Pre-typed answer, a result number or "N".

    Returns:
        Group ID or None for new group.
    """
    pick = "Type a group's number from the list above (1 is the first), or p" if results else "P"
    delete = "/ [d]elete music folder " if offer_deletion else ""
    while True:
        group_id = await click.prompt(
            click.style(
                "\nWould you like to upload to an existing group?\n"
                f"{pick}aste a group URL, or [N]ew group / [a]bort {delete}",
                fg="magenta",
            ),
            default=default,
        )
        if group_id.strip().isdigit():
            raw_input = int(group_id)
            if raw_input == 0 and not results:
                continue
            list_index = max(0, raw_input - 1)  # 1-based → 0-based, clamp to 0
            if list_index < len(results):
                return int(results[list_index]["groupId"])
            else:
                click.echo(f"Interpreting {raw_input} as a group Id")
                return raw_input

        elif group_id.strip().lower().startswith(gazelle_site.base_url + "/torrents.php"):
            parsed_query = parse.parse_qs(parse.urlparse(group_id).query)
            if "id" in parsed_query:
                return int(parsed_query["id"][0])
            elif "torrentid" in parsed_query:
                torrent_id = parsed_query["torrentid"][0]
                result_group_id = await gazelle_site.get_redirect_torrentgroupid(torrent_id)
                if result_group_id is not None:
                    return result_group_id
                continue
            else:
                click.echo("Could not find group ID in URL.")
                continue
        elif group_id.lower().startswith("a"):
            raise click.Abort
        elif group_id.lower().startswith("d") and offer_deletion:
            raise AbortAndDeleteFolder
        elif group_id.lower().startswith("n") or not group_id.strip():
            click.echo("Uploading to a new torrent group.")
            return None


async def print_torrents(
    gazelle_site: "BaseGazelleApi",
    group_id: int,
    rset: dict | None = None,
    highlight_torrent_id: int | None = None,
) -> dict:
    """Print the torrents in a group, highlighting one, and return the group data that was printed."""
    # If rset is not provided, fetch it from the API
    if rset is None:
        try:
            fetched_rset = await gazelle_site.torrentgroup(group_id)
            # account for differences between search result and group result json
            fetched_rset["groupName"] = fetched_rset["group"]["name"]
            fetched_rset["artist"] = ""
            for a in fetched_rset["group"]["musicInfo"]["artists"]:
                fetched_rset["artist"] += a["name"] + " "
            fetched_rset["groupId"] = fetched_rset["group"]["id"]
            fetched_rset["groupYear"] = fetched_rset["group"]["year"]
            rset = fetched_rset
        except RequestError:
            click.secho(f"{group_id} does not exist.", fg="red")
            raise click.Abort from None

    # At this point rset is guaranteed to be non-None
    assert rset is not None

    click.secho(f"\nSelected ID: {rset['groupId']} ", nl=False)
    click.secho(f"| {rset['artist']} - {rset['groupName']} ", fg="cyan", nl=False)
    click.secho(f"({rset['groupYear']})", fg="yellow")
    click.secho("Torrents in this group:", fg="yellow", bold=True)
    for t in rset["torrents"]:
        color = "yellow" if highlight_torrent_id and t.get("id") == highlight_torrent_id else None
        click.secho(f"> {describe_torrent(t, rset)}", fg=color)
    return rset


def describe_torrent(torrent: dict, rset: dict) -> str:
    """One line for a group's torrent: edition, label, catalogue number, media, format and encoding."""
    is_remaster = _is_remaster(torrent)
    group_label = ((rset.get("group") or {}).get("recordLabel") or "").strip()
    label = ((torrent.get("remasterRecordLabel") or "").strip() if is_remaster else "") or group_label
    catno = _edition_catno(torrent, rset)

    prefix_parts = []
    if is_remaster:
        if torrent.get("remasterYear"):
            prefix_parts.append(str(torrent["remasterYear"]))
        title = (torrent.get("remasterTitle") or "").strip()
        if title:
            prefix_parts.append(title)
    else:
        prefix_parts.append("OR")

    if label:
        prefix_parts.append(label)
    if catno:
        prefix_parts.append(catno)

    prefix = " / ".join(prefix_parts)
    if prefix:
        prefix += " / "
    return f"{prefix}{torrent['media']} / {torrent['format']} / {torrent['encoding']}"


def _is_remaster(torrent: dict) -> bool:
    """Robust across RED/OPS: `remastered` is not always sent, so any edition field counts."""
    return bool(torrent.get("remastered")) or any(
        (
            torrent.get("remasterYear"),
            (torrent.get("remasterTitle") or "").strip(),
            (torrent.get("remasterRecordLabel") or "").strip(),
            (torrent.get("remasterCatalogueNumber") or "").strip(),
        )
    )


def _edition_catno(torrent: dict, rset: dict) -> str:
    """Catalogue number of the torrent's edition; only an original release falls back to the group's."""
    if _is_remaster(torrent):
        return (torrent.get("remasterCatalogueNumber") or "").strip()
    return ((rset.get("group") or {}).get("catalogueNumber") or "").strip()


def matching_torrents(rset: dict, release: dict | None) -> list[dict]:
    """Group torrents in the release's edition with its media, format and encoding: uploading it again is a dupe."""
    if not release:
        return []
    wanted = (release.get("source"), release.get("format"), release.get("encoding"))
    if not all(wanted):
        return []
    year = str(release.get("year") or "")
    catno = comparable(generate_catno(release))
    edition_title = comparable(release.get("edition_title"))
    # A torrentgroup response has the year under "group"; a search result has groupYear.
    group_year = rset.get("groupYear") or (rset.get("group") or {}).get("year")
    matches = []
    for torrent in rset.get("torrents") or []:
        if (torrent.get("media"), torrent.get("format"), torrent.get("encoding")) != wanted:
            continue
        edition_year = str(torrent.get("remasterYear") or group_year or "")
        if year and edition_year and edition_year != year:
            continue
        # Another catalogue number or edition title is another release; one missing on either side still counts.
        held_catno = comparable(_edition_catno(torrent, rset))
        if catno and held_catno and held_catno != catno:
            continue
        held_title = comparable(torrent.get("remasterTitle"))
        if edition_title and held_title and held_title != edition_title:
            continue
        matches.append(torrent)
    return matches


async def _confirm_group_id(
    gazelle_site: "BaseGazelleApi", group_id: int, results: list[dict], release: dict | None = None
) -> bool:
    """Confirm the upload; abort is pre-typed when this edition already holds the same media, format and encoding."""
    rset = None
    for r in results:
        if group_id == r["groupId"]:
            rset = r
            break

    rset = await print_torrents(gazelle_site, group_id, rset)
    dupes = matching_torrents(rset, release)
    if dupes:
        held = dupes[0]
        click.secho(
            f"\nDUPE RISK: this edition already has {held['media']} / {held['format']} / {held['encoding']}; "
            "the site removes exact duplicates.",
            fg="red",
            bold=True,
        )
    while True:
        resp = (
            await click.prompt(
                click.style(
                    "\nAre you sure you would you like to upload this torrent to this group? [Y]es, "
                    "[n]ew group, [a]bort, [d]elete music folder",
                    fg="magenta",
                ),
                default="a" if dupes else "Y",
            )
        )[0].lower()
        if resp == "a":
            raise click.Abort
        elif resp == "d":
            raise AbortAndDeleteFolder
        elif resp == "y":
            return True
        elif resp == "n":
            return False


async def choose_source_flac(group: dict, release: dict) -> dict | None:
    """The FLAC in this release's edition that transcodes of it are made from; None to stop."""
    group_id = (group.get("group") or {}).get("id")
    flacs = matching_torrents(group, release)
    wanted = f"{release.get('source')} / FLAC / {release.get('encoding')}"
    if not flacs:
        click.secho(
            f"\nGroup {group_id} has no {wanted} in this release's edition (year, catalogue number, edition title) "
            "to transcode from.",
            fg="red",
            bold=True,
        )
        return None
    if len(flacs) == 1:
        return flacs[0]

    click.secho(f"\nGroup {group_id} has several {wanted} torrents in this edition:", fg="yellow", bold=True)
    for index, torrent in enumerate(flacs, 1):
        click.echo(f" {index:02d} >> {describe_torrent(torrent, group)}")
    if cfg.upload.yes_all:
        click.secho("Not picking the FLAC to transcode from with --yes-all. Run without it to choose.", fg="red")
        return None
    while True:
        choice = (
            (
                await click.prompt(
                    click.style(
                        f"\nWhich one are these transcodes made from? [1-{len(flacs)}] or [a]bort", fg="magenta"
                    ),
                    default="",
                )
            )
            .strip()
            .lower()
        )
        if choice.startswith("a"):
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(flacs):
            return flacs[int(choice) - 1]
        click.secho(f"Enter a number from 1 to {len(flacs)}, or a to abort.", fg="red")
