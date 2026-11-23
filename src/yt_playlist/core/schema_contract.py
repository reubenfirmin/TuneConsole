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


def table_clauses(tokens):
    """Split column and table constraints while keeping expressions intact."""
    start = tokens.index('(') + 1
    depth = 0
    for position in range(start, len(tokens)):
        token = tokens[position]
        if depth == 0 and token in (',', ')'):
            yield tokens[start:position]
            start = position + 1
            if token == ')':
                return
        depth += (token == '(') - (token == ')')
