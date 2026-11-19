"""数据模型：本地分片、服务端分片、目标对象摘要与恢复计划。"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional


class PlanAction(Enum):
    """单个分片或整体上传的处理动作。"""

    RESUME = "resume"          # 续传：服务端缺失，需要上传
    OVERWRITE = "overwrite"    # 覆盖：ETag 不一致，需要重传
    RETRY = "retry"            # 重试：服务端存在但本地无法校验，待确认
    REUSE = "reuse"            # 可复用：ETag 一致
    DISCARD = "discard"        # 废弃：服务端多余或上传已过期
    COMPLETE = "complete"      # 允许 CompleteMultipartUpload
    ABORT = "abort"            # 中止并清理引用


@dataclass(frozen=True)
class LocalPart:
    """本地清单中的一个分片。

    etag 为 None 表示本地无法提供校验值（例如清单来自旧版本工具），
    对应分片只能标记为待确认，不能直接复用。
    """

    part_number: int
    offset: int
    size: int
    etag: Optional[str] = None


@dataclass(frozen=True)
class ServerPart:
    """对象存储中已上传的分片。""" 

    part_number: int
    etag: str
    size: int


@dataclass(frozen=True)
class ObjectSummary:
    """目标对象摘要：总大小与整体数据摘要（S3 风格的分片 ETag 拼接摘要）。"""

    total_size: int
    digest: str


@dataclass(frozen=True)
class UploadSession:
    """一次分片上传会话。""" 

    upload_id: str
    expires_at: Optional[datetime] = None


@dataclass
class RecoveryPlan:
    """恢复规划结果。""" 

    upload_id: str
    action: PlanAction
    can_complete: bool = False
    parts_to_upload: List[LocalPart] = field(default_factory=list)
    parts_to_overwrite: List[LocalPart] = field(default_factory=list)
    parts_to_recheck: List[ServerPart] = field(default_factory=list)
    reusable_parts: List[ServerPart] = field(default_factory=list)
    parts_to_delete: List[ServerPart] = field(default_factory=list)
    upload_batches: List[List[int]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def summary(self) -> Dict[str, object]:
        return {
            "upload_id": self.upload_id,
            "action": self.action.value,
            "can_complete": self.can_complete,
            "parts_to_upload": [p.part_number for p in self.parts_to_upload],
            "parts_to_overwrite": [p.part_number for p in self.parts_to_overwrite],
            "parts_to_recheck": [p.part_number for p in self.parts_to_recheck],
            "reusable_parts": [p.part_number for p in self.reusable_parts],
            "parts_to_delete": [p.part_number for p in self.parts_to_delete],
            "upload_batches": self.upload_batches,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }
