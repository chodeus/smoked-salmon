import json
import os
import shutil

import anyio
import asyncclick as click
from mutagen import File as MutagenFile

from salmon import cfg
from salmon.common import get_audio_files
from salmon.tagger.tagfile import TagFile

STANDARDIZED_TAGS = {
    "date": ["year"],
    "label": ["recordlabel", "organization", "publisher"],
    "catalognumber": ["labelno", "catalog #", "catno"],
    "tracktotal": ["totaltracks", "total tracks"],
    "disctotal": ["totaldiscs", "total discs"],
}

CLASSICAL_GENRES = {
    "classical",
    "baroque",
    "chambermusic",
    "choral",
    "modernclassical",
    "orchestral",
    "opera",
}


async def check_tags(path: str) -> dict[str, TagFile]:
    """Get and then check the tags for problems. Offer user way to edit tags.

    Args:
        path: Path to the directory containing audio files.

    Returns:
        Dictionary mapping filenames to their TagFile objects.

    Raises:
        IndexError: If no tracks are found.
    """
    click.secho("\nChecking tags...", fg="yellow", bold=True)
    tags = gather_tags(path)
    if not tags:
        raise IndexError("No tracks were found.")

    check_required_tags(tags)

    if cfg.upload.prompt_puddletag:
        print_a_tag(next(iter(tags.values())))
        if await prompt_editor(path):
            tags = gather_tags(path)

    return tags


def gather_tags(path):
    """Get the tags of each file."""
    tags = {}
    for filename in get_audio_files(path, sort_by_tracknumber=True):
        tags[filename] = TagFile(os.path.join(path, filename))
    return tags


def check_required_tags(tags):
    """Verify that every track has the required tag fields."""
    offending_files = []
    for fln, tag_item in tags.items():
        missing = []
        for t in ["title", "artist", "album", "tracknumber"]:
            if not getattr(tag_item, t, False):
                missing.append(t)
        if _requires_classical_composer(tag_item) and not getattr(tag_item, "composer", False):
            missing.append("composer")
        if missing:
            offending_files.append(f"{fln} ({', '.join(missing)})")

    if offending_files:
        click.secho(
            "The following files do not contain all the required tags: {}.".format(", ".join(offending_files)),
            fg="red",
        )
    else:
        click.secho("Verified that all files contain the required tags.", fg="green")


def _requires_classical_composer(tag_item) -> bool:
    genres = getattr(tag_item, "genre", None)
    if not genres:
        return False
    if isinstance(genres, str):
        genres = [genres]
    return any(str(genre).strip().lower().replace(" ", "") in CLASSICAL_GENRES for genre in genres)


def print_a_tag(tags):
    """Print all tags in a tag set."""
    for key, value in tags.items():
        click.echo(f"> {key}: {value}")


# The fields worth hand-editing; the rest are derived or written by the tagger.
EDITABLE_TAG_FIELDS = (
    "title",
    "artist",
    "album",
    "albumartist",
    "date",
    "tracknumber",
    "discnumber",
    "genre",
    "label",
    "catno",
    "isrc",
    "comment",
    "composer",
    "conductor",
)


async def prompt_editor(path: str) -> bool:
    """Ask whether the tags are acceptable, and open an editor if they are not.

    Args:
        path: Path to the directory containing audio files.

    Returns:
        True if the tags may have changed and should be re-read.
    """
    if click.confirm(
        click.style("\nAre the above tags acceptable? ([n] to open in tag editor)", fg="magenta"),
        default=True,
    ):
        return False
    return await open_tag_editor(path)


async def open_tag_editor(path: str) -> bool:
    """Open puddletag if it is installed, otherwise edit the tags as JSON.

    puddletag needs a desktop session, so it is unusable over the web interface
    and in a container. click.edit is bridged to the browser, so the JSON route
    works everywhere.

    Returns:
        True if the tags may have changed.
    """
    if shutil.which("puddletag"):
        result = await anyio.run_process(["puddletag", path], check=False)
        if result.returncode == 0:
            return True
        click.secho(f"puddletag exited with {result.returncode}; falling back to the text editor.", fg="yellow")
    return edit_tags_as_json(path)


def _tags_to_dict(tags: dict[str, TagFile]) -> dict[str, dict]:
    return {
        filename: {field: getattr(tag, field, None) for field in EDITABLE_TAG_FIELDS} for filename, tag in tags.items()
    }


def _reject_bad_document(after: object, before: dict[str, dict]) -> str | None:
    """Describe why an edited tag document is unusable, or None if it is fine."""
    if not isinstance(after, dict):
        return "The edited tags must be a JSON object keyed by filename"
    unknown = set(after) - set(before)
    if unknown:
        return f"Unknown file(s) in the edited tags: {', '.join(sorted(unknown))}"
    for filename, fields in after.items():
        if not isinstance(fields, dict):
            return f"{filename} must map to a JSON object, not {type(fields).__name__}"
        for key, value in fields.items():
            if value is None or isinstance(value, (str, int, float)):
                continue
            if isinstance(value, list) and all(isinstance(v, str) for v in value):
                continue
            return f"{filename}.{key} must be text, a number, a list of text, or null"
    return None


def edit_tags_as_json(path: str) -> bool:
    """Edit every track's tags as one JSON document and write back what changed.

    Returns:
        True if any tag was written.
    """
    tags = gather_tags(path)
    if not tags:
        click.secho("No audio files to edit.", fg="red")
        return False

    before = _tags_to_dict(tags)
    edited = click.edit(
        json.dumps(before, indent=2, ensure_ascii=False), extension=".json", editor=cfg.upload.default_editor
    )
    if edited is None:
        click.secho("No changes made.", fg="yellow")
        return False

    try:
        after = json.loads(edited)
    except ValueError as e:
        click.secho(f"That is not valid JSON ({e}); no tags were changed.", fg="red")
        return False

    problem = _reject_bad_document(after, before)
    if problem:
        click.secho(f"{problem}; no tags were changed.", fg="red")
        return False

    # Work out every change before writing any of them, so a document that is
    # only wrong halfway down cannot leave the album half-applied.
    planned = {}
    for filename, fields in after.items():
        changed = {k: v for k, v in fields.items() if k in EDITABLE_TAG_FIELDS and v != before[filename].get(k)}
        if changed:
            planned[filename] = changed

    written = 0
    for filename, changed in planned.items():
        tag = tags[filename]
        for key, value in changed.items():
            setattr(tag, key, value)
        tag.save()
        written += 1

    if not written:
        click.secho("No changes made.", fg="yellow")
        return False
    click.secho(f"Updated tags on {written} file(s).", fg="green")
    return True


def standardize_tags(path: str) -> None:
    """Change ambiguously defined tags field values into standardized fields.

    This function renames tag fields to use consistent naming conventions.
    For example, 'year' becomes 'date', 'recordlabel' becomes 'label', etc.

    Args:
        path: Path to the directory containing audio files.
    """
    for filename in get_audio_files(path):
        mut = MutagenFile(os.path.join(path, filename))
        if mut is None:
            continue
        tags = mut.tags
        if tags is None:
            continue
        found_aliased: set[str] = set()
        for tag, aliases in STANDARDIZED_TAGS.items():
            for alias in aliases:
                if alias in tags:
                    # Mutagen tags support dynamic key access for Vorbis comments
                    tags[tag] = tags[alias]
                    del tags[alias]
                    found_aliased.add(alias)
        if found_aliased:
            mut.save()
            click.secho(f"Unaliased the following tags for {filename}: " + ", ".join(found_aliased))
