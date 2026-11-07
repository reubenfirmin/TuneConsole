"""Shared SQL comparison and errors, independent of schema upgrade execution."""
from functools import lru_cache
import re


class MigrationError(RuntimeError):
    pass


TOKEN = re.compile(r"--[^\n]*|/\*.*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|"
                   r"`(?:``|[^`])*`|\[[^]]*\]|[A-Za-z_]\w*|\S", re.S)


@lru_cache(maxsize=4096)
def tokens(sql):
    result = []
    for token in TOKEN.findall(sql or ''):
        first = token[0]
        if first in '-/' and token[:2] in ('--', '/*'):
            continue
        if first == "'":
            result.append(token)
        elif first in '"`[':
            result.append(token[1:-1].replace(first * 2, first).lower())
        else:
            result.append(token.lower())
    return tuple(result)
