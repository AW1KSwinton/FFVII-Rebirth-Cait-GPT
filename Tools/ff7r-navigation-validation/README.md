# FFVII Rebirth offline navigation validation

This directory is a standalone offline analyzer, deterministic validation
corpus generator, and independent RoutePlan oracle. It imports no planner,
UE4SS, Unreal, EndNavAPI, EQS, or live-game functionality.

## Generate the corpus and report

From the repository root:

```powershell
python Tools/ff7r-navigation-validation/navigation_validation.py analyze
```

Outputs are written under `Tools/ff7r-navigation-validation/outputs/`:

- `topology-analysis.json` — normalized serialized polygons, decoded refs,
  centroids, adjacency evidence, link classifications, components, degree
  data, vertical/stacked candidates, and off-mesh records;
- `validation-corpus.json` — deterministic A/B cases; it stores no production
  route corridor;
- `validation-report.md` — concise human-readable summary.

The default input is the preserved snapshot at
`ProjectKnowledge/Evidence/2026-08-17-shipping-detour-topology-live/navigation-native-geometry.json`.
Use `--snapshot` to analyze another preserved snapshot explicitly.

The snapshot embeds a broader proven topology count than the detailed
serialized `tiles` array. The analyzer reports both scopes and does not invent
per-polygon geometry for proof-only records. Exact source refs in the detail
are reconstructed only when a consistent same-tile link identifies the tile
index and salt; all other decoded target refs remain reference-only.

## Validate a future RoutePlan

The validator accepts one plan object, a JSON list of plans, or
`{"plans": [...]}`. The primary contract is the real
`DetourRoutePlan.to_dict()` shape: `schema_version`, `source`,
`executable_sha256`, `status`, `reason_code`, requested/resolved endpoint
objects, `start_ref`, `end_ref`, `polygon_corridor`, real `portals`,
`waypoints`, `total_estimated_path_length_m`, resolution objects,
`validation`, `diagnostics`, and `movement_sent`. Legacy corridor aliases are
not needed for planner output and are retained only for backwards readability.

```powershell
python Tools/ff7r-navigation-validation/navigation_validation.py validate `
  --corpus Tools/ff7r-navigation-validation/outputs/validation-corpus.json `
  --route-plan path\to\future-route-plans.json `
  --output Tools/ff7r-navigation-validation/outputs/route-plan-validation.json
```

Exit code `0` means every supplied plan passed; exit code `1` means at least
one plan was rejected. Rejection reasons are explicit and include nonexistent
or reference-only refs, missing directed source links, reverse-only or
neighbour-only transitions, invalid endpoint sentinels, wrong link/edge/side
portal fields, incorrect raw-link clipping, non-finite coordinates, endpoint
mismatch, metre/centimetre length disagreement, pathological oscillation, and
incorrect `validation.tile_crossings`.

The frozen compatibility fixture is
`fixtures/real-detour-route-plan.json`. It is JSON data only, shaped like
`DetourRoutePlan.to_dict()`, and contains the independently reconstructed
14-polygon / 9-tile-crossing representative corridor. The oracle never imports
or executes planner code and never generates a route.

## Tests

```powershell
python -m unittest discover -s Tools/ff7r-navigation-validation -p 'test_*.py' -v
```

The tests use deliberately corrupted RoutePlans and never start a game or
invoke a native/path query.
