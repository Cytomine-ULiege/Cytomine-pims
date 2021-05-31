# * Copyright (c) 2020. Authors: see NOTICE file.
# *
# * Licensed under the Apache License, Version 2.0 (the "License");
# * you may not use this file except in compliance with the License.
# * You may obtain a copy of the License at
# *
# *      http://www.apache.org/licenses/LICENSE-2.0
# *
# * Unless required by applicable law or agreed to in writing, software
# * distributed under the License is distributed on an "AS IS" BASIS,
# * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# * See the License for the specific language governing permissions and
# * limitations under the License.
from datetime import datetime
from enum import Enum
from io import BytesIO
from typing import Optional, Union, List

from flask import current_app, send_file
from connexion import request
from pydantic import BaseModel, Field, conint

from pims.api.exceptions import check_path_existence, check_path_is_single, \
    check_representation_existence, NoAppropriateRepresentationProblem
from pims.api.utils.header import add_image_size_limit_header
from pims.api.utils.image_parameter import get_thumb_output_dimensions, safeguard_output_dimensions
from pims.api.utils.mimetype import get_output_format, VISUALISATION_MIMETYPES
from pims.api.utils.models import FormatId, ZoomOrLevel, CollectionSize
from pims.api.utils.parameter import filepath2path, path2filepath
from pims.api.utils.response import response_list, convert_quantity
from pims.formats.utils.metadata import MetadataType
from pims.processing.image_response import AssociatedResponse

from fastapi import APIRouter

router = APIRouter()
api_tags = ['Metadata']


class FileRole(Enum):
    """
    The role of a file. The same image data can be represented in different ways, in different files, each of them
    serving different purposes.

    * `UPLOAD` - This file is the one such as received by PIMS.
    * `ORIGINAL` - This file is in its original format and contains (part of) metadata.
    * `SPATIAL` - This file is used to retrieve regular 2D spatial regions from the image.
    * `SPECTRAL` - This file is used to retrieve spectral data from the image.
    * `NONE` - This file has no defined role for PIMS.
    """

    UPLOAD = 'UPLOAD'
    ORIGINAL = 'ORIGINAL'
    SPATIAL = 'SPATIAL'
    SPECTRAL = 'SPECTRAL'
    NONE = 'NONE'

    @classmethod
    def from_path(cls, path):
        role = cls.NONE
        if path.has_original_role():
            role = cls.ORIGINAL
        if path.has_spatial_role():
            role = cls.SPATIAL
        if path.has_spectral_role():
            role = cls.SPECTRAL
        if path.has_upload_role():
            role = cls.UPLOAD
        return role


class FileType(Enum):
    """
    The type of the file.
    * `SINGLE` - The file only has one image.
    * `COLLECTION` - The file is a container and contains multiple images that need further processing.
    """

    SINGLE = 'SINGLE'
    COLLECTION = 'COLLECTION'

    @classmethod
    def from_path(cls, path):
        if path.is_collection():
            return cls.COLLECTION
        return cls.SINGLE


class SingleFileInfo(BaseModel):
    """
    Information about a file
    """

    file_type: FileType
    filepath: str = Field(
        ...,
        description='The file path (filename with path, relative to the server root)',
        example='/a/b/c/thefile.png',
    )
    stem: str = Field(
        ...,
        description='The file stem (filename without extension)',
        example='thefile',
    )
    extension: str = Field(
        ..., description='The file extension', example='.png'
    )
    created_at: datetime = Field(..., description='The file creation date')
    size: int = Field(..., description='The file size, in bytes.')
    is_symbolic: bool = Field(
        False, description='Whether the file is a symbolic link or not'
    )
    role: FileRole


class CollectionFileInfo(SingleFileInfo):
    children: List[SingleFileInfo] = Field(
        ..., description='Information about children files'
    )


class FileInfo(BaseModel):
    __root__: Union[CollectionFileInfo, SingleFileInfo]

    @classmethod
    def from_path(cls, path):
        info = {
            "file_type": FileType.from_path(path),
            "filepath": path2filepath(path),
            "stem": path.true_stem,
            "extension": path.extension,
            "created_at": path.creation_datetime,
            "size": path.size,
            "is_symbolic": path.is_symlink(),
            "role": FileRole.from_path(path)
        }
        if path.is_collection():
            children = [FileInfo.from_path(p) for p in path.get_extracted_children()]
            return CollectionFileInfo(children=children, **info)
        else:
            return SingleFileInfo(**info)


class PixelType(Enum):
    """
    The type used to store each pixel in the image.
    """

    int8 = 'int8'
    int16 = 'int16'
    int32 = 'int32'
    uint8 = 'uint8'
    uint16 = 'uint16'
    uint32 = 'uint32'


class ImageInfo(BaseModel):
    """
    Information about an image

    """

    original_format: FormatId = Field(
        ..., description='The original image format identifier.'
    )
    width: conint(ge=1) = Field(
        ...,
        description='The (multidimensional) image width. It is the number of pixels along X axis.',
    )
    height: conint(ge=1) = Field(
        ...,
        description='The (multidimensional) image height. It is the number of pixels along Y axis.',
    )
    depth: conint(ge=1) = Field(
        ...,
        description='The multidimensional image depth. It is the number of focal planes.',
    )
    duration: conint(ge=1) = Field(
        ...,
        description='The multidimensional image duration. It is the number of frames.',
    )
    physical_size_x: Optional[float] = Field(
        None,
        description='The physical size of a pixel along the X axis, expressed in micrometers (µm).',
    )
    physical_size_y: Optional[float] = Field(
        None,
        description='The physical size of a pixel along the Y axis, expressed in micrometers (µm).',
    )
    physical_size_z: Optional[float] = Field(
        None,
        description='The physical size of a pixel (voxel) along the Z axis, expressed in micrometers (µm).',
    )
    frame_rate: Optional[float] = Field(
        None,
        description='The frequency at which consecutive timepoints are taken (T axis), expressed in Hz.',
    )
    n_channels: conint(ge=1) = Field(
        ...,
        description='The number of channels in the image.'
                    'Grayscale images have 1 channel. RGB images have 3 channels.',
    )
    n_intrinsic_channels: int = Field(
        ...,
        description='The number of intrinsic channel planes in the image.'
                    'A RGB image has 3 channels, but they are usually interleaved in a single plane. In such a case,'
                    'there is only 1 intrinsic channel.',
    )
    n_planes: int = Field(
        ...,
        description='The number of intrinsic planes in the image.'
                    'It is computed as `n_intrinsic_channels * depth * duration`.',
    )
    acquired_at: Optional[datetime] = Field(
        None, description='The acquisition date of the image.'
    )
    description: Optional[str] = Field(None, description='The image description.')
    pixel_type: PixelType = Field(
        ..., description='The type used to store each pixel in the image.'
    )
    significant_bits: conint(ge=1) = Field(
        ...,
        description='The number of bits within the type storing each pixel that are significant.',
    )

    @classmethod
    def from_image(cls, image):
        return cls(**{
            "original_format": image.format.get_identifier(),
            "width": image.width,
            "height": image.height,
            "depth": image.depth,
            "duration": image.duration,
            "n_channels": image.n_channels,
            "n_intrinsic_channels": image.n_intrinsic_channels,
            "n_planes": image.n_planes,
            "physical_size_x": convert_quantity(image.physical_size_x, "micrometers"),
            "physical_size_y": convert_quantity(image.physical_size_y, "micrometers"),
            "physical_size_z": convert_quantity(image.physical_size_z, "micrometers"),
            "frame_rate": image.frame_rate,
            "acquired_at": image.acquisition_datetime,
            "description": image.description,
            "pixel_type": PixelType[str(image.pixel_type)],
            "significant_bits": image.significant_bits
        })


class TierInfo(BaseModel):
    zoom: ZoomOrLevel = Field(..., description='The zoom at this tier')
    level: ZoomOrLevel = Field(..., description='The level at this tier')
    width: conint(ge=1) = Field(..., description='The tier width')
    height: conint(ge=1) = Field(..., description='The tier height')
    tile_width: conint(ge=1) = Field(
        ..., description='The width of a tile', example=256
    )
    tile_height: conint(ge=1) = Field(
        ..., description='The height of a tile', example=256
    )
    downsampling_factor: float = Field(
        ...,
        description='The factor by which the tier downsamples the basis of the pyramid.',
        example=2.0,
    )
    n_tiles: int = Field(..., description='The number of tiles at this tier')
    n_tx: int = Field(
        ..., description='The number of tiles along horizontal axis'
    )
    n_ty: int = Field(
        ..., description='The number of tiles along vertical axis'
    )

    @classmethod
    def from_tier(cls, tier):
        return cls(**{
            "width": tier.width,
            "height": tier.height,
            "level": tier.level,
            "zoom": tier.zoom,
            "tile_width": tier.tile_width,
            "tile_height": tier.tile_height,
            "downsampling_factor": tier.average_factor,
            "n_tiles": tier.max_ti,
            "n_tx": tier.max_tx,
            "n_ty": tier.max_ty
        })


class PyramidInfo(BaseModel):
    """
    Information about an image pyramid.
    """

    n_tiers: conint(ge=1) = Field(
        ..., description='The number of tiers in the pyramid.'
    )
    tiers: List[TierInfo]

    @classmethod
    def from_pyramid(cls, pyramid):
        return cls(
            n_tiers=pyramid.n_levels,
            tiers=[TierInfo.from_tier(tier) for tier in pyramid]
        )


class SimpleRepresentationInfo(BaseModel):
    """
    Information about an image representation.

    """
    role: FileRole
    file: FileInfo


class FullRepresentationInfo(SimpleRepresentationInfo):
    pyramid: PyramidInfo


class RepresentationInfo(BaseModel):
    __root__: Union[FullRepresentationInfo, SimpleRepresentationInfo]

    @classmethod
    def from_path(cls, path):
        if path.has_spatial_role() or path.has_spectral_role():
            return FullRepresentationInfo(
                role=FileRole.from_path(path),
                file=FileInfo.from_path(path),
                pyramid=PyramidInfo.from_pyramid(path.pyramid)
            )
        else:
            return SimpleRepresentationInfo(
                role=FileRole.from_path(path),
                file=FileInfo.from_path(path)
            )


class Microscope(BaseModel):
    model: Optional[str] = Field(None, description='The microscope model.')

    @classmethod
    def from_image(cls, image):
        return cls(model=image.microscope.model)


class Objective(BaseModel):
    nominal_magnification: Optional[float] = Field(
        None, description='Magnification of the lens specified by the manufacturer.'
    )
    calibrated_magnification: Optional[float] = Field(
        None, description='Magnification of the lens measured by a calibration process.'
    )

    @classmethod
    def from_image(cls, image):
        return cls(nominal_magnification=image.objective.nominal_magnification,
                   calibrated_magnification=image.objective.calibrated_magnification)


class InstrumentInfo(BaseModel):
    """
    Information about an instrument in an image file.
    """

    microscope: Microscope
    objective: Objective

    @classmethod
    def from_image(cls, image):
        return cls(microscope=Microscope.from_image(image),
                   objective=Objective.from_image(image))


class ChannelsInfoItem(BaseModel):
    index: conint(ge=0) = Field(..., description='Channel index.')
    suggested_name: str = Field(
        ...,
        description='Suggested name for the channel inferred from other properties.',
    )
    emission_wavelength: Optional[float] = Field(
        None, description='Wavelength of emission for a particular channel.'
    )
    excitation_wavelength: Optional[float] = Field(
        None, description='Wavelength of excitation for a particular channel.'
    )

    @classmethod
    def from_channel(cls, c):
        return cls(**{
            "index": c.index,
            "emission_wavelength": c.emission_wavelength,
            "excitation_wavelength": c.excitation_wavelength,
            "suggested_name": c.suggested_name
        })


class ChannelsInfo(BaseModel):
    """
    Information about channels in an image file.
    """

    __root__: List[ChannelsInfoItem] = Field(
        ..., description='Information about channels in an image file.'
    )

    @classmethod
    def from_image(cls, image):
        return [ChannelsInfoItem.from_channel(c) for c in image.channels]


class AssociatedName(Enum):
    """
    The type of associated image.

    `macro` - A macro image (generally, in slide scanners, a low resolution picture of the entire slide)
    `label` - A label image (generally a barcode)
    `thumb` - A pre-computed thumbnail
    """

    macro = 'macro'
    label = 'label'
    thumb = 'thumb'


class AssociatedInfoItem(BaseModel):
    """
    Associated images are metadata image stored in the original image file.
    """

    width: conint(ge=1) = Field(
        ...,
        description='The associated image width. It is the number of pixels along X axis.',
    )
    height: conint(ge=1) = Field(
        ...,
        description='The associated image height. It is the number of pixels along Y axis.',
    )
    n_channels: conint(ge=1) = Field(
        ...,
        description='The number of channels in the associated image.'
                    'Grayscale images have 1 channel. RGB images have 3 channels.',
    )
    name: AssociatedName = Field(
        ...,
        description='The type of associated image.'
                    ''
                    '`macro` - A macro image (generally, in slide scanners, a low resolution picture of the entire slide)'
                    '`label` - A label image (generally a barcode)'
                    '`thumb` - A pre-computed thumbnail',
    )

    @classmethod
    def from_associated(cls, associated):
        return cls(**{
            "name": AssociatedName[associated._kind],
            "width": associated.width,
            "height": associated.height,
            "n_channels": associated.n_channels
        })


class AssociatedInfo(BaseModel):
    """
    Information about all associated in an image file.
    """

    __root__: List[AssociatedInfoItem] = Field(
        ..., description='Information about associated in an image file.'
    )

    @classmethod
    def from_image(cls, image):
        return [
            AssociatedInfoItem.from_associated(associated) for associated
            in (image.associated_thumb, image.associated_label, image.associated_macro)
            if associated.exists
        ]


class MetadataTypeEnum(Enum):
    """
    The metadata value type
    """

    STRING = 'STRING'
    INTEGER = 'INTEGER'
    DECIMAL = 'DECIMAL'
    BOOLEAN = 'BOOLEAN'
    JSON = 'JSON'
    BASE64 = 'BASE64'
    DATE = 'DATE'
    DATETIME = 'DATETIME'
    TIME = 'TIME'
    LIST = 'LIST'
    UNKNOWN = 'UNKNOWN'


class Metadata(BaseModel):
    """
    A metadata is a key-value pair stored in an image file.

    """

    key: str = Field(..., description='The metadata key')
    value: str = Field(..., description='The metadata value')
    type: MetadataTypeEnum = Field('STRING', description='The metadata value type')
    namespace: Optional[str] = Field(
        None, description='The metadata namespace to avoid key name conflicts'
    )

    @classmethod
    def from_metadata(cls, metadata):
        return cls(**{
            "namespace": metadata.namespace,
            "key": metadata.key,
            "value": metadata.value if metadata.metadata_type != MetadataType.UNKNOWN else str(metadata.value),
            "type": MetadataTypeEnum[metadata.metadata_type.name]
        })


class ImageFullInfo(BaseModel):
    image: ImageInfo
    channels: ChannelsInfo
    instrument: InstrumentInfo
    associated: AssociatedInfo
    representations: List[RepresentationInfo]


@router.get(
    '/file/{filepath:path}/info',
    response_model=FileInfo,
    tags=api_tags
)
def show_file(
        filepath: str
):
    """
    Get file info
    """
    path = filepath2path(filepath)
    check_path_existence(path)
    return FileInfo.from_path(path)


@router.get(
    '/image/{filepath:path}/info',
    response_model=ImageFullInfo,
    tags=api_tags
)
def show_info(
        filepath: str
):
    """
    Get all image info
    """
    path = filepath2path(filepath)
    check_path_existence(path)
    check_path_is_single(path)

    original = path.get_original()
    check_representation_existence(original)
    data = dict()
    data["image"] = ImageInfo.from_image(original)
    data["instrument"] = InstrumentInfo.from_image(original)
    data["associated"] = AssociatedInfo.from_image(original)
    data["channels"] = ChannelsInfo.from_image(original)
    data["representations"] = [RepresentationInfo.from_path(rpr) for rpr in original.get_representations()]
    return data


# IMAGE

@router.get(
    '/image/{filepath:path}/info/image',
    response_model=ImageInfo,
    tags=api_tags
)
def show_image(
        filepath: str
):
    """
    Get standard image info
    """
    path = filepath2path(filepath)
    check_path_existence(path)
    check_path_is_single(path)

    original = path.get_original()
    check_representation_existence(original)
    return ImageInfo.from_image(original)


# CHANNELS

class ChannelsInfoCollection(CollectionSize):
    items: ChannelsInfo = Field(None, description='Array of channels', title='Channel')


@router.get(
    '/image/{filepath:path}/info/channels',
    response_model=ChannelsInfoCollection,
    tags=api_tags
)
def show_channels(filepath: str):
    """
    Get image channel info
    """
    path = filepath2path(filepath)
    check_path_existence(path)
    check_path_is_single(path)

    original = path.get_original()
    check_representation_existence(original)
    return response_list(ChannelsInfo.from_image(original))


# PYRAMID

@router.get(
    '/image/{filepath:path}/info/normalized-pyramid',
    response_model=PyramidInfo,
    tags=api_tags
)
def show_normalized_pyramid(
        filepath: str
):
    """
    Get image normalized pyramid
    """
    path = filepath2path(filepath)
    check_path_existence(path)
    check_path_is_single(path)

    original = path.get_original()
    check_representation_existence(original)
    return PyramidInfo.from_pyramid(original.normalized_pyramid)


# INSTRUMENT

@router.get(
    '/image/{filepath:path}/info/instrument',
    response_model=InstrumentInfo,
    tags=api_tags
)
def show_instrument(
        filepath: str
):
    """
    Get image instrument info
    """
    path = filepath2path(filepath)
    check_path_existence(path)
    check_path_is_single(path)

    original = path.get_original()
    check_representation_existence(original)
    return InstrumentInfo.from_image(original)


# ASSOCIATED

class AssociatedInfoCollection(CollectionSize):
    items: AssociatedInfo


@router.get(
    '/image/{filepath:path}/info/associated',
    response_model=AssociatedInfoCollection,
    tags=api_tags + ['Associated']
)
def show_associated(
        filepath: str
):
    """
    Get associated file info
    """
    path = filepath2path(filepath)
    check_path_existence(path)
    check_path_is_single(path)

    original = path.get_original()
    check_representation_existence(original)
    return response_list(AssociatedInfo.from_image(original))


def show_associated_image(filepath, associated_key, length=None, width=None, height=None):
    # TODO
    path = filepath2path(filepath)
    check_path_existence(path)
    check_path_is_single(path)

    in_image = path.get_spatial()
    check_representation_existence(in_image)

    associated = None
    if associated_key in ('thumb', 'label', 'macro'):
        associated = getattr(in_image, 'associated_{}'.format(associated_key))

    if not associated or not associated.exists:
        raise NoAppropriateRepresentationProblem(filepath, associated_key)

    out_format, mimetype = get_output_format(request, VISUALISATION_MIMETYPES)
    req_width, req_height = get_thumb_output_dimensions(associated, height, width, length)
    safe_mode = request.headers.get('X-Image-Size-Safety', current_app.config['DEFAULT_IMAGE_SIZE_SAFETY_MODE'])
    out_width, out_height = safeguard_output_dimensions(safe_mode, current_app.config['OUTPUT_SIZE_LIMIT'],
                                                        req_width, req_height)

    thumb = AssociatedResponse(in_image, associated_key, out_width, out_height, out_format)
    fp = BytesIO(thumb.get_response_buffer())
    fp.seek(0)

    headers = dict()
    add_image_size_limit_header(headers, req_width, req_height, out_width, out_height)
    return send_file(fp, mimetype=mimetype), headers


# METADATA

class MetadataCollection(CollectionSize):
    items: List[Metadata]


@router.get(
    '/image/{filepath:path}/metadata',
    response_model=MetadataCollection,
    tags=api_tags
)
def show_metadata(
        filepath: str
):
    """
    Get image metadata
    """
    path = filepath2path(filepath)
    check_path_existence(path)
    check_path_is_single(path)

    original = path.get_original()
    check_representation_existence(original)

    store = original.raw_metadata
    return response_list([Metadata.from_metadata(md) for md in store.values()])


# REPRESENTATIONS

class RepresentationInfoCollection(CollectionSize):
    items: List[RepresentationInfo]


@router.get(
    '/image/{filepath:path}/info/representations',
    response_model=RepresentationInfoCollection,
    tags=api_tags
)
def list_representations(
        filepath: str
):
    """
    Get all image representation info
    """
    path = filepath2path(filepath)
    check_path_existence(path)
    check_path_is_single(path)
    return response_list([RepresentationInfo.from_path(rpr) for rpr in path.get_representations()])


@router.get(
    '/image/{filepath:path}/info/representations/{representation}',
    response_model=RepresentationInfo,
    tags=api_tags
)
def show_representation(
        filepath: str,
        representation: FileRole,
):
    """
    Get image representation info
    """
    path = filepath2path(filepath)
    check_path_existence(path)
    check_path_is_single(path)

    rpr = path.get_representation(representation)
    return RepresentationInfo.from_path(rpr)
