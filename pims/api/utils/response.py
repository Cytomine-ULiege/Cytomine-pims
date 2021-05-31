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
import logging

from pint import Quantity

log = logging.getLogger("pims.api.utils")


def response_list(list_):
    """Format a list for response serialization.
    """
    return {
        "items": list_,
        "size": len(list_)
    }


def convert_quantity(quantity, unit, ndigits=6):
    """
    Convert a quantity to the unit required by API specification.

    Parameters
    ----------
    quantity : Quantity or None or any
        Quantity to convert
    unit : str
        Pint understandable unit
    ndigits : int
        Number of digits to keep for rounding

    Returns
    -------
    float
        Converted quantity to given unit if `quantity` is Quantity
    """
    if quantity is None:
        return None
    elif isinstance(quantity, Quantity):
        return round(quantity.to(unit).magnitude, ndigits)

    log.warning('The quantity {} is not of type Quantity and is thus not converted.'.format(quantity))
    return round(quantity, ndigits)
