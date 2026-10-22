import asyncclick as click


class NoAnswerError(click.Abort):
    """A question went unanswered for too long: the job stops."""


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


class RateLimitedError(RequestError):
    """The tracker rate limited a request and asks for a longer wait than salmon gives one request."""


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


class TLSCertificateError(RequestError):
    """The TLS certificate of the host a request goes to could not be verified.

    It fails in the TLS handshake, before the request is written, so the tracker has not acted on it,
    and it is not sent again: what it is checked against (the CA certificates Python loads, the
    system clock) is on the user's machine, and gives the same answer on the next attempt.
    """

    def __init__(self, host: str, reason: str, tracker: str) -> None:
        super().__init__(f"TLS certificate verification failed for {host}: {reason}")
        self.host = host
        self.reason = reason
        # The site code, for salmon checkconf -t.
        self.tracker = tracker


class UploadRefusedError(RequestError):
    """The tracker's form has no value for this torrent: raised before its upload request, other trackers unaffected."""

    pass
