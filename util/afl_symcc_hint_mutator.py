"""AFL++ Python custom mutator consuming SymCC hints and poly-cache entries.

The module is intentionally dependency-free because afl-fuzz imports it inside
the fuzzer process.  It watches SYMCC_HINT_DIR for changed-byte dictionary
tokens and SYMCC_POLY_CACHE_MUTATOR/SYMCC_POLY_CACHE for cached model/box
entries, then applies them as a lightweight mutation stage before AFL's havoc.
"""

from __future__ import annotations

import os
import json
import random
import time


_rng = random.Random(0)
_hint_dir = ""
_poly_cache = ""
_string_constraints = ""
_last_scan = 0.0
_scan_interval = 1.0
_tokens: list[bytes] = []
_models: list[tuple[tuple[int, int], ...]] = []
_boxes: list[tuple[tuple[int, int, int], ...]] = []
_strings: list[tuple[tuple[int, int], ...]] = []
_polytopes: list[
    tuple[
        tuple[tuple[int, int], ...],
        tuple[tuple[int, int, int], ...],
        tuple[tuple[int, int, tuple[tuple[int, int], ...]], ...],
    ]
] = []
_calls = 0
_description = "symcc_hint"


def _bounded_int(raw: str | None, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(raw))) if raw is not None else default
    except ValueError:
        return default


def _scan_tokens() -> list[bytes]:
    if not _hint_dir or not os.path.isdir(_hint_dir):
        return []
    limit = _bounded_int(os.environ.get("SYMCC_HINT_MUTATOR_TOKENS"), 4096, 1, 65536)
    max_len = _bounded_int(os.environ.get("SYMCC_HINT_MUTATOR_TOKEN_MAX"), 64, 1, 4096)
    tokens: list[bytes] = []
    try:
        names = sorted(os.listdir(_hint_dir))[-limit:]
    except OSError:
        return []
    for name in names:
        if name.startswith("."):
            continue
        path = os.path.join(_hint_dir, name)
        try:
            with open(path, "rb") as stream:
                token = stream.read(max_len + 1)
        except OSError:
            continue
        if 0 < len(token) <= max_len:
            tokens.append(token)
    return tokens


def _parse_model(raw: str) -> tuple[tuple[int, int], ...]:
    if not raw or raw == "-":
        return ()
    out: list[tuple[int, int]] = []
    for item in raw.split(","):
        left, sep, right = item.partition(":")
        if not sep:
            continue
        try:
            offset = int(left)
            value = int(right)
        except ValueError:
            continue
        if offset >= 0 and 0 <= value <= 255:
            out.append((offset, value))
    return tuple(out)


def _parse_box(raw: str) -> tuple[tuple[int, int, int], ...]:
    if not raw or raw == "-":
        return ()
    out: list[tuple[int, int, int]] = []
    for item in raw.split(","):
        parts = item.split(":")
        if len(parts) != 3:
            continue
        try:
            offset, lo, hi = (int(value) for value in parts)
        except ValueError:
            continue
        if offset >= 0 and 0 <= lo <= hi <= 255:
            out.append((offset, lo, hi))
    return tuple(out)


def _parse_constraints(
    raw: str,
) -> tuple[tuple[int, int, tuple[tuple[int, int], ...]], ...]:
    if not raw or raw == "-":
        return ()
    constraints = []
    for item in raw.split(";"):
        lower_text, separator, rest = item.partition(":")
        upper_text, separator2, terms_text = rest.partition(":")
        if not separator or not separator2:
            continue
        try:
            lower = int(lower_text)
            upper = int(upper_text)
        except ValueError:
            continue
        if lower > upper:
            continue
        terms = []
        valid = True
        for term_text in terms_text.split(","):
            offset_text, equals, coefficient_text = term_text.partition("=")
            if not equals:
                valid = False
                break
            try:
                offset = int(offset_text)
                coefficient = int(coefficient_text)
            except ValueError:
                valid = False
                break
            if offset < 0 or coefficient == 0:
                valid = False
                break
            terms.append((offset, coefficient))
        if valid and terms:
            constraints.append((lower, upper, tuple(terms)))
    return tuple(constraints)


def _scan_poly_cache() -> tuple[
    list[tuple[tuple[int, int], ...]],
    list[tuple[tuple[int, int, int], ...]],
    list[
        tuple[
            tuple[tuple[int, int], ...],
            tuple[tuple[int, int, int], ...],
            tuple[tuple[int, int, tuple[tuple[int, int], ...]], ...],
        ]
    ],
]:
    if not _poly_cache or not os.path.isfile(_poly_cache):
        return [], [], []
    max_lines = _bounded_int(os.environ.get("SYMCC_HINT_MUTATOR_CACHE_LINES"), 4096, 1, 65536)
    try:
        with open(_poly_cache, encoding="utf-8", errors="ignore") as stream:
            lines = stream.readlines()[-max_lines:]
    except OSError:
        return [], [], []
    models: list[tuple[tuple[int, int], ...]] = []
    boxes: list[tuple[tuple[int, int, int], ...]] = []
    polytopes = []
    for line in lines:
        fields = line.strip().split()
        if len(fields) < 5 or fields[1] != "sat":
            continue
        model = _parse_model(fields[3])
        box = _parse_box(fields[4])
        constraints = _parse_constraints(fields[5] if len(fields) >= 6 else "-")
        if constraints and box:
            polytopes.append((model, box, constraints))
        else:
            if model:
                models.append(model)
            if box:
                boxes.append(box)
    return models, boxes, polytopes


def _parse_string_constraint(line: str) -> tuple[tuple[int, int], ...]:
    try:
        record = json.loads(line)
    except (TypeError, ValueError):
        return ()
    if not isinstance(record, dict):
        return ()
    if record.get("schema") != "symcc-string-constraint-v1":
        return ()
    patches = record.get("patches", ())
    if not isinstance(patches, list) or len(patches) > 4096:
        return ()
    out: list[tuple[int, int]] = []
    seen_offsets: set[int] = set()
    for patch in patches:
        if not isinstance(patch, dict):
            return ()
        try:
            offset = int(patch.get("offset"))
            value = int(patch.get("value"))
        except (TypeError, ValueError):
            return ()
        if offset < 0 or offset in seen_offsets or not 0 <= value <= 255:
            return ()
        seen_offsets.add(offset)
        out.append((offset, value))
    return tuple(out)


def _scan_string_constraints() -> list[tuple[tuple[int, int], ...]]:
    if not _string_constraints or not os.path.isfile(_string_constraints):
        return []
    max_lines = _bounded_int(
        os.environ.get("SYMCC_HINT_MUTATOR_STRING_LINES"), 4096, 1, 65536)
    try:
        with open(_string_constraints, encoding="ascii", errors="ignore") as stream:
            lines = stream.readlines()[-max_lines:]
    except OSError:
        return []
    constraints = []
    for line in lines:
        patches = _parse_string_constraint(line)
        if patches:
            constraints.append(patches)
    return constraints


def _refresh(force: bool = False) -> None:
    global _last_scan, _tokens, _models, _boxes, _polytopes, _strings
    now = time.monotonic()
    if not force and now - _last_scan < _scan_interval:
        return
    _last_scan = now
    _tokens = _scan_tokens()
    _models, _boxes, _polytopes = _scan_poly_cache()
    _strings = _scan_string_constraints()


def init(seed: int) -> None:
    global _hint_dir, _poly_cache, _string_constraints, _scan_interval
    _rng.seed(seed)
    _hint_dir = os.environ.get("SYMCC_HINT_DIR", "")
    _poly_cache = (
        os.environ.get("SYMCC_POLY_CACHE_MUTATOR")
        or os.environ.get("SYMCC_POLY_CACHE", "")
    )
    _string_constraints = (
        os.environ.get("SYMCC_STRING_CONSTRAINTS")
        or os.environ.get("SYMCC_STRING_CONSTRAINT_OUT", "")
    )
    try:
        _scan_interval = max(
            0.05, float(os.environ.get("SYMCC_HINT_MUTATOR_RESCAN", "1.0")))
    except ValueError:
        _scan_interval = 1.0
    _refresh(force=True)


def deinit() -> None:
    return None


def fuzz_count(buf: bytearray) -> int:
    _refresh()
    if _tokens or _models or _boxes or _polytopes or _strings:
        return _bounded_int(os.environ.get("SYMCC_HINT_MUTATOR_COUNT"), 4, 1, 32)
    return 1


def _apply_model(out: bytearray) -> bool:
    if not _models:
        return False
    model = _rng.choice(_models)
    changed = False
    for offset, value in model:
        if offset < len(out) and out[offset] != value:
            out[offset] = value
            changed = True
    return changed


def _apply_box(out: bytearray) -> bool:
    if not _boxes:
        return False
    box = _rng.choice(_boxes)
    changed = False
    for offset, lo, hi in box:
        if offset < len(out):
            value = _rng.randint(lo, hi)
            if out[offset] != value:
                out[offset] = value
                changed = True
    return changed


def _apply_string(out: bytearray) -> bool:
    if not _strings:
        return False
    patches = _rng.choice(_strings)
    changed = False
    for offset, value in patches:
        if offset < len(out) and out[offset] != value:
            out[offset] = value
            changed = True
    return changed


def _satisfies_constraints(
    values: bytearray,
    constraints: tuple[
        tuple[int, int, tuple[tuple[int, int], ...]], ...
    ],
) -> bool:
    for lower, upper, terms in constraints:
        total = 0
        for offset, coefficient in terms:
            if offset >= len(values):
                return False
            total += coefficient * values[offset]
        if total < lower or total > upper:
            return False
    return True


def _ceil_div(value: int, positive: int) -> int:
    return -((-value) // positive)


def _apply_polytope(out: bytearray) -> bool:
    if not _polytopes:
        return False
    model, box, constraints = _rng.choice(_polytopes)
    if any(offset >= len(out) for offset, _lo, _hi in box):
        return False
    base = bytearray(out)
    for offset, value in model:
        if offset < len(base):
            base[offset] = value
    if not _satisfies_constraints(base, constraints):
        # A cache model stores only bytes changed from its original witness.
        # On an unrelated AFL seed, recover a full feasible point conservatively.
        recovered = None
        attempts = _bounded_int(
            os.environ.get("SYMCC_HINT_MUTATOR_POLY_ATTEMPTS"), 128, 8, 4096)
        for _ in range(attempts):
            candidate = bytearray(base)
            for offset, lower, upper in box:
                candidate[offset] = _rng.randint(lower, upper)
            if _satisfies_constraints(candidate, constraints):
                recovered = candidate
                break
        if recovered is None:
            return False
        base = recovered

    direction = []
    for offset, lower, upper in box:
        if lower == upper:
            continue
        coefficient = _rng.choice((-2, -1, 1, 2))
        direction.append((offset, coefficient))
    if not direction:
        out[:] = base
        return True

    alpha_lower = -1024
    alpha_upper = 1024
    intervals = {offset: (lower, upper) for offset, lower, upper in box}
    for offset, coefficient in direction:
        lower, upper = intervals[offset]
        current = base[offset]
        if coefficient > 0:
            alpha_lower = max(
                alpha_lower, _ceil_div(lower - current, coefficient))
            alpha_upper = min(
                alpha_upper, (upper - current) // coefficient)
        else:
            positive = -coefficient
            alpha_lower = max(
                alpha_lower, _ceil_div(current - upper, positive))
            alpha_upper = min(
                alpha_upper, (current - lower) // positive)

    direction_map = dict(direction)
    for lower, upper, terms in constraints:
        total = sum(coefficient * base[offset]
                    for offset, coefficient in terms)
        delta = sum(
            coefficient * direction_map.get(offset, 0)
            for offset, coefficient in terms
        )
        if delta > 0:
            alpha_lower = max(alpha_lower, _ceil_div(lower - total, delta))
            alpha_upper = min(alpha_upper, (upper - total) // delta)
        elif delta < 0:
            positive = -delta
            alpha_lower = max(
                alpha_lower, _ceil_div(total - upper, positive))
            alpha_upper = min(
                alpha_upper, (total - lower) // positive)
        elif total < lower or total > upper:
            return False
    if alpha_lower > alpha_upper:
        out[:] = base
        return True
    choices = [
        alpha for alpha in range(alpha_lower, alpha_upper + 1)
        if alpha != 0
    ]
    if not choices:
        out[:] = base
        return True
    alpha = _rng.choice(choices)
    candidate = bytearray(base)
    for offset, coefficient in direction:
        candidate[offset] += alpha * coefficient
    if not _satisfies_constraints(candidate, constraints):
        out[:] = base
        return True
    out[:] = candidate
    return True


def _apply_token(out: bytearray, max_size: int) -> bool:
    if not _tokens:
        return False
    token = _rng.choice(_tokens)
    if not token:
        return False
    if not out:
        out.extend(token[:max_size])
        return bool(out)
    overwrite = _rng.random() < 0.75 or len(out) + len(token) > max_size
    if overwrite:
        offset = _rng.randrange(0, len(out))
        end = min(len(out), offset + len(token))
        before = bytes(out[offset:end])
        out[offset:end] = token[: end - offset]
        return before != bytes(out[offset:end])
    offset = _rng.randrange(0, len(out) + 1)
    room = max_size - len(out)
    if room <= 0:
        return False
    out[offset:offset] = token[:room]
    return True


def _splice(out: bytearray, add_buf: bytearray | None, max_size: int) -> bool:
    if not add_buf:
        return False
    donor = bytes(add_buf)
    if not donor:
        return False
    if not out:
        out.extend(donor[:max_size])
        return bool(out)
    src = _rng.randrange(0, len(donor))
    length = min(len(donor) - src, max(1, max_size // 8), max_size)
    dst = _rng.randrange(0, len(out))
    if _rng.random() < 0.5:
        end = min(len(out), dst + length)
        before = bytes(out[dst:end])
        out[dst:end] = donor[src: src + end - dst]
        return before != bytes(out[dst:end])
    room = max_size - len(out)
    if room <= 0:
        return False
    out[dst:dst] = donor[src: src + min(length, room)]
    return True


def _havoc(out: bytearray, max_size: int) -> None:
    if not out and max_size > 0:
        out.append(_rng.randrange(0, 256))
        return
    if out:
        offset = _rng.randrange(0, len(out))
        out[offset] ^= 1 << _rng.randrange(0, 8)


def fuzz(buf: bytearray, add_buf: bytearray | None, max_size: int) -> bytearray:
    global _calls, _description
    _refresh()
    out = bytearray(buf[:max_size])
    modes = []
    if _models:
        modes.append("model")
    if _boxes:
        modes.append("box")
    if _polytopes:
        modes.append("poly")
    if _strings:
        modes.append("string")
    if _tokens:
        modes.append("token")
    if add_buf:
        modes.append("splice")
    if not modes:
        modes.append("havoc")
    mode = modes[_calls % len(modes)]
    _calls += 1

    changed = False
    if mode == "model":
        changed = _apply_model(out)
    elif mode == "box":
        changed = _apply_box(out)
    elif mode == "poly":
        changed = _apply_polytope(out)
    elif mode == "string":
        changed = _apply_string(out)
    elif mode == "token":
        changed = _apply_token(out, max_size)
    elif mode == "splice":
        changed = _splice(out, add_buf, max_size)
    if not changed:
        _havoc(out, max_size)
        mode = "havoc"
    _description = f"symcc_{mode}"
    return out[:max_size]


def post_process(buf: bytearray) -> bytearray:
    return buf


def queue_new_entry(filename_new_queue: bytes, filename_orig_queue: bytes | None) -> None:
    _refresh(force=True)
    return None


def describe(max_description_length: int) -> str:
    return _description[:max_description_length]
