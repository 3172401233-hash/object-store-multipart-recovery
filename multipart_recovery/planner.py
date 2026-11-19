"""恢复规划器：对比本地清单与服务端清单，生成续传/覆盖/重试/废弃计划。"""

import hashlib
from datetime import datetime
from typing import Dict, List, Optional, Sequence

from .models import (
    LocalPart,
    ObjectSummary,
    PlanAction,
    RecoveryPlan,
    ServerPart,
)
from .store import InMemoryObjectStore, ObjectStoreGateway


class ManifestError(ValueError):
    """本地清单不合法（重复编号、编号非递增、范围不连续等）。"""


class RecoveryPlanner:
    """根据本地与服务端状态生成恢复计划。""" 

    def __init__(self, store: ObjectStoreGateway, max_concurrency: int = 4) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency 必须 >= 1")
        self._store = store
        self._max_concurrency = max_concurrency

    def plan(
        self,
        upload_id: str,
        target: ObjectSummary,
        local_parts: Sequence[LocalPart],
        now: Optional[datetime] = None,
    ) -> RecoveryPlan:
        now = now or datetime.utcnow()

        # 1. 本地清单校验：重复编号必须显式报错，不能静默覆盖。
        ordered = self._validate_manifest(local_parts)

        session = self._store.get_session(upload_id)
        server_parts = self._store.list_parts(upload_id)

        # 2. 上传 ID 失效或会话过期：先清理引用，禁止 Complete。
        if session is None:
            return RecoveryPlan(
                upload_id=upload_id,
                action=PlanAction.ABORT,
                parts_to_delete=list(server_parts),
                errors=["上传 ID 失效：%s" % upload_id],
            )
        if InMemoryObjectStore.is_expired(session, now):
            return RecoveryPlan(
                upload_id=upload_id,
                action=PlanAction.ABORT,
                parts_to_delete=list(server_parts),
                errors=["上传已过期：%s" % upload_id],
            )

        plan = RecoveryPlan(upload_id=upload_id, action=PlanAction.RESUME)
        warnings = self._check_part_layout(ordered)
        plan.warnings.extend(warnings)

        # 尾部空块不参与上传，也不参与摘要对齐。
        effective = [p for p in ordered if p.size > 0]
        if len(effective) != len(ordered):
            plan.warnings.append("忽略尾部空块分片")

        server_by_number: Dict[int, ServerPart] = {}
        for sp in server_parts:
            # 服务端重复编号以最新一条为准，但记录告警。
            if sp.part_number in server_by_number:
                plan.warnings.append(
                    "服务端分片编号重复：%d" % sp.part_number
                )
            server_by_number[sp.part_number] = sp

        local_by_number = {p.part_number: p for p in effective}

        # 3. 逐分片对齐。
        for number in sorted(local_by_number):
            lp = local_by_number[number]
            sp = server_by_number.get(number)
            if sp is None:
                plan.parts_to_upload.append(lp)
            elif lp.etag is None:
                # 本地无法校验，标记待确认。
                plan.parts_to_recheck.append(sp)
            elif sp.etag == lp.etag and sp.size == lp.size:
                plan.reusable_parts.append(sp)
            else:
                plan.parts_to_overwrite.append(lp)

        # 4. 服务端多余分片需要废弃。
        for number in sorted(server_by_number):
            if number not in local_by_number:
                plan.parts_to_delete.append(server_by_number[number])

        # 5. 上传批次按并发数切分。
        pending = plan.parts_to_upload + plan.parts_to_overwrite
        plan.upload_batches = [
            [p.part_number for p in pending[i:i + self._max_concurrency]]
            for i in range(0, len(pending), self._max_concurrency)
        ]

        # 6. 只有全部分片可复用且整体摘要一致时才允许 Complete。
        aligned = (
            not plan.parts_to_upload
            and not plan.parts_to_overwrite
            and not plan.parts_to_recheck
            and not plan.parts_to_delete
        )
        if aligned:
            total_size = sum(p.size for p in effective)
            digest = self._combined_digest(plan.reusable_parts)
            if total_size != target.total_size:
                plan.errors.append(
                    "总大小不一致：本地 %d，目标 %d" % (total_size, target.total_size)
                )
            elif digest != target.digest:
                plan.errors.append("数据摘要不一致：%s != %s" % (digest, target.digest))
            else:
                plan.can_complete = True
                plan.action = PlanAction.COMPLETE

        return plan

    @staticmethod
    def _validate_manifest(local_parts: Sequence[LocalPart]) -> List[LocalPart]:
        ordered = sorted(local_parts, key=lambda p: p.part_number)
        seen = set()
        for part in local_parts:
            if part.part_number in seen:
                raise ManifestError(
                    "本地清单分片编号重复：%d" % part.part_number
                )
            seen.add(part.part_number)
        for prev, cur in zip(ordered, ordered[1:]):
            if cur.part_number != prev.part_number + 1:
                raise ManifestError(
                    "分片编号必须递增且连续：%d 之后出现 %d"
                    % (prev.part_number, cur.part_number)
                )
            if cur.offset != prev.offset + prev.size:
                raise ManifestError(
                    "分片范围不连续：分片 %d 结束于 %d，分片 %d 起始于 %d"
                    % (prev.part_number, prev.offset + prev.size,
                       cur.part_number, cur.offset)
                )
        if ordered and ordered[0].offset != 0:
            raise ManifestError("首个分片必须从偏移 0 开始")
        return ordered

    @staticmethod
    def _check_part_layout(ordered: Sequence[LocalPart]) -> List[str]:
        warnings: List[str] = []
        body = [p for p in ordered[:-1] if p.size > 0] if ordered else []
        if body:
            base = body[0].size
            for part in body[1:]:
                if part.size != base:
                    warnings.append(
                        "分片大小不一致：分片 %d 大小 %d，期望 %d"
                        % (part.part_number, part.size, base)
                    )
        if ordered and ordered[-1].size == 0:
            warnings.append("存在尾部空块：分片 %d" % ordered[-1].part_number)
        return warnings

    @staticmethod
    def _combined_digest(parts: Sequence[ServerPart]) -> str:
        """S3 风格：拼接各分片 ETag 后取 MD5，并附分片数。""" 
        ordered = sorted(parts, key=lambda p: p.part_number)
        raw = "".join(p.etag for p in ordered).encode("utf-8")
        return "%s-%d" % (hashlib.md5(raw).hexdigest(), len(ordered))
