import os
import re
import shutil
import sys
from copy import copy
from string import Formatter

import asyncclick as click

from salmon import cfg
from salmon.common import strip_template_keys
from salmon.common.files import rewrite_refusal
from salmon.common.strings import plain_spaces
from salmon.constants import (
    BLACKLISTED_CHARS,
    BLACKLISTED_FULLWIDTH_REPLACEMENTS,
)
from salmon.converter.conversions import carry_conversion
from salmon.errors import UploadError
from salmon.tagger.audio_info import gather_audio_info


def rename_folder(path, metadata, auto_rename, check=True, parent=None):
    """
    Create a revised folder name from the new metadata and present it to the
    user. Have them decide whether or not to accept the folder name.
    Then offer them the ability to edit the folder name in a text editor
    before the renaming occurs.
    For scene releases, the name of the original folder is kept untouched, and
    the folder is copied to the download folder.
    `parent` replaces the download folder as the directory the renamed folder goes into.
    """
    old_base = os.path.basename(path)
    template_fields = {name for _, name, _, _ in Formatter().parse(cfg.upload.formatting.folder_template) if name}
    if "resolution" in template_fields:
        metadata = {**metadata, "resolution": _resolution(path)}
    new_base = generate_folder_name(metadata)
    if metadata["scene"]:
        new_base = old_base
        auto_rename = True

    if check and old_base != new_base:
        click.secho("\nRenaming folder...", fg="cyan", bold=True)
        click.echo(f"Old folder name        : {old_base}")
        click.echo(f"New pending folder name: {new_base}")

        user_rename_choice = cfg.upload.yes_all or click.confirm(
            click.style("\nWould you like to replace the original folder name?", fg="magenta"), default=True
        )

        new_base = _edit_folder_interactive(new_base, auto_rename) if auto_rename or user_rename_choice else old_base

    # A name with a path separator or . / .. would escape the download dir and, via the
    # rmtree below, delete something outside it — reject before building new_path.
    if os.sep in new_base or (os.altsep and os.altsep in new_base) or new_base in {"", ".", ".."}:
        raise UploadError(f"Invalid folder name: {new_base!r}")

    new_path = os.path.join(parent or cfg.directory.download_directory, new_base)
    same_location = os.path.isdir(new_path) and os.path.samefile(path, new_path)
    # Checked whether or not new_path exists yet: a symlinked folder on the way can lead into a library.
    if not same_location and cfg.directory.protects(new_path):
        raise UploadError(f"Not renaming into {new_path}: it is in library_dirs, or holds one.")
    if os.path.isdir(new_path) and not same_location:
        # Often the very album this copy was made from: replacing it would break what seeds from it.
        if (reason := rewrite_refusal(new_path)) is not None:
            raise UploadError(f"Not replacing {new_path}: {reason}. Rename it, or give the upload another folder name.")
        if not check or click.confirm(
            click.style(
                f"A folder already exists with the new folder name '{new_path}', would you like to replace it?",
                fg="magenta",
                bold=True,
            ),
            default=True,
        ):
            shutil.rmtree(new_path)
        else:
            raise UploadError("New folder name already exists.")
    new_path_dirname = os.path.dirname(new_path)
    if not os.path.exists(new_path_dirname):
        os.makedirs(new_path_dirname)

    # Imported here: the uploader package imports this module.
    from salmon.uploader.spectrals import get_spectrals_path, made_by_salmon, spectrals_dir

    # Check if hardlinks can be used
    same_volume = os.stat(path).st_dev == os.stat(cfg.directory.download_directory).st_dev
    # A hardlink shares the inode, so a later tag write on the new folder would reach a library album.
    in_library = cfg.directory.protects(path)
    use_hardlinks = same_volume and cfg.directory.hardlinks and not in_library

    # Spectrals salmon made before this rename move with the album whatever remove_source_dir says; a Spectrals
    # folder it did not make (a seeding source's own) is copied like any other, and a library album keeps its own.
    specs_path = get_spectrals_path(path)
    specs_in_source = (
        not in_library
        and _is_direct_child(specs_path, path)
        and os.path.isdir(specs_path)
        and made_by_salmon(specs_path)
    )
    ignore = _ignoring_top_level(path, os.path.basename(specs_path)) if specs_in_source else None

    if os.path.exists(path) and os.path.exists(new_path) and os.path.samefile(path, new_path):
        click.secho(f"Skipping copy, same location already for '{new_path}'", fg="yellow")
    else:
        if use_hardlinks:
            try:
                shutil.copytree(path, new_path, copy_function=os.link, dirs_exist_ok=True, ignore=ignore)
                click.secho(f"Hardlinked folder to '{new_path}'.", fg="yellow")
            except shutil.Error as _:
                click.secho("Hardlinking didn't work, falling back to non-hardlink copy...", fg="red")
                # A partially hardlinked tree makes the plain copy raise SameFileError (#356)
                shutil.rmtree(new_path, ignore_errors=True)
                shutil.copytree(path, new_path, dirs_exist_ok=True, ignore=ignore)
                click.secho(f"Copied folder to '{new_path}'.", fg="yellow")
        else:
            shutil.copytree(path, new_path, dirs_exist_ok=True, ignore=ignore)
            click.secho(f"Copied folder to '{new_path}'.", fg="yellow")

        if specs_in_source:
            # Moved, not copied: the source must not keep a Spectrals folder the upload never deletes.
            _move_specs_folder(specs_path, get_spectrals_path(new_path))

        if cfg.upload.formatting.remove_source_dir and in_library:
            click.secho(f"Not removing {path}: it is in library_dirs, or holds one.", fg="yellow")
        elif cfg.upload.formatting.remove_source_dir:
            shutil.rmtree(path)
    carry_conversion(path, new_path)

    # Also rename the spectrals folder in tmp_dir, or a dry run's run directory, if there is one.
    if (beside := spectrals_dir()) is not None:
        tmp_old_specs_path = os.path.join(beside, f"spectrals_{old_base}")
        tmp_new_specs_path = os.path.join(beside, f"spectrals_{new_base}")

        if not os.path.exists(tmp_old_specs_path):
            pass  # No spectrals folder exists, nothing to rename
        elif os.path.exists(tmp_new_specs_path) and os.path.samefile(tmp_old_specs_path, tmp_new_specs_path):
            click.secho(f"Skipping move, same location already for '{tmp_new_specs_path}'", fg="yellow")
        else:
            _move_specs_folder(tmp_old_specs_path, tmp_new_specs_path)
            click.secho(f"Moved temporary spectrals folder to '{tmp_new_specs_path}'.", fg="yellow")

    return new_path


def _is_direct_child(child: str, parent: str) -> bool:
    """Whether `child` is an entry directly inside `parent`."""
    return os.path.dirname(os.path.abspath(child)) == os.path.abspath(parent)


def _ignoring_top_level(top: str, name: str):
    """A copytree `ignore` that skips the entry `name` of `top` only, not one of that name further down."""
    top = os.path.abspath(top)

    def ignore(directory, names):
        return {name} & set(names) if os.path.abspath(directory) == top else set()

    return ignore


def _move_specs_folder(src: str, dst: str) -> None:
    """Move a spectrals folder to `dst`, replacing a stale one there, and carry salmon's claim on it."""
    # Imported here: the uploader package imports this module.
    from salmon.uploader.spectrals import carry_specs_claim

    old_real = os.path.realpath(src)
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.move(src, dst)
    carry_specs_claim(old_real, dst)


def _resolution(path):
    """Bit depth and sample rate of the folder's tracks, like "24-96"; blank for lossy, 16/44.1, zero or mixed."""
    return resolution_token(gather_audio_info(path))


def resolution_token(audio_info):
    """_resolution from a gather_audio_info mapping, so the converters know a name's exact token."""
    bits = {info["precision"] for info in audio_info.values()}
    rates = {info["sample rate"] for info in audio_info.values()}
    if len(bits) != 1 or len(rates) != 1:
        return ""
    (bit_depth,), (sample_rate,) = bits, rates
    if not bit_depth or not sample_rate or (bit_depth == 16 and sample_rate == 44100):
        return ""
    return f"{bit_depth}-{sample_rate / 1000:g}"


def _token_span(foldername, token):
    """Where the name's last standalone token is, or None: "1924-48" in a title holds "24-48" but not as a token."""
    matches = list(re.finditer(r"(?<![\w.-])" + re.escape(token) + r"(?![\w.-])", foldername)) if token else []
    return matches[-1].span() if matches else None


def holds_resolution_token(foldername, token) -> bool:
    """Whether the folder name carries token as its own word."""
    return _token_span(foldername, token) is not None


def swap_resolution_token(foldername, token, new):
    """Replace the name's standalone resolution token with new; a name without one is left as it is."""
    span = _token_span(foldername, token)
    return foldername if span is None else foldername[: span[0]] + new + foldername[span[1] :]


def drop_resolution_token(foldername, token):
    """Remove the name's standalone resolution token, one space beside it, and a bracket pair it leaves empty."""
    span = _token_span(foldername, token)
    if span is None:
        return foldername
    start, end = span
    if start > 0 and foldername[start - 1] == " ":
        start -= 1
    elif end < len(foldername) and foldername[end] == " ":
        end += 1
    name = foldername[:start] + foldername[end:]
    if 0 < start < len(name) and name[start - 1] in "[({" and name[start] in "])}":
        name = name[: start - 1].rstrip(" ") + name[start + 1 :]
    return name.strip()


def generate_folder_name(metadata):
    """
    Fill in the values from the folder template using the metadata, then strip
    away the unnecessary keys.
    """
    metadata = {**metadata, **{"artists": _compile_artist_str(metadata["artists"])}}
    template = cfg.upload.formatting.folder_template
    keys = [fn for _, fn, _, _ in Formatter().parse(template) if fn]
    for k in keys.copy():
        if not metadata.get(k):
            template = _strip_blank_resolution(template) if k == "resolution" else strip_template_keys(template, k)
            keys.remove(k)
    sub_metadata = _fix_format(metadata, keys)
    return template.format(**{k: _sub_illegal_characters(sub_metadata[k]) for k in keys})


def _strip_blank_resolution(template):
    """Drop a blank {resolution} placeholder (format spec too) and its bracket only if that empties it."""
    template = re.sub(r"\s*\{resolution(?::[^}]*)?\}", "", template)
    template = re.sub(r"[\[{(]\s*[\]})]", "", template)
    template = re.sub(r"\s+", " ", template).strip()
    return re.sub(r" *- *$", "", template)


def _compile_artist_str(artist_data):
    """Create a string to represent the main artists of the release."""
    artists = [a[0] for a in artist_data if a[1] == "main"]
    if len(artists) > cfg.upload.formatting.various_artist_threshold:
        return cfg.upload.formatting.various_artist_word
    c = ", " if len(artists) > 2 or "&" in "".join(artists) else " & "
    return c.join(sorted(artists))


def _sub_illegal_characters(stri):
    stri = plain_spaces(str(stri))
    if cfg.upload.description.fullwidth_replacements:
        for char, sub in BLACKLISTED_FULLWIDTH_REPLACEMENTS.items():
            stri = str(stri).replace(char, sub)
    return re.sub(BLACKLISTED_CHARS, cfg.upload.formatting.blacklisted_substitution, str(stri))


def _fix_format(metadata, keys):
    """
    Add abbreviated encoding to format key when the format is not 'FLAC'.
    Helpful for 24 bit FLAC and MP3 320/V0 stuff.

    So far only 24 bit FLAC is supported, when I fix the script for MP3 i will add MP3 encodings.
    """
    sub_metadata = copy(metadata)
    if "format" in keys:
        if metadata["format"] == "FLAC" and metadata["encoding"] == "24bit Lossless":
            sub_metadata["format"] = "24bit FLAC"
        elif metadata["format"] == "MP3":
            enc = re.sub(r" \(VBR\)", "", str(metadata["encoding"]))
            sub_metadata["format"] = f"MP3 {enc}"
            if metadata["encoding_vbr"]:
                sub_metadata["format"] += " (VBR)"
        elif metadata["format"] == "AAC":
            enc = re.sub(r" \(VBR\)", "", metadata["encoding"])
            sub_metadata["format"] = f"AAC {enc}"
            if metadata["encoding_vbr"]:
                sub_metadata["format"] += " (VBR)"
    return sub_metadata


def _edit_folder_interactive(foldername, auto_rename):
    """Allow the user to edit the pending folder name in a text editor."""
    if auto_rename:
        return foldername
    if not click.confirm(
        click.style("Is the new folder name acceptable? ([n] to edit)", fg="magenta"),
        default=True,
    ):
        newname = click.edit(foldername, editor=cfg.upload.default_editor)
        while True:
            if newname is None:
                return foldername
            elif re.search(BLACKLISTED_CHARS, newname):
                if not click.confirm(
                    click.style(
                        "Folder name contains invalid characters, retry?",
                        fg="magenta",
                        bold=True,
                    ),
                    default=True,
                ):
                    sys.exit(1)
            else:
                return newname.strip().replace("\n", "")
            newname = click.edit(foldername, editor=cfg.upload.default_editor)
    return foldername
