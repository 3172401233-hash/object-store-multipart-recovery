import unittest
from datetime import datetime, timedelta

from multipart_recovery import (
    InMemoryObjectStore,
    LocalPart,
    ManifestError,
    ObjectSummary,
    PlanAction,
    RecoveryPlanner,
    ServerPart,
    UploadSession,
)

NOW = datetime(2026, 11, 19, 12, 0, 0)
FUTURE = NOW + timedelta(hours=1)
PAST = NOW - timedelta(hours=1)


def make_local(count, size=100, etag_prefix="etag"):
    return [
        LocalPart(
            part_number=i + 1,
            offset=i * size,
            size=size,
            etag="%s%d" % (etag_prefix, i + 1),
        )
        for i in range(count)
    ]


def make_server(count, size=100, etag_prefix="etag"):
    return [
        ServerPart(part_number=i + 1, etag="%s%d" % (etag_prefix, i + 1), size=size)
        for i in range(count)
    ]


def make_target(parts, total_size):
    digest = RecoveryPlanner._combined_digest(parts)
    return ObjectSummary(total_size=total_size, digest=digest)


class RecoveryPlannerTest(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryObjectStore()
        self.planner = RecoveryPlanner(self.store, max_concurrency=2)
        self.store.add_session(UploadSession(upload_id="u-1", expires_at=FUTURE))

    def run_plan(self, local, target, upload_id="u-1"):
        return self.planner.plan(upload_id, target, local, now=NOW)

    def test_resume_missing_parts(self):
        """断点续传：服务端只有前两个分片，其余需要续传。"""
        local = make_local(4)
        for sp in make_server(2):
            self.store.add_part("u-1", sp)
        target = make_target(make_server(4), total_size=400)

        plan = self.run_plan(local, target)

        self.assertEqual(plan.action, PlanAction.RESUME)
        self.assertFalse(plan.can_complete)
        self.assertEqual([p.part_number for p in plan.reusable_parts], [1, 2])
        self.assertEqual([p.part_number for p in plan.parts_to_upload], [3, 4])
        # 并发数为 2，两个待传分片应在同一批次。
        self.assertEqual(plan.upload_batches, [[3, 4]])

    def test_bad_etag_triggers_overwrite(self):
        """坏 ETag：服务端分片 2 校验值不一致，需要覆盖重传。"""
        local = make_local(3)
        server = make_server(3)
        server[1] = ServerPart(part_number=2, etag="corrupted", size=100)
        for sp in server:
            self.store.add_part("u-1", sp)
        target = make_target(make_server(3), total_size=300)

        plan = self.run_plan(local, target)

        self.assertFalse(plan.can_complete)
        self.assertEqual([p.part_number for p in plan.parts_to_overwrite], [2])
        self.assertEqual([p.part_number for p in plan.reusable_parts], [1, 3])
        self.assertEqual(plan.upload_batches, [[2]])

    def test_missing_local_part_breaks_continuity(self):
        """缺片：本地清单编号/范围不连续时直接报错。"""
        local = make_local(3)
        del local[1]
        local[1] = LocalPart(part_number=3, offset=200, size=100, etag="etag3")
        target = make_target(make_server(3), total_size=300)

        with self.assertRaises(ManifestError):
            self.run_plan(local, target)

    def test_duplicate_part_number_rejected(self):
        """重复 Part 编号不能被静默覆盖。"""
        local = make_local(2)
        local.append(LocalPart(part_number=2, offset=100, size=100, etag="other"))
        target = make_target(make_server(2), total_size=200)

        with self.assertRaises(ManifestError):
            self.run_plan(local, target)

    def test_expired_upload_aborts_and_cleans(self):
        """过期上传：先清理引用，禁止 Complete。"""
        self.store.add_session(UploadSession(upload_id="u-exp", expires_at=PAST))
        self.store.add_part("u-exp", ServerPart(part_number=1, etag="etag1", size=100))
        target = make_target(make_server(1), total_size=100)

        plan = self.run_plan(make_local(1), target, upload_id="u-exp")

        self.assertEqual(plan.action, PlanAction.ABORT)
        self.assertFalse(plan.can_complete)
        self.assertEqual([p.part_number for p in plan.parts_to_delete], [1])
        self.assertTrue(plan.errors)

    def test_invalid_upload_id_aborts(self):
        """上传 ID 失效：会话不存在时生成废弃计划。"""
        target = make_target(make_server(1), total_size=100)

        plan = self.run_plan(make_local(1), target, upload_id="u-gone")

        self.assertEqual(plan.action, PlanAction.ABORT)
        self.assertFalse(plan.can_complete)
        self.assertIn("失效", plan.errors[0])

    def test_digest_mismatch_blocks_complete(self):
        """摘要不符：即使全部分片可复用也不允许 Complete。"""
        local = make_local(2)
        for sp in make_server(2):
            self.store.add_part("u-1", sp)
        target = ObjectSummary(total_size=200, digest="wrong-digest")

        plan = self.run_plan(local, target)

        self.assertFalse(plan.can_complete)
        self.assertNotEqual(plan.action, PlanAction.COMPLETE)
        self.assertTrue(any("摘要" in e for e in plan.errors))

    def test_complete_when_all_aligned(self):
        """全部分片可复用且摘要一致时允许 Complete。"""
        local = make_local(3)
        server = make_server(3)
        for sp in server:
            self.store.add_part("u-1", sp)
        target = make_target(server, total_size=300)

        plan = self.run_plan(local, target)

        self.assertEqual(plan.action, PlanAction.COMPLETE)
        self.assertTrue(plan.can_complete)
        self.assertEqual(plan.parts_to_upload, [])
        self.assertEqual(plan.errors, [])

    def test_unverifiable_server_part_marked_recheck(self):
        """服务端存在但本地无法校验的 Part 标记为待确认。"""
        local = make_local(2)
        local[0] = LocalPart(part_number=1, offset=0, size=100, etag=None)
        for sp in make_server(2):
            self.store.add_part("u-1", sp)
        target = make_target(make_server(2), total_size=200)

        plan = self.run_plan(local, target)

        self.assertFalse(plan.can_complete)
        self.assertEqual([p.part_number for p in plan.parts_to_recheck], [1])
        self.assertEqual([p.part_number for p in plan.reusable_parts], [2])

    def test_extra_server_part_deleted(self):
        """服务端多余分片进入删除列表。"""
        local = make_local(1)
        for sp in make_server(2):
            self.store.add_part("u-1", sp)
        target = make_target(make_server(1), total_size=100)

        plan = self.run_plan(local, target)

        self.assertFalse(plan.can_complete)
        self.assertEqual([p.part_number for p in plan.parts_to_delete], [2])

    def test_inconsistent_part_size_warns(self):
        """分片大小不一致时产生告警。"""
        local = [
            LocalPart(part_number=1, offset=0, size=100, etag="etag1"),
            LocalPart(part_number=2, offset=100, size=50, etag="etag2"),
            LocalPart(part_number=3, offset=150, size=30, etag="etag3"),
        ]
        target = ObjectSummary(total_size=180, digest="x")

        plan = self.run_plan(local, target)

        self.assertTrue(any("大小不一致" in w for w in plan.warnings))

    def test_trailing_empty_part_ignored(self):
        """尾部空块不参与上传，只记录告警。"""
        local = make_local(1)
        local.append(LocalPart(part_number=2, offset=100, size=0, etag="empty"))
        self.store.add_part("u-1", ServerPart(part_number=1, etag="etag1", size=100))
        target = make_target(make_server(1), total_size=100)

        plan = self.run_plan(local, target)

        self.assertTrue(any("尾部空块" in w for w in plan.warnings))
        self.assertEqual(plan.parts_to_upload, [])
        self.assertTrue(plan.can_complete)

    def test_upload_batches_respect_concurrency(self):
        """待传分片按并发数分批。"""
        local = make_local(5)
        target = make_target(make_server(5), total_size=500)

        plan = self.run_plan(local, target)

        self.assertEqual(plan.upload_batches, [[1, 2], [3, 4], [5]])


if __name__ == "__main__":
    unittest.main()
