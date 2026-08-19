import os
import re
import shutil
import tempfile
from contextlib import suppress
from itertools import chain
from string import Formatter
from typing import Any

import asyncclick as click
import msgspec

from salmon import cfg
from salmon.constants import (
    ARROWS,
    BLACKLISTED_CHARS,
    BLACKLISTED_FULLWIDTH_REPLACEMENTS,
)
from salmon.errors import UploadError
from salmon.tagger.tagfile import TagFile


class Change(msgspec.Struct, frozen=True):
    """Data structure for tag changes."""

    tag: str
    old: Any
    new: Any


def tag_files(path, tags, metadata, auto_rename):
    """
    Wrapper function that calls the functions that create and print the
    proposed changes, and then prompts for confirmation to retag the file.
    """
    click.secho("\nRetagging files...", fg="cyan", bold=True)
    if not check_whether_to_tag(tags, metadata):
        return
    album_changes = collect_album_data(metadata)
    track_changes = create_track_changes(tags, metadata)
    print_changes(album_changes, track_changes, next(iter(tags.values())))
    if auto_rename or click.confirm(
        click.style("\nWould you like to auto-tag the files with the updated metadata?", fg="magenta"),
        default=True,
    ):
        retag_files(path, album_changes, track_changes)


def check_whether_to_tag(tags, metadata):
    """
    Make sure the number of tracks in the metadata equals the number of tracks
    in the folder.
    """
    if len(tags) != sum([len(disc) for disc in metadata["tracks"].values()]):
        click.secho(
            "Number of tracks differed from number of tracks in metadata, skipping retagging procedure...",
            fg="red",
        )
        return False
    return True


def collect_album_data(metadata):
    """Create a dictionary of the proposed album tags (consistent across every track)."""
    if cfg.upload.formatting.add_edition_title_to_album_tag and metadata["edition_title"]:
        title = f"{metadata['title']} ({metadata['edition_title']})"
    else:
        title = metadata["title"]
    return {
        k: v
        for k, v in {
            "album": title,
            "genre": "; ".join(sorted(metadata["genres"])),
            "date": metadata["group_year"],
            "label": metadata["label"],
            "catno": metadata["catno"],
            "albumartist": _generate_album_artist(metadata["artists"]),
            "upc": metadata["upc"],
            "comment": metadata["comment"] if cfg.upload.description.review_as_comment_tag else None,
        }.items()
        if v
    }


def _generate_album_artist(artists):
    main_artists = [a for a, i in artists if i == "main"]
    if len(main_artists) >= cfg.upload.formatting.various_artist_threshold:
        return cfg.upload.formatting.various_artist_word
    c = ", " if len(main_artists) > 2 or "&" in "".join(main_artists) else " & "
    return c.join(sorted(main_artists))


def create_track_changes(tags, metadata):
    """
    Compare the track data in the metadata to the track data in the tags
    and record all differences.
    """
    changes = {}
    tracks = metadata_to_track_list(metadata["tracks"])

    sorted_tags = sorted(
        tags.items(),
        key=lambda item: (
            _get_tag_number(item[1], "discnumber"),
            _get_tag_number(item[1], "tracknumber"),
        ),
    )

    if len(sorted_tags) != len(tracks):
        raise UploadError(
            f"Track count mismatch: {len(sorted_tags)} audio files but {len(tracks)} metadata tracks. "
            "Fix the folder or the metadata before uploading."
        )

    for (filename, tagset), trackmeta in zip(sorted_tags, tracks, strict=False):
        changes[filename] = []

        try:
            old_artist_str = ", ".join(tagset.artist)
        except TypeError:
            old_artist_str = "None"

        new_artist_str = create_artist_str(trackmeta["artists"])
        if old_artist_str != new_artist_str:
            changes[filename].append(Change("artist", old_artist_str, new_artist_str))

        old_composer = getattr(tagset, "composer", None) or "None"
        new_composer = create_composer_str(trackmeta["artists"])
        if new_composer and old_composer != new_composer:
            changes[filename].append(Change("composer", old_composer, new_composer))

        old_conductor = getattr(tagset, "conductor", None) or "None"
        new_conductor = create_conductor_str(trackmeta["artists"])
        if new_conductor and old_conductor != new_conductor:
            changes[filename].append(Change("conductor", old_conductor, new_conductor))

        if cfg.upload.formatting.guests_in_track_title:
            trackmeta["title"] = append_guests_to_track_titles(trackmeta)

        if cfg.upload.description.empty_track_comment_tag and getattr(tagset, "comment", False):
            changes[filename].append(Change("comment", tagset.comment, ""))

        for tagfield, metafield in [
            ("title", "title"),
            ("isrc", "isrc"),
            ("tracknumber", "track#"),
            ("discnumber", "disc#"),
            ("tracktotal", "tracktotal"),
            ("disctotal", "disctotal"),
        ]:
            change = _compare_tag(tagfield, metafield, tagset, trackmeta)
            if change:
                changes[filename].append(change)
    return changes


def append_guests_to_track_titles(track):
    guest_artists = [a for a, i in track["artists"] if i == "guest"]
    if (
        "feat" not in track["title"]
        and guest_artists
        and len(guest_artists) <= cfg.upload.formatting.various_artist_threshold
    ):
        c = ", " if len(guest_artists) > 2 or "&" in "".join(guest_artists) else " & "
        # If we find a remix parenthetical, remove it and re-add it after the guest artists.
        remix = re.search(r"( \([^\)]+Remix(?:er)?\))", track["title"], flags=re.IGNORECASE)
        if remix:
            track["title"] = track["title"].replace(remix[1], "")
        track["title"] += f" (feat. {c.join(sorted(guest_artists))})"
        if remix:
            track["title"] += remix[1]
    return track["title"]


def _remap_spectral_ids(spectral_ids, to_rename):
    """Remap spectral ids from old filenames to new, once, from a snapshot.

    Applying the original->new map (not each rename sequentially) keeps a chained
    rename (a->b, b->c) from dragging track a's spectral onto track c.
    """
    rename_map = dict(to_rename)
    for key, value in list(spectral_ids.items()):
        if value in rename_map:
            spectral_ids[key] = rename_map[value]


def _disc_track_sort_key(value):
    s = str(value)
    return (0, int(s)) if s.isdigit() else (1, s.lower())


def metadata_to_track_list(metadata):
    """Flatten the {disc: {track: meta}} dict into a list in disc/track order.

    Sorted to match the (disc, track) ordering of the tag side in
    create_track_changes, so the two zip together onto the right files.
    """
    ordered = []
    for disc_key in sorted(metadata, key=_disc_track_sort_key):
        disc = metadata[disc_key]
        for track_key in sorted(disc, key=_disc_track_sort_key):
            ordered.append(disc[track_key])
    return ordered


def _compare_tag(tagfield, metafield, tagset, trackmeta):
    """
    Compare a tag to the equivalent metadata field. If the metadata field
    does not equal the existing tag, return a ``Change``.
    """
    # .get: optional fields (e.g. isrc) may be absent from hand-edited metadata
    if trackmeta.get(metafield):
        if not getattr(tagset, tagfield, False):
            return Change(tagfield, None, trackmeta[metafield])
        if str(getattr(tagset, tagfield, "")) != str(trackmeta[metafield]):
            return Change(tagfield, getattr(tagset, tagfield, "None"), trackmeta[metafield])
    return None


def create_artist_str(artists):
    """Create the artist string from the metadata.

    For classical-friendly tagging, conductor roles are included in the ARTIST
    tag after the main performer list, while composer roles are excluded and
    written to their own COMPOSER tag.
    """
    main_artists = _ordered_unique(a for a, i in artists if i == "main")
    conductors = _ordered_unique(a for a, i in artists if i == "conductor")
    lead_artists = _ordered_unique([*main_artists, *conductors])

    if conductors:
        artist_str = ", ".join(lead_artists)
    else:
        c = ", " if len(lead_artists) > 2 and "&" not in "".join(lead_artists) else " & "
        artist_str = c.join(lead_artists)

    if not cfg.upload.formatting.guests_in_track_title:
        guest_artists = _ordered_unique(a for a, i in artists if i == "guest")
        if len(guest_artists) >= cfg.upload.formatting.various_artist_threshold:
            artist_str += f" (feat. {cfg.upload.formatting.various_artist_word})"
        elif guest_artists:
            c = ", " if len(guest_artists) > 2 and "&" not in "".join(guest_artists) else " & "
            artist_str += f" (feat. {c.join(guest_artists)})"

    return artist_str


def create_composer_str(artists):
    """Create the composer string from the metadata."""
    composers = _ordered_unique(a for a, i in artists if i == "composer")
    return ", ".join(composers)


def create_conductor_str(artists):
    """Create the conductor string from the metadata."""
    conductors = _ordered_unique(a for a, i in artists if i == "conductor")
    return ", ".join(conductors)


def _ordered_unique(values):
    """Preserve the first-seen order while removing duplicates."""
    return list(dict.fromkeys(values))


def print_changes(album_changes, track_changes, a_track):
    """Print all the proposed track changes, then all the album data."""
    if any(t for t in track_changes.values()):
        click.secho("\nProposed tag changes:", fg="yellow", bold=True)
    for filename, changes in track_changes.items():
        if changes:
            click.secho(f"> {filename}", fg="yellow")
            for change in changes:
                click.echo(f"  {change.tag.ljust(20)} ••• {change.old} {ARROWS} {change.new}")

    click.secho("\nAlbum tags (applied to all):", fg="yellow", bold=True)
    for field, value in album_changes.items():
        previous = getattr(a_track, field, "None")
        if isinstance(previous, list):
            previous = "; ".join(previous)
        is_different = str(previous) != str(value)
        if not is_different:
            click.secho(f"> {field.ljust(13)} ••• {previous}")
        else:
            click.echo(
                f"> {click.style(str(field.ljust(13)), bold=True)} ••• {str(previous)} "
                f"{ARROWS} {click.style(str(value), bold=True)}"
            )


def retag_files(path, album_changes, track_changes):
    """Apply the proposed metadata changes to the files."""
    for filename, changes in track_changes.items():
        mut = TagFile(os.path.join(path, filename))
        for change in changes:
            setattr(mut, change.tag, str(change.new))
        for tag, value in album_changes.items():
            setattr(mut, tag, str(value))
        mut.save()
    click.secho("Retagged files.", fg="green")


def rename_files(path, tags, metadata, auto_rename, spectral_ids, source=None):
    """
    Call functions that generate the proposed changes, then print and prompt
    for confirmation. Apply the changes if user agrees.
    """
    to_rename = []
    folders_to_create = set()
    directory_disc_map = {}
    multi_disc = len(metadata["tracks"]) > 1
    md_word = {"CD": "CD", "Vinyl": "LP"}.get(source or "", "Part")
    # "Part" is default if not CD or Vinyl
    split_multi_disc_into_folders = cfg.upload.formatting.split_multi_disc_into_folders

    track_list = list(chain.from_iterable([d.values() for d in metadata["tracks"].values()]))
    multiple_artists = any(
        {a for a, i in t["artists"] if i == "main"} != {a for a, i in track_list[0]["artists"] if i == "main"}
        for t in track_list[1:]
    )

    # Zero-pad width = digits needed for the largest track/disc number in this
    # release, floored at 2 (so "9" -> "09"), growing only if there are 100+.
    track_digits = max(2, len(str(max(_get_tag_number(t, "tracknumber") for t in tags.values()))))
    disc_digits = 1
    if multi_disc:
        disc_digits = len(str(max(_get_tag_number(t, "discnumber") for t in tags.values())))

    for filename, tracktags in tags.items():
        ext = os.path.splitext(filename)[1].lower()
        new_name = generate_file_name(
            tracktags, ext, multiple_artists, track_digits=track_digits, disc_digits=disc_digits
        )
        disc_number = 1
        if multi_disc:
            disc_number = _get_tag_number(tracktags, "discnumber")
            if split_multi_disc_into_folders:
                new_name = os.path.join(f"{md_word}{disc_number:0{disc_digits}d}", new_name)
            else:
                track_number = _get_tag_number(tracktags, "tracknumber")
                new_name = generate_file_name(
                    tracktags,
                    ext,
                    multiple_artists,
                    trackno_or=f"{disc_number:0{disc_digits}d}.{track_number:0{track_digits}d}",
                    track_digits=track_digits,
                    disc_digits=disc_digits,
                )
                old_dir = os.path.dirname(os.path.join(path, filename))
                if old_dir != path:
                    directory_disc_map[old_dir] = disc_number
        if filename != new_name:
            to_rename.append((filename, new_name))
            if multi_disc and split_multi_disc_into_folders:
                folders_to_create.add(os.path.join(path, f"{md_word}{disc_number:0{disc_digits}d}"))

    if to_rename:
        print_filenames(to_rename)
        if auto_rename or click.confirm(
            click.style("\nWould you like to rename the files?", fg="magenta"),
            default=True,
        ):
            for folder in folders_to_create:
                if not os.path.isdir(folder):
                    os.mkdir(folder)
            # os.rename silently overwrites on POSIX; refuse if two files map to one name,
            # or if a target lands on any existing file (tracked or not) that isn't itself
            # a rename source vacating in phase 1.
            renamed_sources = {old for old, _ in to_rename}
            unchanged = set(tags) - renamed_sources
            targets = [n for _, n in to_rename]

            def _target_blocked(name: str) -> bool:
                if name in unchanged:
                    return True
                full = os.path.join(path, name)
                if not os.path.exists(full):
                    return False
                for old in renamed_sources:
                    # samefile: a case-variant of a source (case-insensitive fs) is the
                    # source itself and vacates in phase 1 — not a collision.
                    with suppress(OSError):
                        if os.path.samefile(full, os.path.join(path, old)):
                            return False
                return True

            collisions = sorted({n for n in targets if targets.count(n) > 1 or _target_blocked(n)})
            if collisions:
                raise UploadError(f"Rename would overwrite existing files: {', '.join(collisions)}")

            directory_move_pairs = set()
            # Two-phase via a reserved staging dir: a swap/chain (a->b, b->a) passes the
            # collision check, and a direct os.rename would clobber a source before it moved.
            staging_dir = tempfile.mkdtemp(dir=path, prefix=".salmon-rename-")
            staged: list[tuple[str, str, str]] = []  # (temp, original, final)
            completed: list[tuple[str, str]] = []  # (final, temp)
            try:
                for index, (filename, new_name) in enumerate(to_rename):
                    old_dir = os.path.dirname(os.path.join(path, filename))
                    new_dir = os.path.dirname(os.path.join(path, new_name))
                    if old_dir != path:
                        # lowercase: move_non_audio_files compares against file.lower()
                        directory_move_pairs.add((os.path.splitext(filename)[1].lower(), old_dir, new_dir))
                    temp_path = os.path.join(staging_dir, str(index))
                    os.replace(os.path.join(path, filename), temp_path)
                    staged.append((temp_path, os.path.join(path, filename), os.path.join(path, new_name)))
                for temp_path, _original, final_path in staged:
                    # os.replace for Windows parity: a samefile-exempt target entry may
                    # still exist, where os.rename would raise instead of overwriting.
                    os.replace(temp_path, final_path)
                    completed.append((final_path, temp_path))
            except BaseException:
                # Roll back so a failure (or Ctrl-C) never strands files under temp
                # names or a half-renamed layout. Finals go back to their own empty
                # staging slots first — collision-free for any swap/chain order —
                # then every temp returns to its original name (all slots vacated).
                for final_path, temp_path in completed:
                    with suppress(OSError):
                        os.replace(final_path, temp_path)
                for temp_path, original_path, _final in staged:
                    if os.path.exists(temp_path):
                        with suppress(OSError):
                            os.replace(temp_path, original_path)
                raise
            finally:
                # rmdir not rmtree: if a rollback step failed, staged audio must survive.
                with suppress(OSError):
                    os.rmdir(staging_dir)

            if spectral_ids:
                _remap_spectral_ids(spectral_ids, to_rename)

            move_non_audio_files(directory_move_pairs, directory_disc_map)
            delete_empty_folders(path)
    else:
        click.secho("\nNo file renaming is recommended.", fg="green")

def print_filenames(to_rename):
    """Print all the proposed filename changes."""
    click.secho("\nProposed filename changes:", fg="yellow", bold=True)
    for filename, new_name in to_rename:
        click.echo(f"   {filename} {ARROWS} {new_name}")

def generate_file_name(tags, ext, multiple_artists, trackno_or=None, track_digits=2, disc_digits=2):
    """Generate the template keys and format the template with the tags."""
    template = cfg.upload.formatting.file_template
    keys = [fn for _, fn, _, _ in Formatter().parse(template) if fn]
    if (
        "artist" in keys
        and cfg.upload.formatting.no_artist_in_filename_if_only_one_album_artist
        and not multiple_artists
    ):
        keys.remove("artist")
        template = cfg.upload.formatting.one_album_artist_file_template

    def _width_for(key):
        if key == "tracknumber":
            return track_digits
        if key == "discnumber":
            return disc_digits
        return 2

    if isinstance(tags, dict):
        template_keys: dict[str, str | int] = {}
        for k in keys:
            tag_val = tags.get(k)
            if isinstance(tag_val, list) and tag_val:
                tag_val = tag_val[0]
            template_keys[k] = _parse_integer(
                tag_val if isinstance(tag_val, (str, int)) else "", _width_for(k)
            )
    else:
        template_keys = {}
        for k in keys:
            raw_val = getattr(tags, k, "")
            if k == "artist" and isinstance(raw_val, list) and raw_val:
                raw_val = raw_val[0]
            val = _parse_integer(
                raw_val if isinstance(raw_val, (str, int)) else str(raw_val),
                _width_for(k),
            )
            template_keys[k] = val

    if "artist" in keys:
        if isinstance(tags, dict):
            artist_count = str(tags["artist"]).count(",") + str(tags["artist"]).count("&")
        else:
            artist_count = str(tags.artist).count(",") + str(tags.artist).count("&")
        if artist_count > cfg.upload.formatting.various_artist_threshold:
            template_keys["artist"] = cfg.upload.formatting.various_artist_word
    if "tracknumber" in keys and trackno_or is not None:
        template_keys["tracknumber"] = trackno_or
    new_base = template.format(**template_keys) + ext
    if cfg.upload.description.fullwidth_replacements:
        for char, sub in BLACKLISTED_FULLWIDTH_REPLACEMENTS.items():
            new_base = new_base.replace(char, sub)
    return re.sub(BLACKLISTED_CHARS, cfg.upload.formatting.blacklisted_substitution, new_base)

def _parse_integer(value, width=2):
    if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
        return f"{int(value):0{width}d}"
    return value

def _get_tag_number(tracktags, field):
    if isinstance(tracktags, dict):
        value = tracktags.get(field)
        if isinstance(value, list) and value:
            value = value[0]
    else:
        value = getattr(tracktags, field, None)

    if value is None:
        return 1
    if isinstance(value, list) and value:
        value = value[0]
    if isinstance(value, str):
        value = value.split("/")[0]
        return int(value) if value.isdigit() else 1
    if isinstance(value, int):
        return value
    return 1


def move_non_audio_files(directory_move_pairs, directory_disc_map=None):
    """
    Move every non-music file (log, cue, m3u, cover, etc.) out of each
    per-disc source folder and into its destination folder.

    When multiple disc folders (CD1/CD2, 1/2, etc.) are being merged into
    the same destination, same-named files (e.g. a "log" or "cover.jpg" in
    each disc folder) would otherwise collide and overwrite one another. In
    that case each file is suffixed with its disc number, e.g. "log.1.log",
    "log.2.log", "cover.1.jpg", "cover.2.jpg".
    """
    directory_disc_map = directory_disc_map or {}
    source_dirs = {old_dir for _, old_dir, _ in directory_move_pairs}
    merging_multiple_folders = len(source_dirs) > 1

    for ext, old_dir, new_dir in directory_move_pairs:
        disc_number = directory_disc_map.get(old_dir)
        for file in os.listdir(old_dir):
            file_path = os.path.join(old_dir, file)
            if file.lower().endswith(ext) or os.path.isdir(file_path):
                continue
            dest_name = file
            if merging_multiple_folders and disc_number is not None:
                base, file_ext = os.path.splitext(file)
                dest_name = f"{base}.{disc_number}{file_ext}"
            dest_path = os.path.join(new_dir, dest_name)
            if os.path.abspath(dest_path) == os.path.abspath(file_path):
                continue  # already in place (old_dir == new_dir)
            base, file_ext = os.path.splitext(dest_name)
            counter = 1
            while os.path.exists(dest_path):  # shutil.move overwrites; suffix instead
                dest_path = os.path.join(new_dir, f"{base}.{counter}{file_ext}")
                counter += 1
            shutil.move(file_path, dest_path)


def delete_empty_folders(path):
    for root, dirs, files in os.walk(path):
        if not dirs and not files:
            os.rmdir(root)
