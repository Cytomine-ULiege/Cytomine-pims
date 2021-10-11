#  * Copyright (c) 2020-2021. Authors: see NOTICE file.
#  *
#  * Licensed under the Apache License, Version 2.0 (the "License");
#  * you may not use this file except in compliance with the License.
#  * You may obtain a copy of the License at
#  *
#  *      http://www.apache.org/licenses/LICENSE-2.0
#  *
#  * Unless required by applicable law or agreed to in writing, software
#  * distributed under the License is distributed on an "AS IS" BASIS,
#  * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  * See the License for the specific language governing permissions and
#  * limitations under the License.
import logging
import shutil

from celery import group, signature
from celery.result import allow_join_result

from pims.api.exceptions import (
    BadRequestException, FilepathNotFoundProblem,
    NoMatchingFormatProblem
)
from pims.api.utils.models import HistogramType
from pims.config import get_settings
from pims.files.archive import Archive, ArchiveError
from pims.files.file import (
    EXTRACTED_DIR, HISTOGRAM_STEM, ORIGINAL_STEM, PROCESSED_DIR, Path,
    SPATIAL_STEM, UPLOAD_DIR_PREFIX, unique_name_generator
)
from pims.files.histogram import build_histogram_file
from pims.files.image import Image
from pims.formats.utils.factories import FormatFactory, SpatialReadableFormatFactory
from pims.importer.listeners import CytomineListener, ImportEventType, StdoutListener

log = logging.getLogger("pims.app")

PENDING_PATH = Path(get_settings().pending_path)
FILE_ROOT_PATH = Path(get_settings().root)


class FileErrorProblem(BadRequestException):
    pass


class ImageParsingProblem(BadRequestException):
    pass


class FormatConversionProblem(BadRequestException):
    pass


class FileImporter:
    """
    Image importer from file. It moves a pending file to PIMS root path, tries to
    identify the file format, converts it if needed and checks its integrity.

    Attributes
    ----------
    pending_file : Path
        A file to import from PENDING_PATH directory
    pending_name : str (optional)
        A name to use for the pending file.
        If not provided, the current pending file name is used.
    loggers : list of ImportLogger (optional)
        A list of import loggers

    """

    def __init__(self, pending_file, pending_name=None, loggers=None):
        self.loggers = loggers if loggers is not None else []
        self.pending_file = pending_file
        self.pending_name = pending_name

        self.upload_dir = None
        self.upload_path = None
        self.original_path = None
        self.original = None
        self.spatial_path = None
        self.spatial = None
        self.histogram_path = None
        self.histogram = None

        self.processed_dir = None
        self.extracted_dir = None

    def notify(self, method, *args, **kwargs):
        for logger in self.loggers:
            try:
                getattr(logger, method)(*args, **kwargs)
            except AttributeError:
                log.warning(f"No method {method} for import logger {logger}")

    def run(self, prefer_copy=False):
        """
        Import the pending file. It moves a pending file to PIMS root path, tries to
        identify the file format, converts it if needed and checks its integrity.

        Parameters
        ----------
        prefer_copy : bool
            Prefer copy the pending file instead of moving it. Useful for tests.

        Returns
        -------
        images : list of Image
            A list of images imported from the pending file.

        Raises
        ------
        FilepathNotFoundProblem
            If pending file is not found.
        """
        try:
            self.notify(ImportEventType.START_DATA_EXTRACTION, self.pending_file)

            # Check the file is in pending area,
            # or comes from a extracted collection
            if (not self.pending_file.is_extracted() and
                self.pending_file.parent != PENDING_PATH) \
                    or not self.pending_file.exists():
                self.notify(ImportEventType.FILE_NOT_FOUND, self.pending_file)
                raise FilepathNotFoundProblem(self.pending_file)

            # Move the file to PIMS root path
            upload_dir_name = Path(
                f"{UPLOAD_DIR_PREFIX}"
                f"{str(unique_name_generator())}"
            )
            self.upload_dir = FILE_ROOT_PATH / upload_dir_name
            self.mkdir(self.upload_dir)

            if self.pending_name:
                name = self.pending_name
            else:
                name = self.pending_file.name
            self.upload_path = self.upload_dir / name

            self.move(self.pending_file, self.upload_path, prefer_copy)

            # If the pending file comes from an archive
            if not prefer_copy and self.pending_file.is_extracted():
                # Create symlink in processed to keep track of parent archive
                self.mksymlink(self.pending_file, self.upload_path)

            self.notify(
                ImportEventType.MOVED_PENDING_FILE,
                self.pending_file, self.upload_path
            )
            self.notify(ImportEventType.END_DATA_EXTRACTION, self.upload_path)

            # Identify format
            self.notify(ImportEventType.START_FORMAT_DETECTION, self.upload_path)

            format_factory = FormatFactory()
            format = format_factory.match(self.upload_path)
            archive = None
            if format is None:
                archive = Archive.from_path(self.upload_path)
                if archive:
                    format = archive.format

            if format is None:
                self.notify(ImportEventType.ERROR_NO_FORMAT, self.upload_path)
                raise NoMatchingFormatProblem(self.upload_path)
            self.notify(
                ImportEventType.END_FORMAT_DETECTION,
                self.upload_path, format
            )

            # Create processed dir
            self.processed_dir = self.upload_dir / Path(PROCESSED_DIR)
            self.mkdir(self.processed_dir)

            # Create original role
            original_filename = Path(
                f"{ORIGINAL_STEM}.{format.get_identifier()}"
            )
            self.original_path = self.processed_dir / original_filename
            if archive:
                try:
                    self.notify(
                        ImportEventType.START_UNPACKING, self.upload_path
                    )
                    archive.extract(self.original_path)
                except ArchiveError as e:
                    self.notify(
                        ImportEventType.ERROR_UNPACKING, self.upload_path,
                        exception=e
                    )
                    raise FileErrorProblem(self.upload_path)

                # Now the archive is extracted, check if it's a multi-file format
                format = format_factory.match(self.original_path)
                if format:
                    # It is a multi-file format
                    original_filename = Path(
                        f"{ORIGINAL_STEM}.{format.get_identifier()}"
                    )
                    new_original_path = self.processed_dir / original_filename
                    self.move(self.original_path, new_original_path)
                    self.original_path = new_original_path

                    self.notify(
                        ImportEventType.END_UNPACKING, self.upload_path,
                        self.original_path, format=format, is_collection=False
                    )
                    self.upload_path = self.original_path
                else:
                    self.extracted_dir = self.processed_dir / Path(EXTRACTED_DIR)
                    self.mksymlink(self.extracted_dir, self.original_path)

                    collection = self.import_collection(
                        self.original_path, prefer_copy
                    )
                    self.notify(
                        ImportEventType.END_UNPACKING, self.upload_path,
                        self.original_path, is_collection=True
                    )
                    return collection
            else:
                self.mksymlink(self.original_path, self.upload_path)
                assert self.original_path.has_original_role()

            # Check original image integrity
            self.notify(ImportEventType.START_INTEGRITY_CHECK, self.original_path)
            self.original = Image(self.original_path, format=format)
            errors = self.original.check_integrity(metadata=True)
            if len(errors) > 0:
                self.notify(
                    ImportEventType.ERROR_INTEGRITY_CHECK, self.original_path,
                    integrity_errors=errors
                )
                raise ImageParsingProblem(self.original)
            self.notify(ImportEventType.END_INTEGRITY_CHECK, self.original)

            if format.is_spatial():
                self.deploy_spatial(format)
            else:
                raise NotImplementedError()

            self.deploy_histogram(self.original.get_spatial())

            # Finished
            self.notify(
                ImportEventType.END_SUCCESSFUL_IMPORT,
                self.upload_path, self.original
            )
            return [self.upload_path]
        except Exception as e:
            self.notify(
                ImportEventType.FILE_ERROR,
                self.upload_path, exeception=e
            )
            raise e

    def deploy_spatial(self, format):
        self.notify(ImportEventType.START_SPATIAL_DEPLOY, self.original_path)
        if format.need_conversion:
            # Do the spatial conversion
            try:
                ext = format.conversion_format().get_identifier()
                spatial_filename = Path(f"{SPATIAL_STEM}.{ext}")
                self.spatial_path = self.processed_dir / spatial_filename
                self.notify(
                    ImportEventType.START_CONVERSION,
                    self.spatial_path, self.upload_path
                )

                r = format.convert(self.spatial_path)
                if not r or not self.spatial_path.exists():
                    self.notify(
                        ImportEventType.ERROR_CONVERSION,
                        self.spatial_path
                    )
                    raise FormatConversionProblem()
            except Exception as e:
                self.notify(
                    ImportEventType.ERROR_CONVERSION,
                    self.spatial_path, exception=e
                )
                raise FormatConversionProblem()

            self.notify(ImportEventType.END_CONVERSION, self.spatial_path)

            # Check format of converted file
            self.notify(ImportEventType.START_FORMAT_DETECTION, self.spatial_path)
            spatial_format = SpatialReadableFormatFactory().match(self.spatial_path)
            if not spatial_format:
                self.notify(ImportEventType.ERROR_NO_FORMAT, self.spatial_path)
                raise NoMatchingFormatProblem(self.spatial_path)
            self.notify(
                ImportEventType.END_FORMAT_DETECTION,
                self.spatial_path, spatial_format
            )

            self.spatial = Image(self.spatial_path, format=spatial_format)

            # Check spatial image integrity
            self.notify(ImportEventType.START_INTEGRITY_CHECK, self.spatial_path)
            errors = self.spatial.check_integrity(metadata=True)
            if len(errors) > 0:
                self.notify(
                    ImportEventType.ERROR_INTEGRITY_CHECK, self.spatial_path,
                    integrity_errors=errors
                )
                raise ImageParsingProblem(self.spatial)
            self.notify(ImportEventType.END_INTEGRITY_CHECK, self.spatial)

        else:
            # Create spatial role
            spatial_filename = Path(f"{SPATIAL_STEM}.{format.get_identifier()}")
            self.spatial_path = self.processed_dir / spatial_filename
            self.mksymlink(self.spatial_path, self.original_path)
            self.spatial = Image(self.spatial_path, format=format)

        assert self.spatial.has_spatial_role()
        self.notify(ImportEventType.END_SPATIAL_DEPLOY, self.spatial)
        return self.spatial

    def deploy_histogram(self, image):
        self.histogram_path = self.processed_dir / Path(HISTOGRAM_STEM)
        self.notify(
            ImportEventType.START_HISTOGRAM_DEPLOY,
            self.histogram_path, image
        )
        try:
            self.histogram = build_histogram_file(
                image, self.histogram_path, HistogramType.FAST
            )
        except (FileNotFoundError, FileExistsError) as e:
            self.notify(
                ImportEventType.ERROR_HISTOGRAM, self.histogram_path, image,
                exception=e
            )
            raise FileErrorProblem(self.histogram_path)

        assert self.histogram.has_histogram_role()
        self.notify(
            ImportEventType.END_HISTOGRAM_DEPLOY, self.histogram_path, image
        )
        return self.histogram

    def mkdir(self, directory: Path):
        try:
            directory.mkdir()  # TODO: mode
        except (FileNotFoundError, FileExistsError, OSError) as e:
            self.notify(ImportEventType.FILE_ERROR, directory, exception=e)
            raise FileErrorProblem(directory)

    def move(self, origin: Path, dest: Path, prefer_copy: bool = False):
        try:
            if prefer_copy:
                shutil.copy(origin, dest)
            else:
                shutil.move(origin, dest)
        except (FileNotFoundError, FileExistsError, OSError) as e:
            self.notify(ImportEventType.FILE_NOT_MOVED, origin, exception=e)
            raise FileErrorProblem(origin)

    def mksymlink(self, path: Path, target: Path):
        try:
            path.symlink_to(
                target,
                target_is_directory=target.is_dir()
            )
        except (FileNotFoundError, FileExistsError, OSError) as e:
            self.notify(ImportEventType.FILE_ERROR, path, exception=e)
            raise FileErrorProblem(path)

    def import_collection(self, collection, prefer_copy=False):
        cytomine = None
        for logger in self.loggers:
            if isinstance(logger, CytomineListener):
                cytomine = logger
                break
        if cytomine:
            task = "pims.tasks.worker.run_import_with_cytomine"
        else:
            task = "pims.tasks.worker.run_import"

        imported = list()
        format_factory = FormatFactory()
        tasks = list()
        for child in collection.get_extracted_children(stop_recursion_cond=format_factory.match):
            self.notify(
                ImportEventType.REGISTER_FILE, child, self.upload_path
            )
            try:
                if cytomine:
                    new_listener = cytomine.new_listener_from_registered_child(child)
                    args = [
                        new_listener.auth, str(child), child.name, new_listener, prefer_copy
                    ]
                else:
                    args = [str(child), child.name, prefer_copy]
                tasks.append(signature(task, args=args))
            except Exception as _:  # noqa
                # Do not propagate error to siblings
                # Each importer is independent
                pass

        task_group = group(tasks)
        # WARNING !
        # These tasks are synchronous with respect to the parent task (the archive)
        # It is required to update the parent (the archive) status when everything is
        # finished. Code should be refactored to use Celery callbacks but it does not
        # seem so easy.
        # Current implementation may cause deadlock if the worker pool is exhausted,
        # while the parent task is waiting for subtasks to finish.
        # http://docs.celeryq.org/en/latest/userguide/tasks.html#task-synchronous-subtasks
        with allow_join_result():
            r = task_group.apply_async()
            r.get()  # Wait for group to finish

        return imported


def run_import(filepath, name, extra_listeners=None, prefer_copy=False):
    pending_file = Path(filepath)

    if extra_listeners is not None:
        if not type(extra_listeners) is list:
            extra_listeners = list(extra_listeners)
    else:
        extra_listeners = []

    listeners = [StdoutListener(name)] + extra_listeners
    fi = FileImporter(pending_file, name, listeners)
    fi.run(prefer_copy)
