"""Keep existing empty text frames that Mutagen intentionally omits on save.

Some real ID3v2.3 files carry empty COMM/TDAT frames. Their removal fails our
unchanged-metadata guard. Preserve their wire representation using the pinned
Mutagen serializer, without filtering or weakening that independent guard.
"""
from mutagen.id3._frames import TextFrame


def save(audio, version, **kwargs):
    tags = audio.tags
    write = tags._write
    previous = tags.__dict__.get('_write')
    had_override = '_write' in tags.__dict__

    def preserving_write(config):
        data = write(config)
        for frame in tags.values():
            if not isinstance(frame, TextFrame) or str(frame):
                continue
            body = frame._writeData(config)
            size = len(body)
            if config.v2_version == 4:
                if size >= 1 << 28:
                    raise ValueError('ID3 frame exceeds synchsafe size')
                encoded_size = bytes((size >> shift) & 127 for shift in (21, 14, 7, 0))
            else:
                encoded_size = size.to_bytes(4, 'big')
            data += frame.FrameID.encode('ascii') + encoded_size + b'\x00\x00' + body
        return data

    # This hook belongs to this short-lived tag object, never Mutagen's class.
    tags._write = preserving_write
    try:
        audio.save(v2_version=version, v23_sep=None, **kwargs)
    finally:
        if had_override:
            tags._write = previous
        else:
            del tags._write
