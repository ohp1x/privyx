"""Detection quality on a labeled corpus: precision and recall per entity type.

Each file in ``corpus/`` is text an agent typically reads, with every value that
should be masked labeled inline as ``⟦TYPE:value⟧``.  All values are fake.  A
``·`` inside a label is dropped on load: it splits key-shaped fakes
(``ghp_·…``) so secret scanners leave the repository alone.

A detection counts only if it masks exactly a labeled value with the labeled
type, so a partial mask, an extra one, and the wrong type are all errors.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from pathlib import Path

from privyx.config.loader import load_config
from privyx.core.builder import build_detector_from
from privyx.core.context import Context

from .test_detector import _ADVERSARIAL

CORPUS = Path(__file__).parent / "corpus"
DETECTOR = build_detector_from(load_config(Path(__file__).parents[3] / "configs" / "default.yaml"))
LABEL = re.compile(r"⟦([A-Z_]+):(.*?)⟧", re.DOTALL)

#: Lowest (precision, recall) each entity type may score: its scores when the
#: corpus or the detector last changed them.  Raise a floor when a change
#: improves the type; a type not listed must score 1.0 on both.
FLOORS = {
    "API_KEY": (1.0, 0.92),
    "EMAIL": (0.79, 1.0),
    "PHONE": (0.58, 0.7),
    "SECRET": (0.47, 0.9),
    "URL_CREDENTIAL": (1.0, 0.75),
}


def _parse(raw: str) -> tuple[str, set[tuple[int, int, str]]]:
    """Strip the labels out of ``raw``: the plain text, and (start, end, type) per label."""
    text = ""
    labels = set()
    last = 0
    for match in LABEL.finditer(raw):
        text += raw[last : match.start()]
        value = match[2].replace("·", "")
        labels.add((len(text), len(text) + len(value), match[1]))
        text += value
        last = match.end()
    return text + raw[last:], labels


async def test_each_entity_type_scores_at_least_its_floor(summary: list[str]) -> None:
    tp: Counter[str] = Counter()
    fp: Counter[str] = Counter()
    fn: Counter[str] = Counter()
    errors: list[tuple[str, str]] = []
    for path in sorted(CORPUS.glob("*.txt")):
        text, labels = _parse(path.read_text(encoding="utf-8"))
        detection = (await DETECTOR.detect(text, Context())).merged(text)
        found = {(span.start, span.end, span.entity_type) for span in detection.spans}
        tp.update(entity for _, _, entity in labels & found)
        for kind, spans, counts in (("fp", found - labels, fp), ("fn", labels - found, fn)):
            for start, end, entity in sorted(spans):
                counts[entity] += 1
                errors.append((entity, f"{path.name}: {kind} {entity} {text[start:end]!r}"))

    rows = [f"{'entity':<16}{'tp':>4}{'fp':>4}{'fn':>4}{'precision':>11}{'recall':>8}"]
    below = set()
    for entity in sorted(tp.keys() | fp.keys() | fn.keys()):
        found_count, labeled = tp[entity] + fp[entity], tp[entity] + fn[entity]
        precision = round(tp[entity] / found_count, 2) if found_count else 1.0
        recall = round(tp[entity] / labeled, 2) if labeled else 1.0
        min_precision, min_recall = FLOORS.get(entity, (1.0, 1.0))
        if precision < min_precision or recall < min_recall:
            below.add(entity)
        rows.append(
            f"{entity:<16}{tp[entity]:>4}{fp[entity]:>4}{fn[entity]:>4}"
            f"{precision:>11.2f}{recall:>8.2f}"
        )
    table = "\n".join(rows)
    summary.append(table)
    details = [message for entity, message in errors if entity in below]
    assert not below, "\n".join([table, *details])


async def test_detector_stays_fast_on_adversarial_input() -> None:
    # The inputs that made the old EMAIL pattern quadratic, run through the
    # detector scored above: unlike the per-regex check in test_detector.py,
    # this also times SECRET and the detector's work per match (~50 ms each).
    slow = []
    for shape, text in _ADVERSARIAL.items():
        start = time.perf_counter()
        await DETECTOR.detect(text, Context())
        elapsed = time.perf_counter() - start
        if elapsed > 1.0:
            slow.append(f"{shape}: {elapsed:.1f} s")
    assert not slow
