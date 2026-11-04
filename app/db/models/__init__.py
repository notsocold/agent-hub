"""ORM 模型包：按领域拆分文件，禁止再出现单文件 db.py 堆全部模型。

规划文件（后续编码时按需创建）：
    user.py              users
    conversation.py      conversations
    message.py           messages
    run.py               runs（一次 LangGraph 图执行）
    agent.py             remote_agents（A2A Agent 注册表 + Agent Card 缓存）
    a2a_task.py          a2a_tasks（远程 A2A Task 生命周期）
    tool_invocation.py   tool_invocations（工具调用审计）
    knowledge.py         knowledge_documents / knowledge_chunks（pgvector）
    approval.py          approvals（高风险工具/远程调用的人工确认）
    audit.py             audit_logs

新增模型文件后，必须在本文件导入对应模块，否则 Alembic autogenerate 看不到。
例如：
    from app.db.models.user import User  # noqa: F401
"""
