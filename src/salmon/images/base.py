import mimetypes

mimetypes.init()


class BaseImageUploader:
    """Base class for image uploaders.

    Subclasses should implement the async upload_file method.
    """

    def validate_file(self, filename: str) -> None:
        """Raise ValueError unless ``filename`` has an image MIME type."""
        mime_type, _ = mimetypes.guess_type(filename)
        if not mime_type or mime_type.split("/")[0] != "image":
            raise ValueError(f"Unknown image file type {mime_type}")

    async def upload_file(self, filename: str) -> tuple[str, str | None]:
        """Upload an image file and return the URL.

        Args:
            filename: Path to the image file.

        Returns:
            Tuple of (url, deletion_url). deletion_url may be None.

        Raises:
            ValueError: If the file is not an image.
            NotImplementedError: If not overridden by subclass.
        """
        self.validate_file(filename)
        raise NotImplementedError("Subclasses must implement upload_file")
