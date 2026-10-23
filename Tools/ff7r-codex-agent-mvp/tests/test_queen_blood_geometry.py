from __future__ import annotations

from ff7r_agent.queen_blood_geometry import BoardGeometry, fit_grid, nearest_cell

ORIGIN, U, V = (100.0, 50.0), (-30.0, 2.0), (1.0, 40.0)


def centre(row: int, column: int) -> tuple[float, float]:
    return (ORIGIN[0] + column * U[0] + row * V[0], ORIGIN[1] + column * U[1] + row * V[1])


def test_grid_is_recovered_from_three_cells_and_locates_every_cell() -> None:
    grid = fit_grid({cell: centre(*cell) for cell in [(0, 0), (1, 0), (2, 3)]})
    assert grid is not None
    for row in range(3):
        for column in range(5):
            x, y = centre(row, column)
            assert nearest_cell(grid, (x + 3.0, y - 2.0)) == (row, column)


def test_between_cells_and_collinear_samples_are_unknown() -> None:
    grid = fit_grid({cell: centre(*cell) for cell in [(0, 0), (1, 0), (2, 3)]})
    halfway = tuple((a + b) / 2 for a, b in zip(centre(1, 1), centre(1, 2)))
    assert nearest_cell(grid, halfway) is None
    assert fit_grid({cell: centre(*cell) for cell in [(0, 0), (0, 1), (0, 2)]}) is None


def test_geometry_prefers_hover_samples_and_persists(tmp_path) -> None:
    path = tmp_path / "geometry.json"
    geometry = BoardGeometry(path)
    # Placed cards sit slightly off the held card's hover points.
    for cell in [(0, 0), (1, 2), (2, 1)]:
        geometry.observe("board", cell, (centre(*cell)[0] + 9.0, centre(*cell)[1]))
    assert geometry.hovered_cell(centre(1, 1)) == (1, 1)
    for cell in [(0, 0), (1, 0), (2, 1)]:
        geometry.observe("hover", cell, centre(*cell))
    geometry.save()
    reloaded = BoardGeometry(path)
    assert reloaded.grid() is not None
    assert reloaded.hovered_cell(centre(2, 4)) == (2, 4)
