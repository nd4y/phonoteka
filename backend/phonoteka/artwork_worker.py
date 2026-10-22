"""Isolated, bounded JPEG/PNG decoder. Reads one JSON request from stdin."""
import base64
import hashlib
import io
import json
import sys
import time

from PIL import Image, ImageOps

MAX_SOURCE_BYTES = 20 * 1024 * 1024
MAX_PIXELS = 25_000_000


def transform(raw, options=None):
    if not isinstance(raw, bytes) or len(raw) > MAX_SOURCE_BYTES: raise ValueError('Source image exceeds 20 MiB')
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    with Image.open(io.BytesIO(raw), formats=['JPEG', 'PNG']) as probe:
        if probe.format not in {'JPEG', 'PNG'} or probe.width * probe.height > MAX_PIXELS:
            raise ValueError('Only JPEG/PNG with at most 25 megapixels is supported')
        probe.verify()
    with Image.open(io.BytesIO(raw), formats=['JPEG', 'PNG']) as image:
        if image.format not in {'JPEG', 'PNG'} or image.width * image.height > MAX_PIXELS:
            raise ValueError('Only JPEG/PNG with at most 25 megapixels is supported')
        if getattr(image, 'is_animated', False): raise ValueError('Animated artwork requires an explicit still-image conversion')
        source = {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw), 'width': image.width, 'height': image.height,
                  'mime': 'image/jpeg' if image.format == 'JPEG' else 'image/png'}
        image.load()  # Actual bounded decoding, not only a MIME/header check.
        if options is None: return {'source': source}
        edge, budget, quality = options['max_edge'], options['max_bytes'], options['quality']
        if not 64 <= edge <= 5000 or not 32768 <= budget <= 10 * 1024 * 1024 or not 40 <= quality <= 95 or options['format'] not in {'jpeg', 'png'}:
            raise ValueError('Invalid artwork transform profile')
        ImageOps.exif_transpose(image, in_place=True)
        image.thumbnail((edge, edge), Image.Resampling.LANCZOS)
        if options['format'] == 'jpeg':
            if image.mode in {'RGBA', 'LA'} or image.mode == 'P' and 'transparency' in image.info:
                rgba = image.convert('RGBA'); converted = Image.new('RGB', rgba.size, 'white'); converted.paste(rgba, mask=rgba.getchannel('A')); rgba.close()
            else: converted = image.convert('RGB')
        else: converted = image.convert('RGBA' if image.mode in {'RGBA', 'LA'} or 'transparency' in image.info else 'RGB')
        converted.info.clear()
        try:
            for _ in range(20):
                output = io.BytesIO()
                converted.save(output, format='JPEG' if options['format'] == 'jpeg' else 'PNG', **({'quality': quality} if options['format'] == 'jpeg' else {}))
                encoded = output.getvalue()
                if len(encoded) <= budget: break
                if options['format'] == 'jpeg' and quality > 40: quality = max(40, quality - 10)
                else:
                    smaller = (max(1, int(converted.width * .8)), max(1, int(converted.height * .8)))
                    converted.thumbnail(smaller, Image.Resampling.LANCZOS)
            else: raise ValueError('Artwork cannot fit the selected byte budget')
            return {'source': source, 'result': {'sha256': hashlib.sha256(encoded).hexdigest(), 'bytes': len(encoded),
                    'width': converted.width, 'height': converted.height, 'mime': 'image/jpeg' if options['format'] == 'jpeg' else 'image/png'},
                    'profile': {**options, 'effective_quality': quality if options['format'] == 'jpeg' else None, 'crop': False, 'upscale': False},
                    'image_b64': base64.b64encode(encoded).decode()}
        finally: converted.close()


def main():
    if sys.platform == 'linux':
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024**2, 512 * 1024**2))
        resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
    started = time.monotonic()
    try:
        request = json.loads(sys.stdin.buffer.read(29 * 1024 * 1024))
        result = transform(base64.b64decode(request['image_b64'], validate=True), request.get('options'))
        result['elapsed_ms'] = round((time.monotonic() - started) * 1000)
    except Exception:
        result = {'error': 'invalid_cover', 'message': 'Image decoding or transformation failed within the configured bounds'}
    sys.stdout.write(json.dumps(result, separators=(',', ':')))


if __name__ == '__main__': main()
