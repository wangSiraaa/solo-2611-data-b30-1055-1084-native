"""硬性上限与输入契约测试。"""

from __future__ import annotations

import unittest

from review.fixtures import pending_case
from review.model import (AdmissionCode, BenchmarkPair, CameraSpec, CaseFile,
                          FrameObservation, validate_case, validate_limits)


class TestLimits(unittest.TestCase):
    def test_offset_album_capped_at_five(self):
        spec = CameraSpec(
            "C", (BenchmarkPair(0, frozenset({0})),), (),
            frozenset({0, 1, 2, 3, 4, 5}))
        failures = validate_limits((spec,), (1,), ("D1",))
        self.assertTrue(
            any(f.code == AdmissionCode.OFFSET_OVER_LIMIT for f in failures))

    def test_beat_capped_at_ten(self):
        failures = validate_limits(
            (CameraSpec("C", (), (), frozenset({0})),), (11,), ())
        self.assertTrue(
            any(f.code == AdmissionCode.BAD_LIMITS for f in failures))

    def test_defenders_capped_at_six(self):
        failures = validate_limits((), (1,), tuple(f"D{i}" for i in range(7)))
        self.assertTrue(failures)

    def test_three_cameras_accepted_fourth_rejected(self):
        specs = tuple(
            CameraSpec(f"C{i}", (), (), frozenset({0})) for i in range(4))
        failures = validate_limits(specs, (1,), ("D1",))
        self.assertTrue(
            any("机位" in f.detail for f in failures))

    def test_validate_case_rejects_unknown_camera_reference(self):
        base = pending_case()
        bad = CaseFile(**{
            **base.__dict__,
            "cameras": tuple(base.cameras),
            "observations": (*base.observations,
                             FrameObservation("GHOST", 5, "R", 60))})
        with self.assertRaises(ValueError):
            validate_case(bad)


if __name__ == "__main__":
    unittest.main()
