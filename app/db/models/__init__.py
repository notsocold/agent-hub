"""ORM 模型汇总。

Alembic autogenerate 依赖这里导入全部模型；新增模型文件后必须补充导入。
"""

from app.db.models.approval import Approval
from app.db.models.attachment import Attachment
from app.db.models.audit import AuditLog
from app.db.models.chat import Run, RunEvent
from app.db.models.conversation import Conversation, Message
from app.db.models.knowledge import KnowledgeChunk, KnowledgeDocument
from app.db.models.mcp import McpServer, McpTool, UserCapabilityOverride
from app.db.models.memory import Memory, ToolInvocation
from app.db.models.remote import A2aTask, RemoteAgent
from app.db.models.skill import Skill, SkillFile
from app.db.models.system import SystemSetting, UserSetting
from app.db.models.user import RefreshToken, User

__all__ = [
    "A2aTask",
    "Approval",
    "Attachment",
    "AuditLog",
    "Conversation",
    "KnowledgeChunk",
    "KnowledgeDocument",
    "McpServer",
    "McpTool",
    "Memory",
    "Message",
    "RefreshToken",
    "RemoteAgent",
    "Run",
    "RunEvent",
    "Skill",
    "SkillFile",
    "SystemSetting",
    "ToolInvocation",
    "User",
    "UserCapabilityOverride",
    "UserSetting",
]
