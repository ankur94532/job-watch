"""A small YAML-subset parser, so the runtime stays standard-library only.

Supports what config.yaml uses: nested mappings by indentation, `#` comments,
inline lists (may span lines), block lists of scalars, `{}` / `[]`, quoted and
bare scalars, ints, true/false/null. Anything else raises ConfigError with a
line number rather than being silently misread.
"""

import re


class ConfigError(ValueError):
    pass


def _strip_comment(line):
    out, quote = [], None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out).rstrip()


def _scalar(s, lineno):
    s = s.strip()
    if not s:
        return None
    if s[0] in "\"'":
        if len(s) < 2 or s[-1] != s[0]:
            raise ConfigError(f"line {lineno}: unterminated string {s!r}")
        body = s[1:-1]
        return body.replace('\\"', '"') if s[0] == '"' else body.replace("''", "'")
    if s == "{}":
        return {}
    if s == "[]":
        return []
    if s.startswith("["):
        return _inline_list(s, lineno)
    if s.startswith(("{", "&", "*", "!", "|", ">")):
        raise ConfigError(f"line {lineno}: unsupported YAML syntax {s!r}")
    low = s.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "~"):
        return None
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d+\.\d+", s):
        return float(s)
    return s


def _inline_list(s, lineno):
    if not s.endswith("]"):
        raise ConfigError(f"line {lineno}: unterminated list")
    items, cur, quote = [], [], None
    for ch in s[1:-1]:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            cur.append(ch)
        elif ch == ",":
            items.append("".join(cur))
            cur = []
        elif ch in "[]{}":
            raise ConfigError(f"line {lineno}: nested collections are not supported")
        else:
            cur.append(ch)
    items.append("".join(cur))
    return [_scalar(i, lineno) for i in items if i.strip()]


def _logical_lines(text):
    """(lineno, indent, content) with comments stripped and inline lists joined."""
    pending = None
    for n, raw in enumerate(text.splitlines(), 1):
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise ConfigError(f"line {n}: tabs are not allowed for indentation")
        line = _strip_comment(raw)
        if pending is not None:
            pending = (pending[0], pending[1], pending[2] + " " + line.strip())
            if pending[2].count("[") <= pending[2].count("]"):
                yield pending
                pending = None
            continue
        if not line.strip():
            continue
        entry = (n, len(line) - len(line.lstrip()), line.strip())
        if entry[2].count("[") > entry[2].count("]"):
            pending = entry
        else:
            yield entry
    if pending is not None:
        raise ConfigError(f"line {pending[0]}: unterminated list")


def loads(text):
    lines = list(_logical_lines(text))
    if not lines:
        return {}
    value, i = _block(lines, 0, lines[0][1])
    if i != len(lines):
        raise ConfigError(f"line {lines[i][0]}: unexpected indentation")
    return value


def _block(lines, i, indent):
    if lines[i][2].startswith("- ") or lines[i][2] == "-":
        out = []
        while i < len(lines) and lines[i][1] == indent and lines[i][2].startswith("-"):
            n, _, content = lines[i]
            item = content[1:].strip()
            if re.match(r"^[^\"'\[]*:\s", item + " ") and not item.startswith(("'", '"')):
                raise ConfigError(f"line {n}: lists of mappings are not supported")
            out.append(_scalar(item, n))
            i += 1
        return out, i

    out = {}
    while i < len(lines) and lines[i][1] == indent:
        n, _, content = lines[i]
        m = re.match(r"""^("[^"]*"|'[^']*'|[^:]+?)\s*:(?:\s+(.*))?$""", content)
        if not m:
            raise ConfigError(f"line {n}: expected 'key: value', got {content!r}")
        key = _scalar(m.group(1), n)
        key = str(key) if key is not None else ""
        rest = m.group(2)
        i += 1
        if rest is not None and rest.strip():
            out[key] = _scalar(rest, n)
        elif i < len(lines) and lines[i][1] > indent:
            out[key], i = _block(lines, i, lines[i][1])
        else:
            out[key] = None
    if i < len(lines) and lines[i][1] > indent:
        raise ConfigError(f"line {lines[i][0]}: unexpected indentation")
    return out, i
