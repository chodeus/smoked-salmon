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


class EditedLogError(Exception):
    """Raised when a log file has been edited."""

    pass


class CRCMismatchError(Exception):
    """Raised when CRC values don't match between log and audio files."""

    pass


class DryRunComplete(BaseException):
    """Raised after a --dry-run validation to stop cleanly before any real upload.

    BaseException so ordinary ``except Exception`` handlers in the upload flow can't
    swallow it; only the up() command catches it.
    """

    pass
