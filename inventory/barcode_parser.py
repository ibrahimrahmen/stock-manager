"""
Two barcode types:

  BORDEREAU  — exactly 12 digits from shipping company
               e.g. 123456789012

  PRODUCT    — custom format: [PRODUCT_CODE]-[COLOR]-[SIZE]-[NUMBER]
               e.g. RLF-RED-40-001
               Size is free text: S, M, XL, 40, 41, 1, 2, UNIQUE, etc.
"""

import re
from dataclasses import dataclass
from typing import Optional


BORDEREAU_PATTERN = re.compile(r'^\d{12}$')

PRODUCT_PATTERN = re.compile(
    r'^([A-Z0-9]{2,10})-([A-Z0-9]{2,10})-([A-Z0-9]{1,10})-(\d{2,6})$'
)


@dataclass
class ParsedBarcode:
    product_code: str
    color_name: str
    size: str
    number: str
    raw: str


def is_bordereau_barcode(barcode: str) -> bool:
    """12 digits = shipping company bordereau."""
    return bool(BORDEREAU_PATTERN.match(barcode.strip()))


def is_product_barcode(barcode: str) -> bool:
    return not is_bordereau_barcode(barcode) and parse_barcode(barcode) is not None


def parse_barcode(barcode: str) -> Optional[ParsedBarcode]:
    """
    Parse a product barcode. Returns None if it doesn't match the product format.
    """
    barcode = barcode.strip().upper()
    if is_bordereau_barcode(barcode):
        return None
    match = PRODUCT_PATTERN.match(barcode)
    if not match:
        return None
    product_code, color_name, size, number = match.groups()
    return ParsedBarcode(
        product_code=product_code,
        color_name=color_name,
        size=size,
        number=number,
        raw=barcode,
    )
