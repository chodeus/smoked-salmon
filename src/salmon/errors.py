class ScrapeError(Exception):
    def __init__(self, message, payload=None):
        self.payload = payload
        super().__init__(message)


class AbortAndDeleteFolder(Exception):
    pass


class DownloadError(Exception):
    pass


class UploadError(Exception):
    pass


class FilterError(Exception):
    pass


class TrackCombineError(Exception):
    pass


class SourceNotFoundError(Exception):
    pass


class InvalidMetadataError(Exception):
    pass


class ImageUploadFailed(Exception):
    pass


class InvalidSampleRate(Exception):
    pass


class GenreNotInWhitelist(Exception):
    pass


class NotAValidInputFile(Exception):
    pass


class UpconvertCheckError(Exception):
    """Raised when an upconvert check cannot be performed on a file."""

    pass


class UpconvertCheckNotApplicable(UpconvertCheckError):
    """Raised when a file is out of scope for the upconvert check, not broken."""

    pass


class NoncompliantFolderStructure(Exception):
    pass


class RequestError(Exception):
    pass


class RequestFailedError(RequestError):
    pass


class LoginError(RequestError):
    pass


class UnknownOutcomeError(RequestError):
    """A request that changes state failed after it may have reached the tracker, so it is not re-sent."""

    pass


class EditedLogError(Exception):
    """Raised when a log file has been edited."""

    pass


class CRCMismatchError(Exception):
    """Raised when CRC values don't match between log and audio files."""

    pass


class LogCheckSkipped(Exception):
    """Raised when a log's CRCs can't be checked against the audio; not a verdict on the rip."""

    pass


class DryRunRefused(Exception):
    """A step that would send something ran in a dry run and was stopped; not a RequestError, so no "failed upload"."""

    pass


class UploadRefusedError(RequestError):
    """The tracker's upload form has no value that describes this torrent.

    Raised while the upload form data is built, before the upload request and the authentication
    it needs, so the torrent is not uploaded to that tracker and other trackers are not affected.
    Earlier requests to that tracker (group search, request check) may already have been sent.
    """

    pass
