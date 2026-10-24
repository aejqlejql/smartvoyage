"""SmartVoyage 运维和演示脚本。

该包下的脚本面向本地教学演示和开发调试，不属于线上 HTTP 服务入口：

- ``start_all.py``：按 MCP -> A2A -> API 的顺序启动整套服务。
- ``init_demo_data.py``：初始化 MySQL 测试数据，并可选初始化 Milvus 向量数据。

脚本都假设从 ``04-代码`` 目录使用 ``python -m ai01_SmartVoyage.scripts.xxx``
方式运行，以保证包相对导入路径稳定。
"""
