import logging

from cytomine.models import UploadedFile, AbstractImage, AbstractSliceCollection, AbstractSlice, \
    Property

from pims.api.utils.response import convert_quantity
from pims.files.file import Path
from pims.formats.utils.vips import dtype_to_bits

# TODO
PENDING_PATH = Path("/tmp/uploaded")
FILE_ROOT_PATH = Path("/data/pims")


class ImportListener:
    def start_import(self, path, *args, **kwargs):
        pass

    def file_moved(self, old_path, new_path, *args, **kwargs):
        pass

    def file_not_found(self, path, *args, **kwargs):
        pass

    def file_not_moved(self, path, *args, **kwargs):
        pass

    def start_format_detection(self, path, *args, **kwargs):
        pass

    def no_matching_format(self, path, *args, **kwargs):
        pass

    def matching_format_found(self, path, format, *args, **kwargs):
        pass

    def created_role_file(self, path, role, *args, **kwargs):
        pass

    def generic_file_error(self, path, *args, **kwargs):
        pass

    def integrity_error(self, path, *args, **kwargs):
        pass

    def integrity_success(self, path, *args, **kwargs):
        pass

    def import_success(self, path, image, *args, **kwargs):
        pass

    def conversion_error(self, path, *args, **kwargs):
        pass


class CytomineListener(ImportListener):
    def __init__(self, root_id, uploaded_file_id):
        self.path_uf_mapping = dict()

        root = UploadedFile().fetch(root_id)
        self.path_uf_mapping[root.path] = root
        self.root_path = root.path

        if uploaded_file_id != root_id:
            uf = UploadedFile().fetch(uploaded_file_id)
            self.path_uf_mapping[uf.path] = uf

        self.abstract_images = []

    def propagate_error(self, uf):
        pass  # TODO: propagate error to parents

    def start_import(self, path, *args, **kwargs):
        uf = self.path_uf_mapping[str(path)]
        uf.status = UploadedFile.EXTRACTING_DATA
        uf.update()

    def file_moved(self, old_path, new_path, *args, **kwargs):
        uf = self.path_uf_mapping[str(old_path)]
        uf.filename = str(new_path.relative_to(FILE_ROOT_PATH))
        uf.update()
        self.path_uf_mapping[str(new_path)] = uf

    def file_not_moved(self, path, *args, **kwargs):
        uf = self.path_uf_mapping[str(path)]
        uf.status = UploadedFile.ERROR_EXTRACTION
        uf.update()
        self.propagate_error(uf)

    def file_not_found(self, path, *args, **kwargs):
        uf = self.path_uf_mapping[str(path)]
        uf.status = UploadedFile.ERROR_EXTRACTION
        uf.update()
        self.propagate_error(uf)

    def start_format_detection(self, path, *args, **kwargs):
        uf = self.path_uf_mapping[str(path)]
        uf.status = UploadedFile.DETECTING_FORMAT
        uf.update()
        self.propagate_error(uf)

    def no_matching_format(self, path, *args, **kwargs):
        uf = self.path_uf_mapping[str(path)]
        uf.status = UploadedFile.ERROR_FORMAT
        uf.update()
        self.propagate_error(uf)

    def matching_format_found(self, path, format, *args, **kwargs):
        uf = self.path_uf_mapping[str(path)]
        uf.contentType = format.get_identifier()  # TODO: not the content type
        uf.update()

    def created_role_file(self, path, role, *args, **kwargs):
        # TODO
        uf = UploadedFile()
        uf.originalFilename = path.filename
        uf.filename = str(path.relative_to(FILE_ROOT_PATH))
        uf.size = path.size
        uf.ext = ""
        uf.contentType = ""

        parent = self.path_uf_mapping[self.root_path]
        uf.storage = parent.storage
        uf.user = parent.user
        uf.parent = parent.id
        uf.save()
        self.path_uf_mapping[str(path)] = uf

    def generic_file_error(self, path, *args, **kwargs):
        uf = self.path_uf_mapping[str(path)]
        uf.status = UploadedFile.ERROR_DEPLOYMENT
        uf.update()
        self.propagate_error(uf)

    def integrity_error(self, path, *args, **kwargs):
        uf = self.path_uf_mapping[str(path)]
        uf.status = UploadedFile.ERROR_CONVERSION
        uf.update()
        self.propagate_error(uf)

    def integrity_success(self, path, *args, **kwargs):
        super().integrity_success(path, *args, **kwargs)

    def import_success(self, path, image, *args, **kwargs):
        uf = self.path_uf_mapping[str(path)]

        ai = AbstractImage()
        ai.uploadedFile = uf.id
        ai.originalFilename = uf.originalFilename
        ai.width = image.width
        ai.height = image.height
        ai.depth = image.depth
        ai.duration = image.duration
        ai.channels = image.n_intrinsic_channels
        if image.physical_size_x:
            ai.physicalSizeX = round(convert_quantity(image.physical_size_x, "micrometers").m, 6)
        if image.physical_size_y:
            ai.physicalSizeY = round(convert_quantity(image.physical_size_y, "micrometers").m, 6)
        if image.physical_size_z:
            ai.physicalSizeZ = round(convert_quantity(image.physical_size_z, "micrometers").m, 6)
        ai.fps = image.frame_rate
        ai.magnification = image.objective.nominal_magnification
        ai.bitPerSample = dtype_to_bits(image.pixel_type)
        ai.samplePerPixel = image.n_channels
        ai.save()
        self.abstract_images.append(ai)

        asc = AbstractSliceCollection()
        set_channel_names = image.n_intrinsic_channels == image.n_channels
        for c in range(image.n_intrinsic_channels):
            name = image.channels[c].suggested_name if set_channel_names else None
            for z in range(image.depth):
                for t in range(image.duration):
                    mime = "image/pyrtiff"  # TODO: remove
                    asc.append(AbstractSlice(ai.id, uf.id, mime, c, z, t, channelName=name))
        asc.save()

        # properties = PropertyCollection(ai)
        # for k, v in image.raw_metadata.items():
        #     properties.append(Property(ai, k, str(v)))
        # properties.save()
        # TODO: fix bug for DomainCollection save()
        for metadata in image.raw_metadata.values():
            Property(ai, metadata.namespaced_key, str(metadata.value)).save()
        # ---

        uf.status = UploadedFile.DEPLOYED
        uf.update()

    def conversion_error(self, path, *args, **kwargs):
        uf = self.path_uf_mapping[str(path)]
        uf.status = UploadedFile.ERROR_CONVERSION
        uf.update()
        self.propagate_error(uf)


class StdoutListener(ImportListener):
    def __init__(self, name):
        self.log = logging.getLogger("Upload {}".format(name))

    def start_import(self, path, *args, **kwargs):
        self.log.info("Start import for {}".format(path))

    def file_moved(self, old_path, new_path, *args, **kwargs):
        self.log.info("Moved {} to {}".format(old_path, new_path))

    def file_not_found(self, path, *args, **kwargs):
        self.log.error("File {} is not found".format(path), exc_info=kwargs.get('exception'))

    def file_not_moved(self, path, *args, **kwargs):
        self.log.error("Failed to move {}".format(path), exc_info=kwargs.get('exception'))

    def start_format_detection(self, path, *args, **kwargs):
        self.log.info("Start format detection for {}".format(path))

    def no_matching_format(self, path, *args, **kwargs):
        self.log.warning("No matching format for {}".format(path))

    def matching_format_found(self, path, format, *args, **kwargs):
        self.log.info("Identified format {} for {}".format(format.get_name(), path))

    def created_role_file(self, path, role, *args, **kwargs):
        self.log.info("Created {} file for {}".format(role, path))

    def generic_file_error(self, path, *args, **kwargs):
        self.log.error("Generic file error for {}".format(path), exc_info=kwargs.get('exception'))

    def integrity_error(self, path, *args, **kwargs):
        self.log.error("Integrity error for {}".format(path), exc_info=kwargs.get('exception'))

    def integrity_success(self, path, *args, **kwargs):
        self.log.info("{} passed integrity check".format(path))

    def import_success(self, path, image, *args, **kwargs):
        self.log.info("{} imported !".format(path))

    def conversion_error(self, path, *args, **kwargs):
        self.log.error("Error while converting {}".format(path), exc_info=kwargs.get('exception'))
