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
import re
from collections import OrderedDict
from enum import Enum
from functools import cached_property

from fastapi.params import Path as PathParam

from pims.api.exceptions import NoAcceptableResponseMimetypeProblem


class OutputExtension(str, Enum):
    NONE = ""
    JPEG = ".jpg"
    PNG = ".png"
    WEBP = ".webp"


mimetype_from_extension = {
    OutputExtension.JPEG: "image/jpeg",
    OutputExtension.PNG: "image/png",
    OutputExtension.WEBP: "image/webp"
}

PNG_MIMETYPES = {
    "image/png": OutputExtension.PNG,
    "image/apng": OutputExtension.PNG
}
WEBP_MIMETYPES = {
    "image/webp": OutputExtension.WEBP
}
JPEG_MIMETYPES = {
    "image/jpg": OutputExtension.JPEG,
    "image/jpeg": OutputExtension.JPEG
}


def build_mimetype_dict(*mimetype_dicts):
    """Build an ordered dict from a list of dicts.
    Order in these sub-dictionaries is not guaranteed.
    """
    ordered_mimetypes = OrderedDict()
    for mimetype_dict in mimetype_dicts:
        ordered_mimetypes.update(mimetype_dict)
    return ordered_mimetypes


VISUALISATION_MIMETYPES = build_mimetype_dict(WEBP_MIMETYPES, JPEG_MIMETYPES, PNG_MIMETYPES)
PROCESSING_MIMETYPES = build_mimetype_dict(PNG_MIMETYPES, JPEG_MIMETYPES, WEBP_MIMETYPES)

# Matches 'text' or 'application'
major_type_str = r'[a-zA-Z0-9._-]+'

# Matches 'html', or 'xml+gml'
minor_type_str = r'[a-zA-Z0-9._+-]+'

# Matches either '*', 'image/*', or 'image/png'
valid_mime_type = re.compile(
    r'^(?:\*|{major_type}/\*|{major_type}/{minor_type})$'.format(
        major_type=major_type_str, minor_type=minor_type_str
    )
)

# Matches the 'q=1.23' from the parameters of a Accept mime types
q_match = re.compile(r'(?:^|;)\s*q=([0-9.-]+)(?:$|;)')


class AcceptableType:
    def __init__(self, raw: str):
        tokens = raw.split(';')
        head, tail = tokens[0], tokens[1] if len(tokens) > 1 else ""

        self.mimetype = self._parse_mimetype(head)
        self.weight = self._parse_weight(tail)

    @staticmethod
    def _parse_mimetype(mimetype):
        if not valid_mime_type.match(mimetype):
            raise ValueError(f"{mimetype} is not a valid mime type")
        return mimetype

    @staticmethod
    def _parse_weight(weight):
        q = re.search(q_match, weight)
        if q:
            try:
                return float(q.group(1))
            except ValueError:
                pass
        return 1

    def __eq__(self, other):
        return isinstance(other, AcceptableType) and \
               (self.mimetype, self.weight) == (other.mimetype, other.weight)

    def __lt__(self, other):
        if not isinstance(other, AcceptableType):
            return NotImplemented
        return self.weight < other.weight

    @cached_property
    def pattern(self):
        # *: Simple match all case
        if self.mimetype == '*':
            return valid_mime_type
        # image/*: Match the major type
        if self.mimetype.endswith('*'):
            return re.compile('^' + re.escape(self.mimetype[:-1]) + minor_type_str + '$')
        # All other cases, match the exact mime type string
        return re.compile('^' + re.escape(self.mimetype) + '$')

    def matches(self, mimetype):
        return self.pattern.match(mimetype)


def parse_accept_header(header):
    """
    Parse an ``Accept`` header into a sorted list of acceptable types
    """
    raw_mime_types = header.split(',')
    mime_types = []
    for raw_mime_type in raw_mime_types:
        try:
            mime_types.append(AcceptableType(raw_mime_type.strip()))
        except ValueError:
            pass

    return sorted(mime_types, reverse=True)


def get_best_mimetype(header, available_types):
    """
    Find the best mime type to respond to a request with,
    from an ``Accept`` header and list of response mime types
    the application supports.
    """
    acceptable_types = parse_accept_header(header)

    for available_type in available_types:
        for acceptable_type in acceptable_types:
            if acceptable_type.matches(available_type):
                return available_type

    return None


def get_output_format(extension, accept_header, supported):
    """
    Get the best output/response format and mime type according to
    the request and the ordered dictionary of supported mime types.

    Parameters
    ----------
    extension : OutputExtension
    accept_header : str
    supported : OrderedDict
        Ordered dictionary of supported mime types.

    Returns
    -------
    output_format : str
        PIMS slug for the best match
    output_mimetype : str
        Mime type associated to the output format

    Raises
    ------
    NoAcceptableResponseMimetypeProblem
        If there is no acceptable mime type.
    """
    if extension and extension in mimetype_from_extension:
        response_mimetype = mimetype_from_extension.get(extension)
    else:
        response_mimetype = get_best_mimetype(accept_header, supported.keys())

    output_format = supported.get(response_mimetype)
    if output_format:
        return output_format, response_mimetype

    raise NoAcceptableResponseMimetypeProblem(str(), list(supported.keys()))


def extension_path_parameter(
    extension: OutputExtension = PathParam(
        OutputExtension.NONE,
        description="Image response format. If not set, `Accept` header is used."
    )
):
    return extension
