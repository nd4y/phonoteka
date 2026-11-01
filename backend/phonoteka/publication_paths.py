"""Portable, lossless Ready path checks. Never silently rename a user's file."""
import re
import unicodedata

from . import media


class Index(dict):
    """Files plus their directory prefixes; O(path depth), not O(files squared)."""
    def __init__(self):
        super().__init__()
        self.directories = {}


def key(relative):
    if not isinstance(relative, str) or not relative or relative.startswith('/') or '\\' in relative:
        raise media.MediaError('publication_path_conflict', 'Недопустимый относительный путь публикации')
    parts = relative.split('/')
    for part in parts:
        if (part in {'', '.', '..'} or part.rstrip(' .') != part or
                any(ord(char) < 32 or char in '<>:"|?*' for char in part) or
                re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part)):
            raise media.MediaError('publication_path_conflict', 'Путь несовместим с переносом на устройство: ' + relative)
    return '/'.join(unicodedata.normalize('NFC', part).casefold() for part in parts)


def add(paths, relative):
    normalized = key(relative)
    if isinstance(paths, Index):
        parts, original = normalized.split('/'), relative.split('/')
        if normalized in paths or normalized in paths.directories:
            raise media.MediaError('publication_path_conflict', 'Коллизия путей: ' + relative)
        for index in range(1, len(parts)):
            parent, source = '/'.join(parts[:index]), '/'.join(original[:index])
            if parent in paths or parent in paths.directories and paths.directories[parent] != source:
                raise media.MediaError('publication_path_conflict', 'Конфликт файла/каталога или Unicode/регистра: ' + relative)
        paths[normalized] = relative
        for index in range(1, len(parts)):
            paths.directories['/'.join(parts[:index])] = '/'.join(original[:index])
        return normalized
    for existing, original in paths.items():
        if normalized == existing or normalized.startswith(existing + '/') or existing.startswith(normalized + '/'):
            raise media.MediaError('publication_path_conflict', 'Коллизия путей: ' + original + ' / ' + relative)
        incoming_parts, existing_parts = normalized.split('/'), existing.split('/')
        for index in range(1, min(len(incoming_parts), len(existing_parts))):
            if incoming_parts[:index] == existing_parts[:index] and relative.split('/')[:index] != original.split('/')[:index]:
                raise media.MediaError('publication_path_conflict', 'Разные Unicode/регистр общего каталога: ' + original + ' / ' + relative)
    paths[normalized] = relative
    return normalized
