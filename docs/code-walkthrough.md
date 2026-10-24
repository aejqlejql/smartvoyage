# SmartVoyage 代码走读讲义

> 历史 Agent 层代码导航。新版 API 需要登录，应用记忆改为独立会话持久化；当前入口和模拟交易实现见 [README](../README.md) 及 [个人项目讲解](个人项目讲解.md)。

本文用于带学生按主链路阅读代码。建议先看 `docs/student-handoff-flow.md`，再按本文进入源码。

## 1. 从 HTTP 入口开始

代码入口：

- `api/server.py`
- `static/index.html`

讲解顺序：

1. 浏览器访问 `/`，FastAPI 返回前端页面。
2. 前端调用 `/api/chat` 或 `/api/chat/stream`。
3. API 层不做复杂业务，只把用户消息交给 `ChatService`。

学生需要理解：

- API 层是“门口”，不是“大脑”。
- 真正的意图识别、Agent 调用、总结都不应该写在路由函数里。

课堂提问：

```text
为什么 api/server.py 不直接判断用户是在查天气还是查票？
```

参考答案：这样可以保持 HTTP 层简单，业务编排集中在 ChatService 和 LangGraph，后续换前端或接入其他客户端时不用重写业务逻辑。

## 2. 进入 ChatService

代码入口：

- `app/chat/service.py`

重点看：

- `ChatService.__init__()`
- `ChatService.chat()`
- `ChatService.intent_agent()`
- `ChatService._call_agent_intent()`

讲解顺序：

1. `__init__()` 装配 LLM、Memory、AgentRegistry、TraceRecorder、CollaborationEngine、LangGraph。
2. `chat()` 把请求交给 `SmartVoyageWorkflowGraph`。
3. `intent_agent()` 识别用户意图，把自然语言改写成工具查询。
4. `_call_agent_intent()` 根据意图路由到 A2A 专家 Agent。

学生需要理解：

- `ChatService` 是“装配层”和“能力门面”。
- 它知道有哪些能力，但不应该把所有流程状态都写死在自己里面。
- `_call_agent_intent()` 是主服务和专家 Agent 的关键边界。

课堂提问：

```text
为什么 ChatService 还需要 AgentRegistry，而不是直接写死 5005/5006/5007？
```

参考答案：AgentRegistry 把 Agent 地址和能力声明集中管理，后续新增 Agent 或改部署地址时，主流程不需要大改。

## 3. 阅读 LangGraph 工作流

代码入口：

- `core/graph/state.py`
- `core/graph/workflow.py`

重点看：

- `SmartVoyageWorkflowGraph._intent_node()`
- `SmartVoyageWorkflowGraph._route_node()`
- `SmartVoyageWorkflowGraph._direct_node()`
- `SmartVoyageWorkflowGraph._react_node()`
- `SmartVoyageWorkflowGraph._collaborative_node()`
- `SmartVoyageWorkflowGraph._finalize_node()`

讲解顺序：

1. 每次请求先创建 `SmartVoyageState`。
2. `intent` 节点识别意图并改写查询。
3. `route` 节点决定 follow_up、direct、ReAct、collaborative 或 error。
4. `plan` 节点进一步选择 direct 或 ReAct。
5. 执行完成后由 `finalize` 节点记录 trace 和记忆。

学生需要理解：

- LangGraph 负责“流程推进”。
- ChatService 提供能力，图节点决定什么时候调用。
- 状态字段集中在 `SmartVoyageState`，每个节点只返回自己的状态更新。
- `StateGraph` 声明节点，`add_edge()` 声明固定边，`add_conditional_edges()` 声明路由，
  `compile()` 生成可执行工作流。

课堂提问：

```text
direct、ReAct、collaborative 三种模式分别适合什么问题？
```

参考答案：

- direct：简单单意图，例如查天气。
- ReAct：普通多步骤任务，例如先查天气再决定订票。
- collaborative：综合旅行方案，例如预算、天气、交通、保险同时约束。

## 4. 阅读 A2A 路由

代码入口：

- `agents/registry.py`

重点看：

- `AgentRegistry.__init__()`
- `get_agent()`
- `get_agent_cards()`
- `get_available_tools()`

讲解顺序：

1. 读取环境变量中的 A2A URL。
2. 创建 `A2AClient`。
3. 向 `AgentNetwork` 注册专家 Agent。
4. 给上层提供 Agent Card 和工具列表。

学生需要理解：

- A2A 是主 Agent 和专家 Agent 的通信协议。
- Agent Card 是专家能力说明书。
- 主服务不直接调用数据库，而是通过 A2A 找专家。

## 5. 阅读专家 Agent

代码入口：

- `a2a_server/weather_server.py`
- `a2a_server/ticket_server.py`
- `a2a_server/trip_server.py`

重点看：

- `handle_task()`
- `run_*_agent()` / `query_weather()`
- `ExpertResult.from_text()`

讲解顺序：

1. A2A 服务收到主服务发来的 Task。
2. 专家 Agent 理解任务，并调用 MCP 工具。
3. MCP 返回 JSON 后，专家 Agent 包装成 `ExpertResult`。
4. 上游 ChatService 根据 `status` 决定是否进入总结。

学生需要理解：

- 专家 Agent 不应该自己编造业务数据。
- 它的职责是理解任务、选择工具、整理工具结果。
- 失败和无数据也要结构化返回。

课堂提问：

```text
为什么 A2A 返回结果要用 ExpertResult，而不是直接返回一段中文？
```

参考答案：中文文本不适合做可信边界，SummaryAgent 很难判断它是成功、无数据还是服务异常。结构化结果可以让代码层做硬约束。

## 6. 阅读 MCP 工具层

代码入口：

- `mcp_server/mcp_weather_server.py`
- `mcp_server/mcp_ticket_server.py`
- `mcp_server/mcp_trip_server.py`

重点看：

- `create_*_mcp_server()`
- `query_weather()`
- `query_train()` / `query_flight()`
- `query_car_rental()` / `query_insurance()` / `query_tour_group()`

讲解顺序：

1. MCP 服务注册工具函数。
2. A2A Agent 通过 MCP 调用工具。
3. 工具层查询 MySQL、API 或 Milvus。
4. 工具返回 JSON：`success/no_data/error`。

学生需要理解：

- MCP 是“工具协议层”。
- 业务数据查询应该在 MCP 工具层完成。
- MySQL 和 Milvus 的错误不能被伪装成正常业务结果。

## 7. 阅读可信输出边界

代码入口：

- `contracts/expert_result.py`
- `core/guardrails.py`
- `prompts/smartvoyage.py`

重点看：

- `ExpertResult`
- `contains_no_data_signal()`
- `all_results_unavailable()`
- 总结 prompt 中的禁止编造规则

讲解顺序：

1. MCP/A2A 返回结果统一收敛成 `ExpertResult`。
2. `status=no_data/error/input_required` 不进入自由总结。
3. SummaryAgent 只能总结成功数据。
4. 如果全部专家都不可用，协作链直接返回保守提示。

学生需要理解：

- Prompt 不是唯一安全边界。
- 代码层 guardrail 更稳定。
- 可信智能体必须知道什么时候不能回答。

## 8. 阅读强多智能体协作

代码入口：

- `core/collaboration_engine.py`
- `core/trace_recorder.py`

重点看：

- `_build_plan()`
- `_execute_task()`
- `_review()`
- `_summarize()`
- `record_collaboration()`

讲解顺序：

1. PlannerAgent 拆任务。
2. 多个专家 Agent 执行子任务。
3. CriticAgent 检查预算、天气、时间和完整性。
4. 必要时进行一轮修正查询。
5. SummaryAgent 生成最终方案。
6. TraceRecorder 保存完整协作链路。

学生需要理解：

- 复杂任务不是一个 Agent 从头答到尾。
- Planner、Expert、Critic、Summary 各自负责一段。
- Trace 是解释多智能体协作过程的重要证据。

## 9. 阅读部署和健康检查

代码入口：

- `docker-compose.yml`
- `Dockerfile`
- `core/health.py`
- `.env.docker.example`

重点看：

- Docker Compose 中的 `api`、`mysql`、`weather-a2a`、`weather-mcp` 等服务。
- `/api/health`
- `/api/health/dependencies`

讲解顺序：

1. 本机模式用 `scripts/start_all.py` 启动多个进程。
2. Docker 模式用 Compose 编排多个容器。
3. 健康检查接口定位 MySQL、MCP、A2A、Milvus 哪一层不可用。

学生需要理解：

- 页面能打开不代表业务链路都可用。
- “天气服务不可用”可能是 MySQL、MCP、A2A 任一层的问题。
- Milvus 是可选依赖，只影响旅游团 RAG。

## 10. 建议课堂练习

练习一：新增演示数据。

```text
给 weather_data 增加北京 2026-05-01 天气，让“北京5月1日天气”能查到真实结果。
```

练习二：新增专家 Agent。

```text
增加 HotelAssistant，支持酒店查询，并接入 AgentRegistry。
```

练习三：增强可信输出。

```text
让前端 Collaboration Trace 显示每个专家结果的 status/data/message。
```

练习四：增强健康检查。

```text
在前端页面显示 /api/health/dependencies 的结果。
```
