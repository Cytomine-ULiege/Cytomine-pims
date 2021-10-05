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
import itertools
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Response
from pydantic import BaseModel, Field, conint
from starlette import status

from pims.api.exceptions import BadRequestException, check_representation_existence
from pims.api.utils.image_parameter import (
    ensure_list, get_channel_indexes, get_timepoint_indexes,
    get_zslice_indexes
)
from pims.api.utils.models import CollectionSize, HistogramType
from pims.api.utils.parameter import imagepath_parameter
from pims.api.utils.response import response_list
from pims.files.file import HISTOGRAM_STEM, Path
from pims.files.histogram import argmax_nonzero, argmin_nonzero, build_histogram_file

router = APIRouter()
api_tags = ['Histograms']


class HistogramInfo(BaseModel):
    type: HistogramType = Field(...)
    minimum: int = Field(..., description="Minimum intensity value")
    maximum: int = Field(..., description="Maximum intensity value")


class Histogram(HistogramInfo):
    first_bin: int = Field(..., description="Index of first bin returned in histogram")
    last_bin: int = Field(..., description="Index of last bin returned in histogram")
    n_bins: int = Field(..., description="The number of bins in the full range histogram")
    histogram: List[int] = Field(..., description="Histogram")


class ChannelHistogramInfo(HistogramInfo):
    channel: int = Field(..., description="Image channel index")
    color: Optional[str] = Field(None, description="Channel color")


class ChannelHistogram(ChannelHistogramInfo, Histogram):
    pass


class ChannelsHistogramCollection(CollectionSize):
    items: List[ChannelHistogram] = Field(
        None, description='Array of channel histograms', title='Channel histogram'
    )


class ChannelsHistogramInfoCollection(CollectionSize):
    items: List[ChannelHistogramInfo] = Field(
        None, description='Array of channel histograms', title='Channel histogram'
    )


class PlaneHistogramInfo(ChannelHistogramInfo):
    z_slice: int = Field(..., description="Image focal point index")
    timepoint: int = Field(..., description="Image timepoint index")


class PlaneHistogram(PlaneHistogramInfo, Histogram):
    pass


class PlaneHistogramCollection(CollectionSize):
    items: List[PlaneHistogram] = Field(
        None, description='Array of plane histograms', title='Plane histogram'
    )


class PlaneHistogramInfoCollection(CollectionSize):
    items: List[PlaneHistogramInfo] = Field(
        None, description='Array of plane histograms', title='Plane histogram'
    )


def parse_n_bins(n_bins, hist_len):
    return n_bins if n_bins <= hist_len else hist_len


def _histogram_binning(hist, n_bins):
    if hist.shape[-1] % n_bins != 0:
        raise BadRequestException(
            detail=f"Cannot make {n_bins} bins from histogram "
                   f"with shape {hist.shape}"
        )
    return hist.reshape((n_bins, -1)).sum(axis=1)


def histogram_formatter(hist, bounds, n_bins, full_range):
    if n_bins == len(hist):
        bin_bounds = bounds
        if not full_range:
            hist = hist[bin_bounds[0]:bin_bounds[1] + 1]
    else:
        hist = _histogram_binning(hist, n_bins)
        bin_bounds = argmin_nonzero(hist), argmax_nonzero(hist)
        if not full_range:
            hist = hist[bin_bounds[0]:bin_bounds[1] + 1]

    first_bin, last_bin = bin_bounds
    mini, maxi = bounds
    return {
        "histogram": list(hist),
        "first_bin": first_bin,
        "last_bin": last_bin,
        "minimum": mini,
        "maximum": maxi,
        "n_bins": n_bins
    }


def is_power_of_2(n):
    return (n & (n - 1) == 0) and n != 0


class HistogramConfig:
    def __init__(
        self,
        n_bins: int = Query(
            256,
            description="Number of bins. Must be a power of 2. "
                        "If `nbins > 2 ** image.significant_bits` then "
                        "´nbins = 2 ** image.significant_bits` "
        ),
        full_range: bool = Query(
            False,
            description="Whether to return full histogram range, "
                        "including leading and ending zero bins. "
                        "When set, `first_bin = 0` and "
                        "`last_bin = 2 ** image.significant_bits - 1`."
        )
    ):
        if not is_power_of_2(n_bins):
            raise BadRequestException(detail=f"{n_bins} is not a power of 2.")

        self.n_bins = n_bins
        self.full_range = full_range


@router.get(
    '/image/{filepath:path}/histogram/per-image',
    tags=api_tags, response_model=Histogram
)
def show_image_histogram(
    path: Path = Depends(imagepath_parameter),
    hist_config: HistogramConfig = Depends()
):
    """
    Get histogram for full image where all planes (C,Z,T) are merged.
    """
    in_image = path.get_spatial()
    check_representation_existence(in_image)

    n_bins = parse_n_bins(hist_config.n_bins, len(in_image.value_range))
    htype = in_image.histogram_type()
    return Histogram(
        type=htype,
        **histogram_formatter(
            in_image.image_histogram(), in_image.image_bounds(),
            n_bins, hist_config.full_range
        )
    )


@router.get(
    '/image/{filepath:path}/histogram/per-image/bounds',
    tags=api_tags, response_model=HistogramInfo
)
def show_image_histogram_bounds(
    path: Path = Depends(imagepath_parameter)
):
    """
    Get histogram info for full image where all planes (C,Z,T) are merged.
    """
    in_image = path.get_spatial()
    check_representation_existence(in_image)

    htype = in_image.histogram_type()
    mini, maxi = in_image.image_bounds()
    return HistogramInfo(type=htype, minimum=mini, maximum=maxi)


@router.get(
    '/image/{filepath:path}/histogram/per-channels',
    tags=api_tags, response_model=ChannelsHistogramCollection
)
def show_channels_histogram(
    path: Path = Depends(imagepath_parameter),
    hist_config: HistogramConfig = Depends(),
    channels: Optional[List[conint(ge=0)]] = Query(
        None, description="Only return histograms for these channels"
    ),
):
    """
    Get histograms per channel where all planes (Z,T) are merged.
    """
    in_image = path.get_spatial()
    check_representation_existence(in_image)

    channels = ensure_list(channels)
    channels = get_channel_indexes(in_image, channels)

    histograms = []
    n_bins = parse_n_bins(hist_config.n_bins, len(in_image.value_range))
    htype = in_image.histogram_type()
    for channel in channels:
        histograms.append(
            ChannelHistogram(
                channel=channel, type=htype,
                color=in_image.channels[channel].hex_color,
                **histogram_formatter(
                    in_image.channel_histogram(channel),
                    in_image.channel_bounds(channel),
                    n_bins, hist_config.full_range
                )
            )
        )

    return response_list(histograms)


@router.get(
    '/image/{filepath:path}/histogram/per-channels/bounds',
    tags=api_tags, response_model=ChannelsHistogramInfoCollection
)
def show_channels_histogram_bounds(
    path: Path = Depends(imagepath_parameter),
    channels: Optional[List[conint(ge=0)]] = Query(
        None, description="Only return histograms for these channels"
    ),
):
    """
    Get histogram bounds per channel where all planes (Z,T) are merged.
    """
    in_image = path.get_spatial()
    check_representation_existence(in_image)

    channels = ensure_list(channels)
    channels = get_channel_indexes(in_image, channels)

    hist_info = []
    htype = in_image.histogram_type()
    for channel in channels:
        mini, maxi = in_image.channel_bounds(channel)
        hist_info.append(
            ChannelHistogramInfo(
                channel=channel, type=htype,
                color=in_image.channels[channel].hex_color,
                minimum=mini, maximum=maxi
            )
        )

    return response_list(hist_info)


@router.get(
    '/image/{filepath:path}/histogram/per-plane/z/{z_slices}/t/{timepoints}',
    tags=api_tags, response_model=PlaneHistogramCollection
)
def show_plane_histogram(
    z_slices: conint(ge=0),
    timepoints: conint(ge=0),
    path: Path = Depends(imagepath_parameter),
    hist_config: HistogramConfig = Depends(),
    channels: Optional[List[conint(ge=0)]] = Query(
        None, description="Only return histograms for these channels"
    ),
):
    """
    Get histogram per plane.
    """
    in_image = path.get_spatial()
    check_representation_existence(in_image)

    channels = ensure_list(channels)
    z_slices = ensure_list(z_slices)
    timepoints = ensure_list(timepoints)

    channels = get_channel_indexes(in_image, channels)
    z_slices = get_zslice_indexes(in_image, z_slices)
    timepoints = get_timepoint_indexes(in_image, timepoints)

    histograms = []
    n_bins = parse_n_bins(hist_config.n_bins, len(in_image.value_range))
    htype = in_image.histogram_type()
    for c, z, t in itertools.product(channels, z_slices, timepoints):
        histograms.append(
            PlaneHistogram(
                channel=c, z_slice=z, timepoint=t, type=htype,
                color=in_image.channels[c].hex_color,
                **histogram_formatter(
                    in_image.plane_histogram(c, z, t),
                    in_image.plane_bounds(c, z, t),
                    n_bins, hist_config.full_range
                )
            )
        )

    return response_list(histograms)


@router.get(
    '/image/{filepath:path}/histogram/per-plane/z/{z_slices}/t/{timepoints}/bounds',
    tags=api_tags, response_model=PlaneHistogramInfoCollection
)
def show_plane_histogram(
    z_slices: conint(ge=0),
    timepoints: conint(ge=0),
    path: Path = Depends(imagepath_parameter),
    channels: Optional[List[conint(ge=0)]] = Query(
        None, description="Only return histograms for these channels"
    ),
):
    """
    Get histogram per plane.
    """
    in_image = path.get_spatial()
    check_representation_existence(in_image)

    channels = ensure_list(channels)
    z_slices = ensure_list(z_slices)
    timepoints = ensure_list(timepoints)

    channels = get_channel_indexes(in_image, channels)
    z_slices = get_zslice_indexes(in_image, z_slices)
    timepoints = get_timepoint_indexes(in_image, timepoints)

    hist_info = []
    htype = in_image.histogram_type()
    for c, z, t in itertools.product(channels, z_slices, timepoints):
        mini, maxi = in_image.plane_bounds(c, z, t)
        hist_info.append(
            PlaneHistogramInfo(
                channel=c, z_slice=z, timepoint=t, type=htype,
                color=in_image.channels[c].hex_color,
                minimum=mini, maximum=maxi
            )
        )

    return response_list(hist_info)


@router.post('/image/{filepath:path}/histogram', tags=api_tags)
def compute_histogram(
    response: Response,
    background: BackgroundTasks,
    path: Path = Depends(imagepath_parameter),
    # companion_file_id: Optional[int] = Body(None, description="Cytomine ID for the histogram")
    sync: bool = True,
    overwrite: bool = True
):
    """
    Ask for histogram computation
    """
    in_image = path.get_spatial()
    check_representation_existence(in_image)

    hist_type = HistogramType.FAST  # TODO: allow to build complete histograms
    hist_path = in_image.processed_root() / Path(HISTOGRAM_STEM)

    if sync:
        build_histogram_file(in_image, hist_path, hist_type, overwrite)
        response.status_code = status.HTTP_201_CREATED
    else:
        background.add_task(build_histogram_file, in_image, hist_path, hist_type, overwrite)
        response.status_code = status.HTTP_202_ACCEPTED
