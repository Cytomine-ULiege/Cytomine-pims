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
from datetime import datetime
from functools import cached_property

from pims import UNIT_REGISTRY
from pims.formats.utils.abstract import AbstractFormat, AbstractParser, AbstractReader
from pims.formats.utils.annotations import ParsedMetadataAnnotation
from pims.formats.utils.checker import SignatureChecker
from pims.formats.utils.engines.vips import VipsHistogramReader, VipsSpatialConvertor
from pims.formats.utils.metadata import ImageChannel, ImageMetadata, parse_float
from pims.formats.utils.vips import np_dtype
from pydicom import dcmread
from pydicom.multival import MultiValue
from shapely.errors import WKTReadingError
from shapely.wkt import loads as wkt_loads

log = logging.getLogger("pims.formats")


def cached_dcmread(format):
    return format.get_cached('_dcmread', dcmread, format.path.resolve(), force=True)


class DicomChecker(SignatureChecker):
    OFFSET = 128

    @classmethod
    def match(cls, pathlike):
        buf = cls.get_signature(pathlike)
        return (len(buf) > cls.OFFSET + 4 and
                buf[cls.OFFSET] == 0x44 and
                buf[cls.OFFSET + 1] == 0x49 and
                buf[cls.OFFSET + 2] == 0x43 and
                buf[cls.OFFSET + 3] == 0x4D)


class DicomParser(AbstractParser):
    def parse_main_metadata(self):
        ds = cached_dcmread(self.format)

        imd = ImageMetadata()
        imd.width = ds.Columns
        imd.height = ds.Rows
        imd.depth = ds.get('NumberOfFrames', 1)
        imd.duration = 1

        imd.n_channels = ds.SamplesPerPixel  # Only 1 or 3
        imd.n_intrinsic_channels = ds.SamplesPerPixel
        imd.n_channels_per_read = 1
        if imd.n_channels == 3:
            imd.set_channel(ImageChannel(index=0, suggested_name='R'))
            imd.set_channel(ImageChannel(index=1, suggested_name='G'))
            imd.set_channel(ImageChannel(index=2, suggested_name='B'))
        else:
            imd.set_channel(ImageChannel(index=0, suggested_name='L'))

        imd.significant_bits = ds.BitsAllocated
        imd.pixel_type = np_dtype(imd.significant_bits)
        return imd

    def parse_known_metadata(self):
        ds = cached_dcmread(self.format)
        imd = super().parse_known_metadata()

        imd.description = None  # TODO
        imd.acquisition_datetime = self.parse_acquisition_date(
            ds.get('AcquisitionDate'), ds.get('AcquisitionTime'))
        if imd.acquisition_datetime is None:
            imd.acquisition_datetime = self.parse_acquisition_date(
                ds.get('ContentDate'), ds.get('ContentTime')
            )
        pixel_spacing = ds.get('PixelSpacing')
        if pixel_spacing:
            imd.physical_size_x = self.parse_physical_size(pixel_spacing[0])
            imd.physical_size_y = self.parse_physical_size(pixel_spacing[1])
        imd.physical_size_z = self.parse_physical_size(ds.get('SpacingBetweenSlices'))

        imd.is_complete = True
        return imd

    @staticmethod
    def parse_acquisition_date(date, time=None):
        """
        Date examples: 20211105
        Time examples: 151034, 151034.123
        """
        try:
            if date and time:
                return datetime.strptime(f"{date} {time.split('.')[0]}", "%Y%m%d %H%M%S")
            elif date:
                return datetime.strptime(date, "%Y%m%d")
            else:
                return None
        except (ValueError, TypeError):
            return None

    def parse_raw_metadata(self):
        store = super(DicomParser, self).parse_raw_metadata()
        ds = cached_dcmread(self.format)

        for data_element in ds:
            if type(data_element.value) in (bytes, bytearray) \
                    or data_element.VR == "SQ":  # TODO: support sequence metadata
                continue

            name = data_element.name
            if data_element.is_private:
                name = f"{data_element.tag.group:04x}_{data_element.tag.element:04x}"
            name = name.replace(' ', '')

            value = data_element.value
            if type(value) is MultiValue:
                value = list(value)
            store.set(name, value, namespace="DICOM")
        return store

    @staticmethod
    def parse_physical_size(physical_size):
        if physical_size is not None and parse_float(physical_size) is not None:
            return parse_float(physical_size) * UNIT_REGISTRY("millimeter")
        return None

    def parse_annotations(self):
        """
        DICOM/DICONDE extension for Annotations
        * 0x0077-0x1900 (US) - Annotation.Number
        * 0x0077-0x1901 (SQ) - Annotation.Definition
        * 0x0077-0x1912 (DS, multiple) - Annotation.Row
        * 0x0077-0x1913 (DS, multiple) - Annotation.Col
        * 0x0077-0x1903 (LO) - Annotation.Indication
        * 0x0077-0x1904 (US) - Annotation.Severity
        * 0x0077-0x1911 (LT) - Annotation.Polygon (WKT format)
        """
        ds = cached_dcmread(self.format)
        channels = list(range(self.format.main_imd.n_channels))
        parsed_annots = []
        annots_sq = ds.get((0x77, 0x1901))
        if annots_sq and annots_sq.VR == "SQ":
            for annot in annots_sq:
                try:
                    wkt = annot.get((0x77, 0x1911))
                    if wkt.value is not None:
                        geometry = wkt_loads(wkt.value)
                        parsed = ParsedMetadataAnnotation(geometry, channels, 0, 0)

                        indication = annot.get((0x77, 0x1903))
                        if indication:
                            parsed.add_term(indication.value)

                        severity = annot.get((0x77, 0x1904))
                        if severity:
                            parsed.add_property("severity", severity.value)

                        parsed_annots.append(parsed)
                except WKTReadingError:
                    pass

        return parsed_annots


class DicomReader(AbstractReader):
    def read_thumb(self, out_width, out_height, precomputed=None, c=None, z=None, t=None):
        pass

    def read_window(self, region, out_width, out_height, c=None, z=None, t=None):
        pass

    def read_tile(self, tile, c=None, z=None, t=None):
        pass


class DicomFormat(AbstractFormat):
    """Dicom Format.

    References

    """
    checker_class = DicomChecker
    parser_class = DicomParser
    reader_class = DicomReader
    histogram_reader_class = VipsHistogramReader  # TODO
    convertor_class = VipsSpatialConvertor  # TODO

    def __init__(self, *args, **kwargs):
        super(DicomFormat, self).__init__(*args, **kwargs)
        self._enabled = True

    @classmethod
    def is_spatial(cls):
        return True

    @cached_property
    def need_conversion(self):
        imd = self.main_imd
        return not (imd.width < 1024 and imd.height < 1024)  # TODO

    @property
    def media_type(self):
        return "application/dicom"
