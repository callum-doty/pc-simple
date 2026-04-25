"""
Dropbox integration service — lists new files via cursor-based sync and downloads them.
"""

import logging
import os
from io import BytesIO
from typing import Generator, Optional, Tuple

import dropbox
from dropbox.exceptions import ApiError, AuthError
from dropbox.files import FileMetadata, ListFolderResult

logger = logging.getLogger(__name__)


def _require_env(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        raise ValueError(f"{key} environment variable is not set")
    return val


class DropboxService:
    """Wraps the Dropbox SDK for folder listing and file downloads."""

    def __init__(self):
        app_key = _require_env("DROPBOX_APP_KEY")
        app_secret = _require_env("DROPBOX_APP_SECRET")
        refresh_token = _require_env("DROPBOX_REFRESH_TOKEN")

        self._dbx = dropbox.Dropbox(
            app_key=app_key,
            app_secret=app_secret,
            oauth2_refresh_token=refresh_token,
        )
        self._folder_path = os.environ.get(
            "DROPBOX_FOLDER_PATH", "/Press Files 2019-2020/2026"
        )

    def list_new_files(
        self, cursor: Optional[str]
    ) -> Tuple[Generator[FileMetadata, None, None], str]:
        """
        Yields FileMetadata for each new/changed file since the last cursor.
        On first run (cursor=None) performs a full folder scan.
        Returns (generator, new_cursor).
        """
        try:
            if cursor:
                result: ListFolderResult = self._dbx.files_list_folder_continue(cursor)
            else:
                result = self._dbx.files_list_folder(
                    self._folder_path, recursive=False
                )
        except AuthError as e:
            logger.error(f"Dropbox auth error: {e}")
            raise
        except ApiError as e:
            if cursor and e.error.is_reset():
                # Cursor expired — fall back to full rescan
                logger.warning("Dropbox cursor reset; performing full rescan")
                result = self._dbx.files_list_folder(
                    self._folder_path, recursive=False
                )
            else:
                logger.error(f"Dropbox API error: {e}")
                raise

        def _iter(first_result: ListFolderResult):
            r = first_result
            while True:
                for entry in r.entries:
                    if isinstance(entry, FileMetadata):
                        yield entry
                if not r.has_more:
                    break
                r = self._dbx.files_list_folder_continue(r.cursor)

        # Drain the generator once to get the final cursor, but we need
        # to return both the lazy iterator and the latest cursor.
        # Strategy: collect entries page-by-page, yield them, capture cursor.
        entries: list[FileMetadata] = []
        r = result
        while True:
            for entry in r.entries:
                if isinstance(entry, FileMetadata):
                    entries.append(entry)
            if not r.has_more:
                new_cursor = r.cursor
                break
            r = self._dbx.files_list_folder_continue(r.cursor)
            new_cursor = r.cursor

        return iter(entries), new_cursor

    def download_file(self, dropbox_path: str) -> BytesIO:
        """Download a file and return its contents as a BytesIO buffer."""
        try:
            _, response = self._dbx.files_download(dropbox_path)
            buf = BytesIO(response.content)
            buf.seek(0)
            return buf
        except ApiError as e:
            logger.error(f"Failed to download {dropbox_path}: {e}")
            raise
