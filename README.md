# object-store-multipart-recovery

恢复对象存储分片上传并对齐校验、续传和清理。

## 功能

对比本地清单与对象存储中已上传的分片，生成恢复计划：

- **续传（resume）**：服务端缺失的分片，按允许的并发数分批输出
- **覆盖（overwrite）**：ETag 或大小不一致的分片，需要重传
- **待确认（recheck）**：服务端存在但本地无法提供校验值的 Part
- **可复用（reuse）**：ETag 与大小均一致的分片
- **废弃（discard）**：服务端多余分片，或过期/失效上传的全部引用

只有所有分片可复用且整体数据摘要（S3 风格的分片 ETag 拼接 MD5）一致时，
计划才允许 `Complete`；过期上传或失效的上传 ID 会生成 `abort` 计划并先清理引用。

## 约束

- Part 编号必须递增且范围连续，重复编号抛出 `ManifestError`，不会被静默覆盖
- 非末尾分片大小不一致、尾部空块会记录告警；尾部空块不参与上传
- 摘要或总大小不符时禁止 Complete

## 存储 API 抽象

存储 API 已抽象为本地接口 `ObjectStoreGateway`
（`multipart_recovery/store.py`），规划器不连接真实服务：

- `get_session(upload_id)`：查询上传会话，ID 失效返回 `None`
- `list_parts(upload_id)`：列出服务端已上传分片
- `abort(upload_id)`：中止上传并清理引用

离线规划与测试使用内存实现 `InMemoryObjectStore`；
接入真实对象存储时只需实现同一接口。

## 使用

```python
from datetime import datetime, timedelta
from multipart_recovery import (
    InMemoryObjectStore, LocalPart, ObjectSummary,
    RecoveryPlanner, ServerPart, UploadSession,
)

store = InMemoryObjectStore()
store.add_session(UploadSession("u-1", expires_at=datetime.utcnow() + timedelta(hours=1)))
store.add_part("u-1", ServerPart(part_number=1, etag="etag1", size=100))

planner = RecoveryPlanner(store, max_concurrency=4)
plan = planner.plan(
    upload_id="u-1",
    target=ObjectSummary(total_size=200, digest="<combined-md5>-2"),
    local_parts=[
        LocalPart(part_number=1, offset=0, size=100, etag="etag1"),
        LocalPart(part_number=2, offset=100, size=100, etag="etag2"),
    ],
)
print(plan.summary())
```

## 测试

```bash
python -m unittest
```

覆盖断点续传、坏 ETag、缺片、重复 Part、过期上传、上传 ID 失效、
摘要不符、分片大小不一致、尾部空块与并发分批。
