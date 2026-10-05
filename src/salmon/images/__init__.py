import contextlib
from collections.abc import Callable, Sequence

import anyio
import asyncclick as click
import pyperclip

from salmon import cfg, dryrun
from salmon.common import AliasedCommands, commandgroup, is_http_url
from salmon.config.validations import SPECTRALS_REFUSED, host_refusal
from salmon.errors import ImageUploadFailed
from salmon.images import catbox, imgbb, imgbox, oeimg, ptscreens, ra, red
from salmon.images.base import BaseImageUploader

HOSTS = {
    "catbox": catbox,
    "ptscreens": ptscreens,
    "oeimg": oeimg,
    "imgbb": imgbb,
    "imgbox": imgbox,
    "ra": ra,
    "red": red,
}

# How many uploads to one image host a batch runs at once, each on a connection of its own,
# reused from one image to the next. Chosen by measurement against a local fake host.
UPLOAD_CONNECTIONS = 8


class ImageHostRefused(ValueError):
    """An image host may not be used for a tracker's images; the message says why."""


def validate_image_host(ctx: click.Context, param: click.Parameter, value: str | None) -> str | None:
    """Validate an image host name, passing "no host given" through."""
    if value is not None and value not in HOSTS:
        raise click.BadParameter(f"{value} is not a valid image host")
    return value


def image_host_for_tracker(tracker: str, explicit_host: str | None = None) -> str:
    """The host for images on `tracker`'s pages: explicit_host if allowed there, else the tracker's image_uploader.

    Raises:
        ImageHostRefused: If explicit_host may not be used for that tracker's images, with the reason.
    """
    if explicit_host is None:
        return cfg.image.resolve(tracker, "image_uploader")
    # A host picked by hand is held to the cover rule, the most a tracker's own pages allow.
    if (reason := host_refusal(tracker.lower(), "cover_uploader", explicit_host)) is not None:
        raise ImageHostRefused(f"{explicit_host} can't be used for {tracker.upper()}'s images: {reason}")
    return explicit_host


def validate_tracker(ctx: click.Context, param: click.Parameter, value: str | None) -> str | None:
    """Validate a tracker given by code, in any case, against the configured trackers."""
    if value is None:
        return None
    from salmon import trackers  # Read at run time, as the configured trackers are.

    if value.upper() not in trackers.tracker_list:
        configured = ", ".join(trackers.tracker_list) or "none"
        raise click.BadParameter(f"{value} is not a tracker in your config (configured: {configured})")
    return value.upper()


@commandgroup.group(cls=AliasedCommands)
async def images() -> None:
    """Create and manage uploads to image hosts."""
    pass


@images.command()
@click.argument(
    "filepaths",
    type=click.Path(exists=True, dir_okay=False, resolve_path=True),
    nargs=-1,
)
@click.option(
    "--image-host",
    "-i",
    help=(
        "The image host to upload to. With --tracker, defaults to that tracker's image_uploader; "
        "otherwise [image] image_uploader"
    ),
    default=None,
    callback=validate_image_host,
)
@click.option(
    "--tracker",
    "-t",
    help="The tracker the images are for: its image_uploader is the default host, and the host must be allowed there",
    default=None,
    callback=validate_tracker,
)
async def up(filepaths: tuple[str, ...], image_host: str | None, tracker: str | None) -> None:
    """Upload images to an image host."""
    if tracker is not None:
        try:
            image_host = image_host_for_tracker(tracker, image_host)
        except ImageHostRefused as error:
            raise click.BadParameter(str(error), param_hint="'--image-host'") from None
    await upload_images(filepaths, HOSTS[image_host or cfg.image.image_uploader])


async def upload_images(filepaths: Sequence[str], image_host) -> list[str]:
    """Upload images to the specified host, over at most UPLOAD_CONNECTIONS connections.

    Args:
        filepaths: File paths to upload.
        image_host: The image host module.

    Returns:
        List of uploaded URLs.
    """
    failures: list[Exception] = []
    try:
        results = await _upload_groups(
            image_host.ImageUploader(),
            [[filepath] for filepath in filepaths],
            on_failure=lambda _index, error: failures.append(error),
        )
        if failures:
            raise failures[0]
        urls = [group[0] for group in results if group is not None]
        for url in urls:
            if not is_http_url(url):
                raise ImageUploadFailed(f"{image_host.__name__} returned no usable URL: {str(url)[:200]!r}")
            click.secho(url)
        if cfg.upload.description.copy_uploaded_url_to_clipboard:
            # Clipboard is unavailable on headless servers; never fail the upload over it.
            with contextlib.suppress(Exception):
                pyperclip.copy("\n".join(urls))
        return urls
    except (ImageUploadFailed, ValueError) as error:
        click.secho(f"Image Upload Failed. {error}", fg="red")
        raise ImageUploadFailed("Failed to upload image") from error


async def _upload_groups(
    uploader: BaseImageUploader,
    groups: Sequence[Sequence[str]],
    on_start: Callable[[int], None] = lambda _index: None,
    on_failure: Callable[[int, ImageUploadFailed], None] = lambda _index, _error: None,
) -> list[list[str] | None]:
    """Upload groups of images to one host, over at most UPLOAD_CONNECTIONS connections.

    Images queue for a free connection, in order, and a slow or failed upload holds up none
    of the others. Once an upload fails, no new group starts: the host may be down, so the
    groups not started yet are left for the caller to send elsewhere. The images of a group
    already started still go.

    Args:
        uploader: The image uploader to send every image through.
        groups: The image paths to upload, in groups that succeed or fail together.
        on_start: Called with a group's index when its first image starts uploading.
        on_failure: Called with a group's index and the error when one of its images fails.

    Returns:
        Each group's URLs, in the order of its paths, or None if the group failed or never started.

    Raises:
        DryRunRefused: In a dry run, before any upload starts.
    """
    # Here as well as in upload_file: refused in several workers at once, it would come out as a group.
    if dryrun.active():
        dryrun.refuse(f"upload {sum(len(paths) for paths in groups)} image(s) to {uploader.host}")
    queue = iter([(index, position, path) for index, paths in enumerate(groups) for position, path in enumerate(paths)])
    urls: list[list[str]] = [[""] * len(paths) for paths in groups]
    started: set[int] = set()
    failed: set[int] = set()

    async def worker() -> None:
        # Every worker takes from the same iterator, so each image is taken exactly once.
        for index, position, path in queue:
            if index not in started:
                if failed:
                    return  # The rest of the queue belongs to groups not started either.
                started.add(index)
                on_start(index)
            try:
                urls[index][position], _ = await uploader.upload_file(path)
            except ImageUploadFailed as error:
                if index not in failed:
                    failed.add(index)
                    on_failure(index, error)

    raised: BaseException | None = None
    try:
        # The pool closes only once every worker is done, on success, failure or cancellation.
        async with uploader.connections(UPLOAD_CONNECTIONS), anyio.create_task_group() as tg:
            for _ in range(UPLOAD_CONNECTIONS):
                tg.start_soon(worker)
    except BaseExceptionGroup as group:
        # Raise what an upload raised as it is, as a plain gather would, not wrapped in a group.
        if len(group.exceptions) != 1:
            raise
        raised = group.exceptions[0]
    if raised is not None:
        raise raised

    return [group_urls if index in started and index not in failed else None for index, group_urls in enumerate(urls)]


async def upload_cover(cover_path: str | None, site_code: str | None = None) -> str | None:
    """Upload cover image to the image host configured for this tracker (or the global one)."""
    if not cover_path:
        click.secho("\nNo Cover Image Path was provided to upload...", fg="red", nl=False)
        return None
    host = cfg.image.resolve(site_code, "cover_uploader")
    click.secho(f"Uploading cover to {host}...", fg="yellow", nl=False)
    try:
        uploader = HOSTS[host].ImageUploader()
        url, _ = await uploader.upload_file(cover_path)
        # A host can return without raising and still hand back nothing usable; treat that as a failure too.
        if not is_http_url(url):
            click.secho(f" failed :( host returned no usable URL: {str(url)[:200]!r}", fg="red")
            return None
        click.secho(f" done! {url}", fg="yellow")
        return url
    except (ImageUploadFailed, ValueError) as error:
        click.secho(f" failed :( {error}", fg="red")
        return None


async def upload_spectrals(spectrals, uploader=None, successful=None) -> dict:
    """Upload spectral images to image host.

    Args:
        spectrals: List of (spec_id, filename, spectral_paths) tuples.
        uploader: The image host module to use.
        successful: Set of already successful spec_ids.

    Returns:
        Dictionary mapping spec_id to list of URLs.
    """
    if uploader is None:
        uploader = HOSTS[cfg.image.specs_uploader]

    successful = successful or set()
    pending = [(sid, filename, paths) for sid, filename, paths in spectrals if sid not in successful]

    def on_start(index: int) -> None:
        click.secho(f"Uploading spectrals for {pending[index][1]}...", fg="yellow")

    def on_failure(index: int, error: ImageUploadFailed) -> None:
        click.secho(f"Failed to upload spectrals for {pending[index][1]}: {error}", fg="red")

    results = await _upload_groups(uploader.ImageUploader(), [paths for _, _, paths in pending], on_start, on_failure)

    response = {}
    for (sid, _, _), urls in zip(pending, results, strict=True):
        if urls is not None:
            response[sid] = urls
            successful.add(sid)
    if len(response) < len(pending):
        retry_result = await _handle_failed_spectrals(spectrals, successful)
        return {**response, **retry_result}
    return response


async def _handle_failed_spectrals(spectrals, successful) -> dict:
    """Handle failed spectral uploads by prompting for a new host.

    Args:
        spectrals: List of spectral tuples.
        successful: Set of already successful spec_ids.

    Returns:
        Dictionary of uploaded URLs.
    """
    spec_hosts = {k: v for k, v in HOSTS.items() if k not in SPECTRALS_REFUSED}
    while True:
        host_input: str = await click.prompt(
            click.style(
                "Some spectrals failed to upload. Which image host would you like to retry "
                f"with? (Options: {', '.join(spec_hosts)})",
                fg="magenta",
                bold=True,
            ),
            default="catbox",
        )
        host = host_input.lower()
        if host in SPECTRALS_REFUSED:
            click.secho(f"{host} can't be used for spectrals: {SPECTRALS_REFUSED[host]}.", fg="red")
        elif host not in spec_hosts:
            click.secho(f"{host} is an invalid image host. Please choose another one.", fg="red")
        else:
            return await upload_spectrals(spectrals, uploader=spec_hosts[host], successful=successful)
