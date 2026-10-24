"""初始化 SmartVoyage 本地演示数据。

运行位置：
    从 ``04-代码`` 目录执行。

常用命令：
    python -m ai01_SmartVoyage.scripts.init_demo_data --reset-db
    python -m ai01_SmartVoyage.scripts.init_demo_data --reset-db --with-vectors

数据范围：
    - MySQL：票务、天气、租车、保险、记忆表等演示数据。
    - Milvus：旅游团向量检索数据，仅在指定 ``--with-vectors`` 时初始化。

注意：
    ``--reset-db`` 会执行 create_all_tables.sql，其中包含 DROP DATABASE。
    该脚本只用于本地教学/演示环境，不应指向生产数据库。
"""

import argparse
from pathlib import Path

import mysql.connector

from ..infrastructure.config import Config

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SQL_DIR = PACKAGE_ROOT / "sql"


def iter_sql_statements(sql_text: str):
    """把 SQL 文件内容拆成可逐条执行的语句。

    当前 SQL 文件结构简单，注释行以 ``--`` 开头，语句以分号结尾。
    这里没有引入额外 SQL 解析依赖，保持初始化脚本轻量。
    """
    buffer = []
    for raw_line in sql_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("--"):
            continue
        buffer.append(raw_line)
        if line.endswith(";"):
            statement = "\n".join(buffer).strip().rstrip(";")
            buffer.clear()
            if statement:
                yield statement
    remaining = "\n".join(buffer).strip()
    if remaining:
        yield remaining


def execute_sql_file(connection, sql_path: Path):
    """按顺序执行一个 SQL 文件中的所有语句。"""
    print(f"[sql] executing {sql_path.name}")
    with sql_path.open("r", encoding="utf-8") as sql_file:
        statements = list(iter_sql_statements(sql_file.read()))

    cursor = connection.cursor()
    try:
        for statement in statements:
            cursor.execute(statement)
        connection.commit()
    finally:
        cursor.close()


def init_mysql(reset_db: bool):
    """初始化 MySQL 测试数据。

    reset_db=True 时先重建库表，再插入测试数据；否则只执行插入脚本。
    """
    conf = Config()
    connection = mysql.connector.connect(
        host=conf.host,
        port=conf.port,
        user=conf.user,
        password=conf.password,
        connection_timeout=conf.db_connect_timeout,
    )
    try:
        if reset_db:
            execute_sql_file(connection, SQL_DIR / "create_all_tables.sql")
        execute_sql_file(connection, SQL_DIR / "insert_may01_data.sql")
    finally:
        connection.close()


def init_vectors():
    """初始化旅游团 Milvus 向量数据。

    该步骤依赖 Milvus 已启动，并且 DASHSCOPE_API_KEY 可用于 embedding 接口。
    """
    from ..sql import init_tour_group_rag

    collection = init_tour_group_rag.create_tour_group_collection()
    init_tour_group_rag.insert_tour_groups(collection)
    init_tour_group_rag.test_search(collection)


def main():
    """解析命令行参数并按需初始化 MySQL/Milvus。"""
    parser = argparse.ArgumentParser(description="Initialize SmartVoyage demo data.")
    parser.add_argument(
        "--reset-db",
        action="store_true",
        help="Drop and recreate the travel_rag database before inserting demo data.",
    )
    parser.add_argument(
        "--with-vectors",
        action="store_true",
        help="Initialize Milvus tour-group vector data after MySQL data.",
    )
    parser.add_argument(
        "--skip-mysql",
        action="store_true",
        help="Skip MySQL initialization and only run the optional vector step.",
    )
    args = parser.parse_args()

    if not args.skip_mysql:
        init_mysql(reset_db=args.reset_db)
    if args.with_vectors:
        init_vectors()
    print("[done] SmartVoyage demo data initialized.")


if __name__ == "__main__":
    main()
