import os

import asyncclick as click

from salmon import cfg, dryrun, interaction
from salmon.images import upload_cover
from salmon.tagger.cover import download_cover_if_nonexistent


async def _new_group_cover_url(
    site_code: str, stored_cover_urls: dict[str, str], path: str, cover_source: str | None, remove_downloaded: bool
) -> str | None:
    """Cover URL for a new group on this tracker, uploading it unless its host already has it."""
    cover_host = cfg.image.host_for(site_code, "cover_uploader")
    if cover_host not in stored_cover_urls:
        cover_path, is_downloaded = await download_cover_if_nonexistent(path, cover_source)
        if dryrun.active() and cover_path:
            dryrun.say(f"not uploading the cover {os.path.basename(cover_path)} to {cover_host}.")
            uploaded = dryrun.image_url(cover_path, cover_host)
        else:
            uploaded = await upload_cover(cover_path, site_code)
        if uploaded:
            stored_cover_urls[cover_host] = uploaded
        if is_downloaded and remove_downloaded and cover_path:
            click.secho("Removing downloaded Cover Image File", fg="yellow")
            os.remove(cover_path)
    return stored_cover_urls.get(cover_host)


async def resolve_cover_url(
    site_code: str,
    group_id: int | None,
    stored_cover_urls: dict[str, str],
    path: str,
    cover_source: str | None,
    remove_downloaded: bool,
) -> tuple[bool, str | None]:
    """Whether to upload to this tracker, and the cover URL; asks before a new group goes up without one."""
    if group_id:
        if not remove_downloaded:
            await download_cover_if_nonexistent(path, cover_source)
        return True, None

    cover_url = await _new_group_cover_url(site_code, stored_cover_urls, path, cover_source, remove_downloaded)
    if cover_url:
        return True, cover_url

    host = cfg.image.host_for(site_code, "cover_uploader")
    click.secho(
        f"\nNo cover image for this new group on {site_code}: none was found, its download failed, or the upload to "
        f"{host} failed.",
        fg="yellow",
        bold=True,
    )
    if await interaction.assume_defaults():
        click.secho("Not uploading a new group without a cover image with -yyy (upload.yes_all).", fg="red", bold=True)
        return False, None

    while True:
        raw_choice = await interaction.prompt(
            click.style("Continue upload without a cover image? [y/N/reload]", fg="magenta"),
            default="n",
            show_default=False,
        )
        choice = raw_choice.strip().lower()
        if choice in ("y", "yes"):
            return True, None
        if choice not in ("r", "reload"):
            return False, None

        click.secho("Checking for cover image again...", fg="cyan")
        cover_url = await _new_group_cover_url(site_code, stored_cover_urls, path, cover_source, remove_downloaded)
        if cover_url:
            click.secho("Cover image found and uploaded.", fg="green")
            return True, cover_url
        click.secho("Still no cover image found. Add a cover.jpg to the release folder and try again.", fg="yellow")
