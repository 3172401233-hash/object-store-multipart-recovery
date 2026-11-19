"""对象存储 API 的本地抽象。

规划器只依赖 ObjectStoreGateway 接口，不连接真实服务；
测试与离线演练使用 InMemoryObjectStore。
"""

import abc
from datetime import datetime
from typing import Dict, List, Optional

from .models import ServerPart, UploadSession


class ObjectStoreGateway(abc.ABC):
    """对象存储分片上传接口的抽象。""" 

    @abc.abstractmethod
    def get_session(self, upload_id: str) -> Optional[UploadSession]:
        """返回上传会话；upload_id 失效时返回 None。"""

    @abc.abstractmethod
    def list_parts(self, upload_id: str) -> List[ServerPart]:
        """列出服务端已上传的分片。""" 

    @abc.abstractmethod
    def abort(self, upload_id: str) -> None:
        """中止上传并清理服务端引用。""" 


class InMemoryObjectStore(ObjectStoreGateway):
    """内存实现，用于离线规划与单元测试。""" 

    def __init__(self) -> None:
        self._sessions: Dict[str, UploadSession] = {}
        self._parts: Dict[str, List[ServerPart]] = {}
        self.aborted: List[str] = []

    def add_session(self, session: UploadSession) -> None:
        self._sessions[session.upload_id] = session
        self._parts.setdefault(session.upload_id, [])

    def add_part(self, upload_id: str, part: ServerPart) -> None:
        self._parts.setdefault(upload_id, []).append(part)

    def get_session(self, upload_id: str) -> Optional[UploadSession]:
        return self._sessions.get(upload_id)

    def list_parts(self, upload_id: str) -> List[ServerPart]:
        return list(self._parts.get(upload_id, []))

    def abort(self, upload_id: str) -> None:
        self._sessions.pop(upload_id, None)
        self._parts.pop(upload_id, None)
        self.aborted.append(upload_id)

    @staticmethod
    def is_expired(session: UploadSession, now: datetime) -> bool:
        return session.expires_at is not None and now >= session.expires_at
