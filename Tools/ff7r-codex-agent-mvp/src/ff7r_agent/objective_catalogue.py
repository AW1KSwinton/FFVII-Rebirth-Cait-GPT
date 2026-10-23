"""Static story/marker joins. Row existence never proves current progress."""
from __future__ import annotations

from typing import Any

import csv
import hashlib
import io
import os
from pathlib import Path

from .navigation_values import MissionError

DATA_ROOT = Path(r'C:\CaitGPT\DataTableIndex\Input')


def read_rows(path: Path) -> list[dict]:
    if path.stat().st_size > 8 * 1024 * 1024:
        raise MissionError('objective_table_too_large')
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    reader = csv.reader(io.StringIO(data.decode('utf-8-sig')))
    headers = next(reader)
    if len(headers) > 4096 or 'Name' not in headers:
        raise MissionError('objective_table_schema_invalid')
    # The exporter repeats numeric column names for each flattened array.
    counts, columns = {}, []
    for header in headers:
        counts[header] = counts.get(header, 0) + 1
        columns.append(header if counts[header] == 1 else f'{header}__{counts[header]}')
    rows = []
    while True:
        line = reader.line_num + 1
        try:
            cells = next(reader)
        except StopIteration:
            break
        if len(cells) != len(headers) or len(rows) >= 20000:
            raise MissionError('objective_table_rows_invalid')
        values, arrays, group = {}, {}, None
        for header, column, value in zip(headers, columns, cells):
            if header.endswith('_Array'):
                group = header
                arrays[group] = []
                if value and '|' not in value:
                    raise MissionError('objective_array_encoding_unsupported')
            elif header.isdigit():
                if group is None:
                    raise MissionError('objective_array_without_owner')
                if value:
                    arrays[group].append({'position': int(header), 'source_column': column, 'value': value})
            else:
                group = None
                values[header] = value
        rows.append({'values': values, 'arrays': arrays,
                     'source': {'path': str(path), 'source_row_number': line,
                                'row_index': values.get('Index'), 'sha256': sha}})
    return rows


def number(row: dict, name: str) -> int:
    try:
        return int(row['values'][name])
    except (KeyError, TypeError, ValueError) as exc:
        raise MissionError('objective_numeric_field_invalid:' + name) from exc


def load_catalogue(root: Path | None = None) -> dict:
    root = Path(root or os.environ.get('FF7R_OBJECTIVE_DATA_ROOT') or DATA_ROOT)
    try:
        tables = {name: read_rows(root / 'Resident' / (name + '.csv'))
                  for name in ('StoryResident', 'StoryFlag', 'Chapter', 'MapIconInfo')}
    except (OSError, UnicodeError, csv.Error, StopIteration) as exc:
        raise MissionError('objective_catalogue_unavailable') from exc
    indexes = {}
    for table, rows in tables.items():
        index = {}
        for row in rows:
            name = row['values'].get('Name')
            if not name or name in index:
                raise MissionError('objective_static_identity_ambiguous')
            index[name] = row
        indexes[table] = index
    return {'tables': tables, 'indexes': indexes}


def main_story_candidates(catalogue: dict, chapter_progress: int) -> tuple[list[tuple[dict, list[dict]]], list[dict]]:
    """Every main-story root (with its steps) of the chapter the native
    progress counter names. Chapter 1 has two: the Zack prologue and the
    Nibelheim flashback share one chapter ID."""
    # Bind the native progress counter through the actual Chapter initializer
    # data, rather than guessing a chapter from its decimal digits.
    chapters, evidence = set(), []
    listed = []  # (progress value, chapter, source) of every initializer
    for row in catalogue['tables']['Chapter']:
        ids = {e['position']: e['value'] for e in row['arrays'].get('ResidentWorkIDList_Array', [])}
        values = {e['position']: e['value'] for e in row['arrays'].get('ResidentWorkValueList_Array', [])}
        for position, work in ids.items():
            chapter = row['values'].get('RefMainChapterID')
            if work != 'rswChapterProgress' or not chapter:
                continue
            try:
                listed.append((int(values.get(position)), chapter, row['source']))
            except (TypeError, ValueError):
                continue
            if values.get(position) == str(chapter_progress):
                chapters.add(chapter); evidence.append(row['source'])
    if not chapters:
        # Live 2026-10-20 (Mt. Nibel, the gas): the counter read 1045, between
        # initializers 1040 and 1050. A value the table does not list belongs
        # to a chapter only when the nearest listed values on both sides do.
        below = [item for item in listed if item[0] < chapter_progress]
        above = [item for item in listed if item[0] > chapter_progress]
        if below and above:
            low, high = max(v for v, _, _ in below), min(v for v, _, _ in above)
            around = [item for item in listed if item[0] in (low, high)]
            if len({chapter for _, chapter, _ in around}) == 1:
                chapters = {around[0][1]}
                evidence = [source for _, _, source in around]
    if len(chapters) != 1:
        raise MissionError('objective_chapter_progress_unmapped_or_ambiguous')
    chapter = next(iter(chapters))
    roots = []
    for row in catalogue['tables']['StoryResident']:
        flag = catalogue['indexes']['StoryFlag'].get(row['values'].get('StoryProgressFlagID'))
        if (number(row, 'StoryType') == 1 and flag is not None
                and flag['values'].get('ChapterID') == chapter):
            roots.append(row)
    if not roots:
        raise MissionError('objective_main_chapter_ambiguous')
    candidates = []
    for root in roots:
        steps = [r for r in catalogue['tables']['StoryResident']
                 if r['values'].get('ParentID') == root['values']['Name'] and number(r, 'StoryType') == 2]
        if not steps or len(steps) + 1 > 256:
            raise MissionError('objective_main_story_scope_unsupported')
        candidates.append((root, steps))
    if sum(len(steps) + 1 for _, steps in candidates) > 256:
        raise MissionError('objective_main_story_scope_unsupported')
    return candidates, evidence


def main_story_scope(catalogue: dict, chapter_progress: int) -> tuple[dict, list[dict], list[dict]]:
    candidates, evidence = main_story_candidates(catalogue, chapter_progress)
    if len(candidates) != 1:
        raise MissionError('objective_main_chapter_ambiguous')
    root, steps = candidates[0]
    return root, steps, evidence


def active_main_scope(candidates: list[tuple[dict, list[dict]]], flags: dict) -> tuple[dict, list[dict], dict]:
    """The one candidate root with exactly one active step, and that step.
    A root that is inactive or has no active step is not the current one;
    any other refusal (incomplete evidence, ambiguity) stops the search."""
    found = []
    for root, steps in candidates:
        try:
            found.append((root, steps, active_main_step(root, steps, flags)))
        except MissionError as exc:
            if str(exc) not in ('objective_main_chapter_inactive', 'objective_none_active') or len(candidates) == 1:
                raise
    if len(found) != 1:
        raise MissionError('objective_main_chapter_ambiguous' if found else 'objective_none_active')
    return found[0]


def active_main_step(root: dict, steps: list[dict], flags: dict) -> dict:
    required = [r['values']['StoryProgressFlagID'] for r in [root, *steps]]
    if any(type(flags.get(key)) is not bool for key in required):
        raise MissionError('objective_flag_evidence_incomplete')
    if not flags[root['values']['StoryProgressFlagID']]:
        raise MissionError('objective_main_chapter_inactive')
    groups = {}
    for row in steps:
        groups.setdefault(number(row, 'GroupIndex'), []).append(row)
    active = []
    for siblings in groups.values():
        reached = [r for r in siblings if flags[r['values']['StoryProgressFlagID']]]
        if not reached:
            continue
        ordinal = max(number(r, 'StoryIndex') for r in reached)
        latest = [r for r in reached if number(r, 'StoryIndex') == ordinal]
        if len(latest) != 1:
            raise MissionError('objective_step_version_ambiguous')
        status = number(latest[0], 'CompleteStatus')
        if status == 0:
            active.append(latest[0])
        elif status != 1:
            raise MissionError('objective_completion_state_unsupported')
    if len(active) != 1:
        raise MissionError('objective_active_ambiguous' if active else 'objective_none_active')
    return active[0]


SUB_OBJECTIVE_STATES = {0: 'active', 1: 'complete', 7: 'closed'}


def sub_objective_rows(catalogue: dict, step: dict) -> list[dict]:
    """A step's sub-objectives (StoryType 4 children): Chapter 1's town visit
    lists Cloud's house, Tifa's house, the water tank and the inn this way."""
    return [r for r in catalogue['tables']['StoryResident']
            if r['values'].get('ParentID') == step['values']['Name'] and number(r, 'StoryType') == 4]


def sub_objective_states(rows: list[dict], flags: dict) -> list[dict]:
    """Each sub-objective group's latest reached row and its state; groups
    with no reached row are not_started."""
    groups: dict[Any, list[dict]] = {}
    for row in rows:
        groups.setdefault(number(row, 'GroupIndex'), []).append(row)
    states = []
    for group, siblings in sorted(groups.items(), key=lambda item: item[0]):
        if any(type(flags.get(r['values']['StoryProgressFlagID'])) is not bool for r in siblings):
            raise MissionError('objective_flag_evidence_incomplete')
        reached = [r for r in siblings if flags[r['values']['StoryProgressFlagID']]]
        title = siblings[0]['values'].get('ItemName')
        if not reached:
            states.append({'group': group, 'title_text_id': title, 'state': 'not_started', 'row': None})
            continue
        latest = max(reached, key=lambda r: number(r, 'StoryIndex'))
        state = SUB_OBJECTIVE_STATES.get(number(latest, 'CompleteStatus'), 'unknown')
        states.append({'group': group, 'title_text_id': title, 'state': state, 'row': latest})
    return states


def objective_references(catalogue: dict, step: dict) -> list[dict]:
    references = []
    for entry in step['arrays'].get('MapIconInfoID_Array', []):
        marker = catalogue['indexes']['MapIconInfo'].get(entry['value'])
        if marker is None:
            raise MissionError('objective_marker_identity_missing')
        kinds = [e['value'] for e in marker['arrays'].get('IconType_Array', [])]
        values = marker['values']
        references.append({'target_type': 'poi', 'target_id': values['Name'],
                           'object_id': values.get('ObjectID'), 'field': values.get('AffiliationName'),
                           'location_id': values.get('LocationId'), 'goto_field': values.get('GotoLocationID'),
                           'primary': kinds == ['2'], 'icon_kinds': kinds,
                           'source': 'StoryResident.MapIconInfoID_Array -> MapIconInfo',
                           'story_column': entry['source_column'], 'marker_source': marker['source']})
    if not references:
        raise MissionError('objective_target_reference_missing')
    if len(references) == 1 and not references[0]['primary']:
        # Live 2026-10-20 (Mt. Nibel, "get rid of the gas"): the step's only
        # marker is the lift it is about (icon type 11). A sole marker is the
        # step's place, whatever its icon.
        references[0].update(primary=True, primary_basis='sole_marker')
    if len([r for r in references if r['primary']]) != 1:
        raise MissionError('objective_primary_target_ambiguous_or_unsupported')
    return references
