"""Construct aged content for retention tests without adding a production API."""
from dataclasses import replace

from yt_playlist.core import fao

_bytes = fao.Content.bytes.fget


def read_bytes(path, **options):
    return _bytes(fao.read(path, **options))


def patch_byte_reads(patch, callback):
    patch.setattr(fao.Content, 'bytes', property(
        lambda content: callback(content.path, max_bytes=content.max_bytes)))


def age_file(path, *, mtime_ns):
    content = fao.read(path)
    encoded = content.bytes
    fao.write(path, fao.Content(encoded, metadata=replace(content.info, mtime_ns=mtime_ns)))
