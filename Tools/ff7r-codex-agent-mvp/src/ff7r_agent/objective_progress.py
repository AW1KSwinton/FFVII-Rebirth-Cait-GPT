"""One request-scoped, read-only snapshot of declared story flags."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
import uuid

from .character_locator import _sample_source_sequence
from .map_query import file_signature
from .navigation_values import MissionError
from .queen_blood_runs import process_identity

FLAG_CATALOG = Path(r'C:\CaitGPT\DataTableIndex\Input\Resident\StoryFlag.csv')
TOKEN = re.compile(r'[A-Za-z0-9_-]{1,160}\Z')
MAX_FLAGS = 256


def declared_flags(path: Path = FLAG_CATALOG) -> tuple[set[str], str]:
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            raise MissionError('objective_flag_catalog_too_large')
        data = path.read_bytes()
    except OSError as exc:
        raise MissionError('objective_flag_catalog_unavailable') from exc
    rows = csv.DictReader(data.decode('utf-8-sig').splitlines())
    return {r['Name'] for r in rows if TOKEN.fullmatch(r.get('Name', ''))}, hashlib.sha256(data).hexdigest()


def read_objective_progress(service, story_flags: list[str], *, flag_catalog_path: Path | None = None,
                            timeout_seconds: float = 8.0) -> dict:
    if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
            or not 0.1 <= timeout_seconds <= 8):
        raise MissionError('objective_timeout_invalid')
    if (not isinstance(story_flags, list) or len(story_flags) > MAX_FLAGS
            or any(not isinstance(v, str) or not TOKEN.fullmatch(v) for v in story_flags)
            or len(set(story_flags)) != len(story_flags)):
        raise MissionError('objective_flag_request_invalid')
    export_root = os.environ.get('FF7R_OBJECTIVE_DATA_ROOT')
    flag_catalog_path = Path(flag_catalog_path or
                            (Path(export_root) / 'Resident/StoryFlag.csv' if export_root else FLAG_CATALOG))
    known, catalog_sha = declared_flags(flag_catalog_path)
    if not set(story_flags) <= known:
        raise MissionError('objective_flag_not_declared')
    identity = process_identity(service._environment)
    base = service.observe()
    actor = (base.get('player') or {}).get('actor_id')
    if (identity is None or not actor or not base.get('map_id')
            or (base.get('telemetry') or {}).get('live') is not True):
        raise MissionError('objective_live_identity_unavailable')
    directory = Path(service.telemetry.path).parent
    request_path = directory / 'objective-query.request'
    response_path = directory / 'objective-query.json'
    request_id = uuid.uuid4().hex
    content = '|'.join([request_id, str(int(time.time()) + 30), ','.join(story_flags) or '-'])
    temporary = request_path.with_name(request_path.name + '.' + request_id)
    seen = file_signature(response_path)
    sleeper = service._sleeper
    try:
        temporary.write_text(content, encoding='ascii')
        os.replace(temporary, request_path)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            signature = file_signature(response_path)
            if signature is not None and signature != seen:
                seen = signature
                if response_path.stat().st_size > 1024 * 1024:
                    raise MissionError('objective_response_too_large')
                try:
                    payload = json.loads(response_path.read_text(encoding='utf-8'))
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    raise MissionError('objective_response_invalid') from exc
                if not isinstance(payload, dict):
                    raise MissionError('objective_response_invalid')
                if payload.get('request_id') == request_id:
                    break
            sleeper(0.05)
        else:
            raise MissionError('objective_sampler_timeout')
    finally:
        temporary.unlink(missing_ok=True)
        try:
            if request_path.read_text(encoding='ascii').split('|')[0] == request_id:
                request_path.unlink()
        except FileNotFoundError:
            pass
    after = service.observe()
    if (process_identity(service._environment) != identity or after.get('map_id') != base['map_id']
            or (after.get('player') or {}).get('actor_id') != actor
            or payload.get('map_id') != base['map_id'] or payload.get('player_actor_id') != actor):
        raise MissionError('objective_identity_changed')
    if (payload.get('schema_version') != 1 or payload.get('source') != 'ue4ss_objective_progress'
            or type(payload.get('source_sequence')) is not int
            or payload['source_sequence'] < _sample_source_sequence(base)):
        raise MissionError('objective_response_invalid')
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(payload['timestamp_utc'].replace('Z', '+00:00'))).total_seconds()
    except (KeyError, ValueError, TypeError, AttributeError):
        raise MissionError('objective_timestamp_invalid') from None
    if not 0 <= age <= 2.5:
        raise MissionError('objective_snapshot_stale')
    evidence = {**payload, 'identity': {'world_id': base['map_id'], 'map_id': base['map_id'],
                                       'process_token': identity['pid'] + ':' + identity['token']},
                'flag_catalog_sha256': catalog_sha, 'input_sent': False, 'input_authorized': False}
    if payload.get('observed') is not True:
        return {**evidence, 'ok': False}
    flags = payload.get('flags')
    if (not isinstance(flags, dict) or set(flags) != set(story_flags)
            or any(type(v) is not bool for v in flags.values())
            or type(payload.get('chapter_progress')) is not int):
        raise MissionError('objective_progress_incomplete')
    return {**evidence, 'ok': True}
