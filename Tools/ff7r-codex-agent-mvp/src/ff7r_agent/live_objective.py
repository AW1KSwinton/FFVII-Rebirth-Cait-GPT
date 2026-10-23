"""Discover the current supported MSQ step, then ask the target resolver where."""
from __future__ import annotations

import time

from .navigation_values import MissionError
from .objective_catalogue import (active_main_scope, active_main_step, load_catalogue, main_story_candidates,
                                  number, objective_references, sub_objective_rows, sub_objective_states)
from .objective_progress import read_objective_progress
from .objective_resolver import objective_key, resolve_active_objective
from .world_target_resolver import resolve_objective_reference


def get_active_objective(service, activity_type: str = 'main_story') -> dict:
    refusal = {'ok': False, 'position': None, 'input_sent': False, 'input_authorized': False}
    if activity_type != 'main_story':
        return {**refusal, 'reason': 'objective_activity_type_unsupported', 'activity_type': activity_type}
    deadline = time.monotonic() + 15.0
    # What is known so far rides on a refusal, so a failed resolution still
    # names the step and the marker it could not place.
    known: dict = {}
    def progress(flags):
        remaining = deadline - time.monotonic()
        if remaining < 0.1:
            raise MissionError('objective_resolution_timeout')
        return read_objective_progress(service, flags, timeout_seconds=min(8.0, remaining))
    try:
        context = progress([])
        if not context.get('ok'):
            return {**refusal, 'reason': context.get('reason'), 'progress_evidence': context}
        catalogue = load_catalogue()
        candidates, chapter_evidence = main_story_candidates(catalogue, context['chapter_progress'])
        flags = list(dict.fromkeys(r['values']['StoryProgressFlagID']
                                   for root, steps in candidates for r in [root, *steps]))
        observed = progress(flags)
        _same_context(context, observed, catalogue)
        root, steps, step = active_main_scope(candidates, observed['flags'])
        known.update(step_id=step['values']['Name'], title_text_id=step['values'].get('ItemName'),
                     chapter_progress=context['chapter_progress'])
        references = objective_references(catalogue, step)
        known['target_references'] = references
        primary = next(r for r in references if r['primary'])
        # The existing paired POI read has a four-second observation bound.
        if deadline - time.monotonic() < 4.1:
            raise MissionError('objective_resolution_timeout')
        destination = resolve_objective_reference(service, primary)
        # A new step can activate while a placement query is waiting. Return
        # only a target still belonging to the fresh active step afterward.
        checked = progress(flags)
        _same_context(context, checked, catalogue)
        current = active_main_step(root, steps, checked['flags'])
        if current['values']['Name'] != step['values']['Name']:
            raise MissionError('objective_changed_during_resolution')
        values = current['values']
        snapshot = {'schema_version': 1, 'observed': True, 'sequence': checked['source_sequence'],
                    'observed_at': checked['timestamp_utc'], 'identity': checked['identity'],
                    'source': 'EndDataBaseAPI.IsStoryFlag joined to StoryResident',
                    'authority': 'runtime_story_progress_with_static_target_references',
                    'objectives': [{'quest_id': root['values']['Name'],
                                    'objective_id': root['values']['Name'] + ':' + str(number(current, 'GroupIndex')),
                                    'step_id': values['Name'], 'state': 'active', 'targets': [primary]}]}
        result = resolve_active_objective(snapshot, current_identity=checked['identity'],
                                          resolve_target=lambda _: destination)
        subs = sub_objectives(service, catalogue, current, context)
        return {**result, 'activity_type': activity_type, 'objective_key': objective_key(result),
                'sub_objectives': subs,
                'title_text_id': values.get('ItemName'), 'chapter_progress': checked['chapter_progress'],
                'target_references': references, 'progress_evidence': checked,
                'objective_row_source': current['source'], 'chapter_binding_sources': chapter_evidence,
                'destination_evidence': destination, 'input_authorized': False}
    except MissionError as exc:
        return {**refusal, 'reason': str(exc), 'activity_type': activity_type, **known}


def _same_context(initial: dict, current: dict, catalogue: dict) -> None:
    if not current.get('ok'):
        raise MissionError(current.get('reason') or 'objective_progress_unobserved')
    if (current['identity'] != initial['identity']
            or current['player_actor_id'] != initial['player_actor_id']):
        raise MissionError('objective_identity_changed')
    if current['chapter_progress'] != initial['chapter_progress']:
        raise MissionError('objective_chapter_changed_during_resolution')
    flag_sha = catalogue['tables']['StoryFlag'][0]['source']['sha256']
    if current['flag_catalog_sha256'] != flag_sha:
        raise MissionError('objective_flag_catalog_changed')


def sub_objectives(service, catalogue: dict, step: dict, context: dict) -> dict:
    """The active step's sub-objectives with their states, and the marker
    position of each active one. Advisory: a failure here never hides the
    step's own destination."""
    rows = sub_objective_rows(catalogue, step)
    if not rows:
        return {'ok': True, 'items': []}
    deadline = time.monotonic() + 20.0
    try:
        flags = list(dict.fromkeys(r['values']['StoryProgressFlagID'] for r in rows))
        observed = read_objective_progress(service, flags, timeout_seconds=8.0)
        _same_context(context, observed, catalogue)
        items = []
        for entry in sub_objective_states(rows, observed['flags']):
            row = entry.pop('row')
            item = {**entry, 'step_id': row['values']['Name'] if row else None}
            if entry['state'] == 'active':
                item['position'] = None
                try:
                    references = objective_references(catalogue, row)
                except MissionError as exc:
                    item['reason'] = str(exc)
                    references = []
                if references:
                    reference = next((r for r in references if r['primary']), references[0])
                    item['marker'] = reference['target_id']
                    if deadline - time.monotonic() > 4.1:
                        located = resolve_objective_reference(service, reference)
                        item['position'] = located.get('position')
                        item['marker_status'] = located.get('status')
                    else:
                        item['reason'] = 'objective_resolution_timeout'
            items.append(item)
        return {'ok': True, 'items': items}
    except MissionError as exc:
        return {'ok': False, 'reason': str(exc), 'items': []}
