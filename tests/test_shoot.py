"""补拍单测试：两套工位、纸封不可倒回、同拍约束、正背服饰、换偶约束、拒收溯源。"""

from __future__ import annotations

import unittest

from animation_pipeline.puppets import (
    ORIENTATION_LEFT,
    Hole,
    Mark,
    Pattern,
    Seed,
    read_scene,
)
import numpy as np

from animation_pipeline.shoot import (
    STATION_DOUBLE_FRAME,
    STATION_SINGLE_TURNTABLE,
    Camera,
    FrameJob,
    RealPuppet,
    Worksheet,
    plan_shoot,
    verify_plan,
)
from animation_pipeline.scenes import (
    base_jobs,
    build_crowd_scene,
    build_example_scene,
    build_two_act_scene,
    build_variant_clear_scene,
    build_variant_no_back_scene,
    no_back_jobs,
    two_act_jobs,
)

RED = (220, 40, 40)

DOUBLE = Pattern("double", frozenset({(0, 0), (1, 0)}), notch=(1, 0),
                 front_costume="cf", back_costume="cb", orientation=ORIENTATION_LEFT)
LEFT_ONLY = Pattern("one", frozenset({(0, 0)}), notch=(0, 0),
                    front_costume="cf", back_costume=None, orientation=ORIENTATION_LEFT)
FRONT_ONLY_DOUBLE = Pattern("double", frozenset({(0, 0), (1, 0)}), notch=(1, 0),
                            front_costume="cf", back_costume=None,
                            orientation=ORIENTATION_LEFT)


def cams():
    return {
        "cf": Camera("cf", "front"),
        "cb": Camera("cb", "back"),
    }


def red_canvas(double=True):
    c = np.zeros((3, 4, 4), dtype=np.uint8)
    c[:, :, 3] = 255
    c[0, 0] = (*RED, 255)
    if double:
        c[0, 1] = (*RED, 255)
    return c


def reading_for(canvas, patterns, fid=10):
    marks = (Mark(Seed("p", 0, 0, RED), Hole("h", 0, 0),
                  frozenset({ORIENTATION_LEFT})),)
    r = read_scene(canvas, marks, tuple(patterns), frame_id=fid, time_ms=fid)
    return {(fid, "p"): r.puppets["p"]}


class DoubleFrameStationTests(unittest.TestCase):
    def test_simultaneous_front_back_two_exposures(self) -> None:
        rp = RealPuppet("rp", "p", pose="double", sealed=True)
        ws = Worksheet(
            jobs=(FrameJob(1, 10, "p", "double", ("cf", "cb"), simultaneous=True),),
            cameras=cams(), puppets=(rp,), patterns={"double": DOUBLE},
            binding={"p": "rp"},
        )
        plan = plan_shoot(ws, STATION_DOUBLE_FRAME)
        self.assertEqual(len(plan.exposures), 2)
        self.assertEqual({e.side for e in plan.exposures}, {"front", "back"})
        self.assertEqual(len({e.group for e in plan.exposures}), 1)
        self.assertEqual(len({e.slot_index for e in plan.exposures}), 1)  # 同一拍
        self.assertEqual(verify_plan(plan, ws), [])

    def test_single_face_job_single_exposure(self) -> None:
        rp = RealPuppet("rp", "p", pose="double", sealed=True)
        ws = Worksheet(
            jobs=(FrameJob(1, 10, "p", "double", ("cf",), simultaneous=False),),
            cameras=cams(), puppets=(rp,), patterns={"double": DOUBLE},
            binding={"p": "rp"},
        )
        plan = plan_shoot(ws, STATION_DOUBLE_FRAME)
        self.assertEqual(len(plan.exposures), 1)
        self.assertIsNone(plan.exposures[0].group)

    def test_remount_costs_a_slot(self) -> None:
        rp = RealPuppet("rp", "p", pose="double", sealed=True)
        ws = Worksheet(
            jobs=(
                FrameJob(1, 10, "p", "double", ("cf",)),
                FrameJob(2, 20, "p", "double", ("cf",)),
            ),
            cameras=cams(), puppets=(rp,), patterns={"double": DOUBLE},
            binding={"p": "rp"},
        )
        plan = plan_shoot(ws, STATION_DOUBLE_FRAME)
        # 第一格换框一次（+曝光），第二格仍挂同一只偶不再换框。
        kinds = [s.kind for s in plan.slots]
        self.assertEqual(kinds.count("remount"), 1)
        self.assertEqual(len(plan.exposures), 2)

    def test_frame_cannot_change_puppet_mid_frame(self) -> None:
        # 两格不同 frame、都挂同一只偶：第二次同框不需换框（框内不换偶）。
        rp = RealPuppet("rp", "p", pose="double", sealed=True)
        ws = Worksheet(
            jobs=(
                FrameJob(1, 10, "p", "double", ("cf", "cb"), simultaneous=True),
                FrameJob(2, 20, "p", "double", ("cf", "cb"), simultaneous=True),
            ),
            cameras=cams(), puppets=(rp,), patterns={"double": DOUBLE},
            binding={"p": "rp"},
        )
        plan = plan_shoot(ws, STATION_DOUBLE_FRAME)
        self.assertEqual([s.kind for s in plan.slots].count("remount"), 1)
        self.assertEqual(len(plan.exposures), 4)

    def test_no_back_costume_rejects_whole_frame(self) -> None:
        rp = RealPuppet("rp", "p", pose="double", sealed=True)
        ws = Worksheet(
            jobs=(FrameJob(1, 10, "p", "double", ("cf", "cb"), simultaneous=True),),
            cameras=cams(), puppets=(rp,),
            patterns={"double": FRONT_ONLY_DOUBLE}, binding={"p": "rp"},
        )
        plan = plan_shoot(ws, STATION_DOUBLE_FRAME)
        self.assertEqual(plan.exposures, [])
        self.assertEqual(len(plan.undelivered), 1)
        self.assertIn("back", plan.undelivered[0].trace)

    def test_camera_clash_same_timepoint(self) -> None:
        rp = RealPuppet("rp", "p", pose="double", sealed=True)
        # 同一时点两格抢同一台机位 → 第二格拒收并追到被占相机。
        ws = Worksheet(
            jobs=(
                FrameJob(1, 10, "p", "double", ("cf", "cb"), simultaneous=True),
                FrameJob(2, 10, "p", "double", ("cf", "cb"), simultaneous=True),
            ),
            cameras=cams(), puppets=(rp,), patterns={"double": DOUBLE},
            binding={"p": "rp"},
        )
        plan = plan_shoot(ws, STATION_DOUBLE_FRAME)
        self.assertEqual(len(plan.exposures), 2)  # 只第一格成功
        self.assertEqual(len(plan.undelivered), 1)
        self.assertIn("相机在同时点被占", plan.undelivered[0].reason)

    def test_two_frames_grab_same_puppet_simultaneously(self) -> None:
        # 同一时点、不同机位，但只有一只实偶 → 抢偶拒收，追到被占实偶。
        rp = RealPuppet("rp", "p", pose="double", sealed=True)
        cameras = dict(cams(), cf2=Camera("cf2", "front"))
        ws = Worksheet(
            jobs=(
                FrameJob(1, 10, "p", "double", ("cf", "cb"), simultaneous=True),
                FrameJob(2, 10, "p", "double", ("cf2",), simultaneous=False),
            ),
            cameras=cameras, puppets=(rp,), patterns={"double": DOUBLE},
            binding={"p": "rp"},
        )
        plan = plan_shoot(ws, STATION_DOUBLE_FRAME)
        self.assertEqual(len(plan.exposures), 2)
        self.assertEqual(len(plan.undelivered), 1)
        self.assertIn("抢实偶", plan.undelivered[0].reason)
        self.assertIn("rp", plan.undelivered[0].trace)


class SealTests(unittest.TestCase):
    def test_cannot_reach_pose_after_seal(self) -> None:
        # 实偶封锁在 A，工单要 B → 不可达，拒收溯源到纸封所在格。
        rp = RealPuppet("a_pup", "p", pose="one", sealed=False,
                        transitions=(("one", "double", True),))
        # 先在第 10ms 格拍到 double（会拍封），再在第 20ms 格要 one（倒回）。
        ws = Worksheet(
            jobs=(
                FrameJob(1, 10, "p", "double", ("cf",), simultaneous=False),
                FrameJob(2, 20, "p", "one", ("cf",), simultaneous=False),
            ),
            cameras=cams(), puppets=(rp,),
            patterns={"double": DOUBLE, "one": LEFT_ONLY},
            binding={"p": "a_pup"},
        )
        plan = plan_shoot(ws, STATION_DOUBLE_FRAME)
        # 第一格成功（拍封锁 double）；第二格要 one 不可达。
        self.assertEqual(len(plan.exposures), 1)
        self.assertEqual(len(plan.undelivered), 1)
        self.assertIn("纸封", plan.undelivered[0].trace)
        self.assertIn("frame 1", plan.undelivered[0].trace)

    def test_sealed_puppet_only_current_pose(self) -> None:
        rp = RealPuppet("rp", "p", pose="one", sealed=True)
        ws = Worksheet(
            jobs=(FrameJob(1, 10, "p", "double", ("cf",), simultaneous=False),),
            cameras=cams(), puppets=(rp,), patterns={"double": DOUBLE},
            binding={"p": "rp"},
        )
        plan = plan_shoot(ws, STATION_DOUBLE_FRAME)
        self.assertEqual(plan.exposures, [])
        self.assertIn("锁死", plan.undelivered[0].trace)

    def test_plan_cannot_reverse_seal(self) -> None:
        # 手工构造一个“封后倒回”的坏计划，verify_plan 必须抓出来。
        from animation_pipeline.shoot import Exposure, ShootPlan, Slot
        rp = RealPuppet("a_pup", "p", pose="one", sealed=False,
                        transitions=(("one", "double", True),))
        ws = Worksheet(
            jobs=(), cameras=cams(), puppets=(rp,),
            patterns={"double": DOUBLE, "one": LEFT_ONLY}, binding={"p": "a_pup"},
        )
        bad = ShootPlan(
            station=STATION_DOUBLE_FRAME,
            slots=[Slot(0, "expose", ""), Slot(1, "expose", "")],
            exposures=[
                Exposure("E001", 1, "cf", "front", "cf", "double", "a_pup", 0, None),
                Exposure("E002", 2, "cf", "front", "cf", "one", "a_pup", 1, None),
            ],
        )
        problems = verify_plan(bad, ws)
        self.assertTrue(any("纸封倒回" in p for p in problems))


class TurntableStationTests(unittest.TestCase):
    def test_simultaneous_rejected(self) -> None:
        rp = RealPuppet("rp", "p", pose="double", sealed=True)
        ws = Worksheet(
            jobs=(FrameJob(1, 10, "p", "double", ("cf", "cb"), simultaneous=True),),
            cameras=cams(), puppets=(rp,), patterns={"double": DOUBLE},
            binding={"p": "rp"},
        )
        plan = plan_shoot(ws, STATION_SINGLE_TURNTABLE)
        self.assertEqual(plan.exposures, [])
        self.assertIn("无法同拍", plan.undelivered[0].reason)

    def test_sequential_front_back_no_group(self) -> None:
        rp = RealPuppet("rp", "p", pose="double", sealed=True)
        # 非 simultaneously 的两面要求 → 转盘先后出片，绝不发同拍组号。
        ws = Worksheet(
            jobs=(FrameJob(1, 10, "p", "double", ("cf", "cb"), simultaneous=False),),
            cameras=cams(), puppets=(rp,), patterns={"double": DOUBLE},
            binding={"p": "rp"},
        )
        plan = plan_shoot(ws, STATION_SINGLE_TURNTABLE)
        self.assertEqual(len(plan.exposures), 2)
        self.assertTrue(all(e.group is None for e in plan.exposures))
        self.assertNotEqual(plan.exposures[0].slot_index, plan.exposures[1].slot_index)
        self.assertEqual(verify_plan(plan, ws), [])

    def test_sequential_front_and_back_faces(self) -> None:
        rp = RealPuppet("rp", "p", pose="double", sealed=True)
        ws = Worksheet(
            jobs=(FrameJob(1, 10, "p", "double", ("cf", "cb"), simultaneous=False),),
            cameras=cams(), puppets=(rp,), patterns={"double": DOUBLE},
            binding={"p": "rp"},
        )
        plan = plan_shoot(ws, STATION_SINGLE_TURNTABLE)
        # 固定 front/back 两台机位：天然各出正面/背面，无需转盘挑面。
        self.assertEqual([e.side for e in plan.exposures], ["front", "back"])
        self.assertNotIn("rotate", [s.kind for s in plan.slots])

    def test_single_turntable_camera_rotates_to_other_face(self) -> None:
        # 一台纯转盘机位（无固定面）拍正背两面：首次不转，第二次反面要转一格。
        rp = RealPuppet("rp", "p", pose="double", sealed=True)
        cameras = {"tt": Camera("tt", None)}
        ws = Worksheet(
            jobs=(FrameJob(1, 10, "p", "double", ("tt",), simultaneous=False),),
            cameras=cameras, puppets=(rp,), patterns={"double": DOUBLE},
            binding={"p": "rp"},
        )
        plan = plan_shoot(ws, STATION_SINGLE_TURNTABLE)
        kinds = [s.kind for s in plan.slots]
        self.assertEqual(len(plan.exposures), 2)  # 正背各一张
        self.assertIn("rotate", kinds)            # 转一次占一格
        self.assertEqual(kinds.count("rotate"), 1)  # 只转一次（front→back）
        self.assertTrue(all(e.group is None for e in plan.exposures))


class SubstituteAndReadingTests(unittest.TestCase):
    def test_borrow_same_model_when_allowed(self) -> None:
        solo = Pattern("solo", frozenset({(0, 0), (0, 1)}), notch=(0, 1),
                       front_costume="sf", back_costume="sb")
        rp1 = RealPuppet("r1", "p", pose="one", sealed=False,
                         transitions=(("one", "double", True),))
        rp2 = RealPuppet("r2", "p", pose="solo", sealed=True)
        # r1 在 frame1 拍封锁在 double，frame2 要 solo：r1 已封不可达 → 借同款 r2。
        ws = Worksheet(
            jobs=(
                FrameJob(1, 10, "p", "double", ("cf",), simultaneous=False),
                FrameJob(2, 20, "p", "solo", ("cf",), simultaneous=False,
                         allow_substitute=True),
            ),
            cameras=cams(), puppets=(rp1, rp2),
            patterns={"double": DOUBLE, "solo": solo},
            binding={"p": "r1"},
        )
        plan = plan_shoot(ws, STATION_DOUBLE_FRAME)
        self.assertEqual(len(plan.exposures), 2)
        self.assertEqual(plan.exposures[0].puppet, "r1")
        self.assertEqual(plan.exposures[1].puppet, "r2")
        self.assertEqual(verify_plan(plan, ws), [])

    def test_no_substitute_without_permission(self) -> None:
        # r1 封在 one（不同姿势）；r2 同款且正在 double。第二格不许换偶 →
        # 必须拒收，绝不许偷偷借 r2。
        rp1 = RealPuppet("r1", "p", pose="one", sealed=True)
        rp2 = RealPuppet("r2", "p", pose="double", sealed=True)
        ws = Worksheet(
            jobs=(
                FrameJob(2, 20, "p", "double", ("cf",), simultaneous=False,
                         allow_substitute=False),
            ),
            cameras=cams(), puppets=(rp1, rp2),
            patterns={"double": DOUBLE, "one": LEFT_ONLY}, binding={"p": "r1"},
        )
        plan = plan_shoot(ws, STATION_DOUBLE_FRAME)
        self.assertEqual(plan.exposures, [])  # 不许用 r2
        self.assertEqual(len(plan.undelivered), 1)
        self.assertIn("r1", plan.undelivered[0].trace)

    def test_reading_gate_rejects_mismatched_pose(self) -> None:
        rd = reading_for(red_canvas(double=True), patterns=(DOUBLE,), fid=10)
        rp = RealPuppet("rp", "p", pose="double", sealed=True)
        ws = Worksheet(
            jobs=(FrameJob(10, 100, "p", "one", ("cf",), simultaneous=False),),
            cameras=cams(), puppets=(rp,),
            patterns={"double": DOUBLE, "one": LEFT_ONLY},
            binding={"p": "rp"}, readings=rd,
        )
        plan = plan_shoot(ws, STATION_DOUBLE_FRAME)
        self.assertEqual(plan.exposures, [])
        self.assertIn("读法", plan.undelivered[0].reason)

    def test_reading_gate_accepts_matching_pose(self) -> None:
        rd = reading_for(red_canvas(double=True), patterns=(DOUBLE,), fid=10)
        rp = RealPuppet("rp", "p", pose="double", sealed=True)
        ws = Worksheet(
            jobs=(FrameJob(10, 100, "p", "double", ("cf", "cb"), simultaneous=True),),
            cameras=cams(), puppets=(rp,), patterns={"double": DOUBLE},
            binding={"p": "rp"}, readings=rd,
        )
        plan = plan_shoot(ws, STATION_DOUBLE_FRAME)
        self.assertEqual(len(plan.exposures), 2)


class ExampleScenarioTests(unittest.TestCase):
    """题面例子与两个对照变体，端到端。"""

    def _readings(self, scene):
        out = {}
        for fid, marks in scene.marks_by_frame.items():
            r = read_scene(scene.frames[fid].canvas, marks,
                           tuple(scene.patterns.values()), frame_id=fid, time_ms=fid * 10)
            for name, pr in r.puppets.items():
                out[(fid, name)] = pr
        return out

    def _ws(self, scene, jobs):
        return Worksheet(
            jobs=jobs, cameras=scene.cameras, puppets=scene.puppets,
            patterns=scene.patterns, binding=scene.binding,
            readings=self._readings(scene),
        )

    def test_base_both_later_frames_read_double(self) -> None:
        sc = build_example_scene()
        rs = self._readings(sc)
        self.assertEqual(rs[(1, "redpup")].pose.name, "red_double")
        self.assertEqual(rs[(2, "redpup")].pose.name, "red_double")

    def test_base_double_frame_delivers_both_frames(self) -> None:
        sc = build_example_scene()
        ws = self._ws(sc, base_jobs())
        plan = plan_shoot(ws, STATION_DOUBLE_FRAME)
        self.assertEqual(plan.undelivered, [])
        self.assertEqual({e.frame_id for e in plan.exposures}, {1, 2})
        self.assertEqual(verify_plan(plan, ws), [])
        # 每格正背同拍 → 两组、各两张。
        groups = {}
        for e in plan.exposures:
            groups.setdefault(e.group, []).append(e)
        self.assertEqual(len(groups), 2)
        self.assertTrue(all(len(v) == 2 for v in groups.values()))

    def test_base_turntable_cannot_make_simultaneous_pair(self) -> None:
        sc = build_example_scene()
        ws = self._ws(sc, base_jobs())
        plan = plan_shoot(ws, STATION_SINGLE_TURNTABLE)
        self.assertEqual(plan.exposures, [])
        self.assertTrue(all("无法同拍" in u.reason for u in plan.undelivered))

    def test_variant_clear_last_frame_not_double(self) -> None:
        sc = build_variant_clear_scene()
        rs = self._readings(sc)
        self.assertEqual(rs[(1, "redpup")].pose.name, "red_double")
        # 末帧只剩左格 → 读法不是 double，工单无法据此签。
        self.assertNotEqual(rs[(2, "redpup")].pose, sc.patterns["red_double"]
                            if "red_double" in sc.patterns else None)
        ws = self._ws(sc, base_jobs())
        plan = plan_shoot(ws, STATION_DOUBLE_FRAME)
        delivered_frames = {e.frame_id for e in plan.exposures}
        self.assertNotIn(2, delivered_frames)  # 补不齐
        self.assertIn(2, {u.frame_id for u in plan.undelivered})

    def test_variant_no_back_rejected_even_if_reading_ok(self) -> None:
        sc = build_variant_no_back_scene()
        rs = self._readings(sc)
        self.assertEqual(rs[(1, "redpup")].pose.name, "red_double")
        ws = self._ws(sc, no_back_jobs())
        plan = plan_shoot(ws, STATION_DOUBLE_FRAME)
        self.assertEqual(plan.exposures, [])
        self.assertTrue(all("服饰" in u.reason for u in plan.undelivered))

    def test_report_is_json_serializable(self) -> None:
        import json
        sc = build_example_scene()
        ws = self._ws(sc, base_jobs())
        plan = plan_shoot(ws, STATION_DOUBLE_FRAME)
        payload = json.loads(json.dumps(plan.to_report(), ensure_ascii=False))
        self.assertEqual(payload["station"], STATION_DOUBLE_FRAME)
        self.assertEqual(len(payload["exposures"]), len(plan.exposures))
        self.assertEqual(payload["delivery_order"], plan.delivery_order)


class TwoActTests(unittest.TestCase):
    """首幕正背同拍、次幕只要正面新姿势；首幕拍封后唯一实偶赶不上次幕。"""

    def _readings(self, scene):
        out = {}
        for fid, marks in scene.marks_by_frame.items():
            r = read_scene(scene.frames[fid].canvas, marks,
                           tuple(scene.patterns.values()), frame_id=fid, time_ms=fid * 10)
            for name, pr in r.puppets.items():
                out[(fid, name)] = pr
        return out

    def _ws(self, scene, jobs):
        return Worksheet(
            jobs=jobs, cameras=scene.cameras, puppets=scene.puppets,
            patterns=scene.patterns, binding=scene.binding,
            readings=self._readings(scene),
        )

    def test_act1_delivered_act2_rejected_on_seal(self) -> None:
        sc = build_two_act_scene()
        rs = self._readings(sc)
        self.assertEqual(rs[(0, "redpup")].pose.name, "act1")
        self.assertEqual(rs[(1, "redpup")].pose.name, "act2")
        ws = self._ws(sc, two_act_jobs())
        plan = plan_shoot(ws, STATION_DOUBLE_FRAME)
        # 首幕正背同拍交付两张。
        self.assertEqual({e.frame_id for e in plan.exposures}, {0})
        self.assertEqual(len(plan.exposures), 2)
        group = plan.exposures[0].group
        self.assertEqual({e.group for e in plan.exposures}, {group})
        # 次幕因唯一实偶不能换到 act2 拒收，且追到首幕那次纸封。
        und = [u for u in plan.undelivered if u.frame_id == 1]
        self.assertEqual(len(und), 1)
        self.assertIn("frame 0", und[0].trace)
        self.assertEqual(verify_plan(plan, ws), [])

    def test_turntable_cannot_do_act1_pair(self) -> None:
        sc = build_two_act_scene()
        ws = self._ws(sc, two_act_jobs())
        plan = plan_shoot(ws, STATION_SINGLE_TURNTABLE)
        und0 = [u for u in plan.undelivered if u.frame_id == 0]
        self.assertEqual(len(und0), 1)
        self.assertIn("无法同拍", und0[0].reason)


class CrowdSceneTests(unittest.TestCase):
    """多偶 + 合并 + 同款备份；规模在 5 偶/12 格/3 相机内。"""

    def _readings(self, scene):
        out = {}
        for fid, marks in scene.marks_by_frame.items():
            r = read_scene(scene.frames[fid].canvas, marks,
                           tuple(scene.patterns.values()), frame_id=fid, time_ms=fid * 10)
            for name, pr in r.puppets.items():
                out[(fid, name)] = pr
        return out

    def test_scale_limits(self) -> None:
        sc = build_crowd_scene()
        self.assertLessEqual(len(sc.puppets), 5)
        self.assertLessEqual(len(sc.frames), 12)
        self.assertLessEqual(len(sc.cameras), 3)

    def test_frame0_all_readable(self) -> None:
        sc = build_crowd_scene()
        rs = self._readings(sc)
        self.assertEqual(rs[(0, "redpup")].pose.name, "crowd_red")
        self.assertEqual(rs[(0, "greenpup")].pose.name, "crowd_green")
        self.assertEqual(rs[(0, "bluepup")].pose.name, "crowd_blue")

    def test_frame1_reds_merge_no_pattern_for_either(self) -> None:
        sc = build_crowd_scene()
        rs = self._readings(sc)
        self.assertTrue(rs[(1, "redpup")].merged)
        self.assertTrue(rs[(1, "redpup2")].merged)
        self.assertIsNone(rs[(1, "redpup")].pose)
        self.assertIsNone(rs[(1, "redpup2")].pose)

    def test_borrow_backup_for_blue(self) -> None:
        sc = build_crowd_scene()
        rs = self._readings(sc)
        # 蓝偶在两格都不许换偶时会用绑定 b-real；允许换偶时才能借 b-real2。
        jobs = (
            FrameJob(0, 0, "bluepup", "crowd_blue", ("cf", "cb"), simultaneous=True),
        )
        ws = Worksheet(jobs=jobs, cameras=sc.cameras, puppets=sc.puppets,
                       patterns=sc.patterns, binding=sc.binding, readings=rs)
        plan = plan_shoot(ws, STATION_DOUBLE_FRAME)
        self.assertTrue(all(e.puppet == "b-real" for e in plan.exposures))
        self.assertEqual(verify_plan(plan, ws), [])


if __name__ == "__main__":
    unittest.main()
