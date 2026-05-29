"""Image encoding and processing."""

from .bitplanes import encode_bitplanes, encode_gray4_bitplanes
from .compression import compress_image_data, decompress_image_data
from .images import encode_1bpp, encode_2bpp, encode_4bpp, encode_image, fit_image

__all__ = [
    "fit_image",
    "encode_image",
    "encode_1bpp",
    "encode_2bpp",
    "encode_4bpp",
    "encode_bitplanes",
    "encode_gray4_bitplanes",
    "compress_image_data",
    "decompress_image_data",
]
