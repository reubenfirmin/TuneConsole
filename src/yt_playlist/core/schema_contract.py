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
    for match in TOKEN.finditer(sql or ''):
        token = match.group()
        if token.startswith(('--', '/*')):
            continue
        if token.startswith("'"):
            result.append(token)
        elif token.startswith(('"', '`', '[')):
            result.append(token[1:-1].replace(token[0] * 2, token[0]).lower())
        else:
            result.append(token.lower())
    return tuple(result)
