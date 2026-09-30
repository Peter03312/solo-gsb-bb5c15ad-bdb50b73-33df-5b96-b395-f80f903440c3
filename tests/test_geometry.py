"""几何模块测试：带洞多边形、洞边界、零长接触。"""
import math

import pytest

from app.geometry import (
    GeometryError,
    build_polygon,
    merge_intervals,
    point_in_region,
    segment_inside_params,
)

SQUARE = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)]


def test_valid_polygon_with_hole():
    poly = build_polygon(SQUARE, [[(30, 30), (70, 30), (70, 70), (30, 70)]])
    assert poly.area == pytest.approx(10000 - 1600)


def test_self_intersecting_polygon_rejected():
    bowtie = [(0, 0), (100, 100), (100, 0), (0, 100)]
    with pytest.raises(GeometryError, match="自交|不合法"):
        build_polygon(bowtie)


def test_hole_outside_rejected():
    with pytest.raises(GeometryError, match="越出|相接|不合法"):
        build_polygon(SQUARE, [[(200, 200), (240, 200), (240, 240), (200, 240)]])


def test_overlapping_holes_rejected():
    with pytest.raises(GeometryError, match="相交|重叠|不合法"):
        build_polygon(
            SQUARE,
            [
                [(10, 10), (60, 10), (60, 60), (10, 60)],
                [(40, 40), (90, 40), (90, 90), (40, 90)],
            ],
        )


def test_non_finite_coord_rejected():
    with pytest.raises(GeometryError, match="有限"):
        build_polygon([(0, 0), (float("nan"), 0), (100, 100)])
    with pytest.raises(GeometryError, match="有限"):
        build_polygon([(0, 0), (100, float("inf")), (100, 100)])


def test_segment_full_crossing():
    poly = build_polygon(SQUARE)
    params = segment_inside_params(poly, (-50, 50), (150, 50))
    assert params == [(0.25, 0.75)]


def test_segment_on_outer_boundary_counts():
    poly = build_polygon(SQUARE)
    params = segment_inside_params(poly, (0, 0), (100, 0))
    assert params == [(0.0, 1.0)]


def test_zero_duration_corner_touch_not_crossing():
    poly = build_polygon(SQUARE)
    # 仅擦过角点 (0,0)
    params = segment_inside_params(poly, (-10, 10), (10, -10))
    assert params == []


def test_tangent_edge_touch_not_crossing():
    poly = build_polygon(SQUARE)
    # 与外边界相切于单点 (50,0)
    params = segment_inside_params(poly, (50, 10), (50, -10))
    assert params == [(0.0, 0.5)]  # 进入边界内侧的一段算；切点不增加区间
    # 完全在外侧、只碰一点：
    params2 = segment_inside_params(poly, (40, -10), (60, -10))
    assert params2 == []


def test_hole_interior_and_boundary_excluded():
    poly = build_polygon(SQUARE, [[(30, 30), (70, 30), (70, 70), (30, 70)]])
    # 水平穿过：0-30 在区域，30-70 在洞（含边界），70-100 在区域
    params = segment_inside_params(poly, (0, 50), (100, 50))
    assert len(params) == 2
    assert params[0][1] == pytest.approx(0.30, abs=1e-8)
    assert params[1][0] == pytest.approx(0.70, abs=1e-8)


def test_segment_running_along_hole_boundary_excluded():
    poly = build_polygon(SQUARE, [[(30, 30), (70, 30), (70, 70), (30, 70)]])
    # 沿洞边界 y=30 行走：该边不属于区域
    params = segment_inside_params(poly, (20, 30), (80, 30))
    flat = [v for pair in params for v in pair]
    assert 0.3 not in [round(v, 6) for v in flat]
    # 区间应只剩 20-30 与 70-80
    lo, hi = params[0]
    assert (hi - lo) == pytest.approx(10 / 60, abs=1e-8)
    lo2, hi2 = params[1]
    assert (hi2 - lo2) == pytest.approx(10 / 60, abs=1e-8)


def test_stationary_point_classification():
    poly = build_polygon(SQUARE, [[(30, 30), (70, 30), (70, 70), (30, 70)]])
    assert point_in_region(poly, (50, 50)) is False       # 洞内
    assert point_in_region(poly, (30, 50)) is False       # 洞边界
    assert point_in_region(poly, (0, 0)) is True          # 外边界
    assert point_in_region(poly, (10, 50)) is True        # 区域内
    assert point_in_region(poly, (-1, 50)) is False       # 区域外
    assert segment_inside_params(poly, (50, 50), (50, 50)) == []
    assert segment_inside_params(poly, (10, 50), (10, 50)) == [(0.0, 1.0)]


def test_merge_intervals_touching():
    assert merge_intervals([(0.0, 5.0), (5.0, 10.0)]) == [(0.0, 10.0)]
    assert merge_intervals([(0.0, 5.0), (6.0, 10.0)]) == [(0.0, 5.0), (6.0, 10.0)]


def test_degenerate_ring_rejected():
    with pytest.raises(GeometryError):
        build_polygon([(0, 0), (100, 0), (0, 0)])
    with pytest.raises(GeometryError):
        build_polygon([(0, 0), (50, 0), (50, 0), (0, 100)])
