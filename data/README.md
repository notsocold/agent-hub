# 知识库原始文档

放置待入库的政策、产品手册、FAQ 等 Markdown/TXT 文档。

- 文件名即文档标题（可选前置 front matter 覆盖）；
- 一级目录可作为 `category` 元数据；
- 修改文档后运行 `scripts/build_kb.py` 重新入库；
- 该目录只放源文件，不放生成的向量索引（向量在 PostgreSQL 中）。
