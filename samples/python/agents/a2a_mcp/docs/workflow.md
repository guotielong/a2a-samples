这段代码定义了一个用于工作流（workflow）管理的基础结构，主要包括状态枚举和工作流节点类。下面是详细解释：

1. **导入部分**  
   - 导入了常用的库（如 `json`, `logging`, `uuid`），以及异步、枚举、图结构、HTTP 客户端等相关模块。
   - 从本地模块和包中导入了类型、工具函数和客户端。

2. **Status 枚举类**  
   - `Status` 是一个枚举类，表示工作流节点的状态，包括：  
     - `READY`：准备好  
     - `RUNNING`：运行中  
     - `COMPLETED`：已完成  
     - `PAUSED`：已暂停  
     - `INITIALIZED`：已初始化

3. **WorkflowNode 类**  
   - 表示工作流图中的一个节点，每个节点封装了一个具体任务（如查找 agent 或调用 agent 能力）。
   - 属性包括：
     - `id`：唯一标识符（UUID）
     - `node_key`：节点键（可选）
     - `node_label`：节点标签（可选）
     - `task`：节点要执行的任务
     - `results`：任务执行结果，初始为 None
     - `state`：节点当前状态，初始为 READY

**总结**：  
这部分代码为后续的工作流编排和节点任务执行提供了基础的数据结构和状态管理。


我要读取 workflow.py 的其余内容来找出生成工作流的代码路径（目的：定位负责“动态生成工作流”的函数和逻辑），然后解释其输入/输出和流程。接下来我将读取文件其余行。 我将搜索代码库中如何创建/填充/运行 `WorkflowGraph`（查找 `WorkflowGraph`, `add_node(`, `add_edge(`, `run_workflow(`），以找到动态生成工作流的调用点并据此解释整体流程。预期结果：返回使用这些符号的文件和行片段。 我要打开 orchestrator_agent.py 以查看它如何动态生成工作流。接下来读取该文件。 我将解析并说明代码中“如何动态生成工作流”的路径与运行时行为，接下来给出要点检查表并逐步解释代码实现细节与数据流。

检查表（从你的请求抽取的需求）
- [x] 找到生成/扩展工作流的代码位置（文件与函数）。
- [x] 说明初始图如何创建（planner 节点）。
- [x] 说明 planner 返回的 artifact 如何被解析并用来动态插入任务节点与边。
- [x] 说明节点属性（query/task_id/context_id）如何传播以供执行。
- [x] 说明执行、暂停（input_required）和恢复的流程。
- [x] 指出关键函数/类型和典型边界情况。

进度更新（我做了什么）
- 已读取并分析 workflow.py 和 orchestrator_agent.py 的相关实现，定位到动态生成图的核心逻辑并确认数据流和控制流。

## 核心结论（一句话）
工作流实现以一个最初只含“planner”节点的有向图开始；当 planner 返回包含任务列表的 artifact（名为 `PlannerAgent-result`）时，`OrchestratorAgent` 解析该 artifact 的 `tasks`，为每个 task 动态创建 `WorkflowNode` 并把它们连成子图，然后重新从第一个新插入的任务节点继续执行，直到图完成或被暂停要求用户输入再恢复。

## 关键代码位置
- 构建/执行图与节点类型：workflow.py
  - `WorkflowNode`：封装单个任务执行（有 `run_node`、agent 查找等）
  - `WorkflowGraph`：图结构、`add_node`/`add_edge`、`run_workflow`（按拓扑顺序运行子图）
- 动态生成与控制逻辑：orchestrator_agent.py
  - `OrchestratorAgent.stream()`：初始创建 planner 节点，接收 planner artifact，解析 tasks 并调用 `add_graph_node` 动态插入节点与边，控制循环/重启执行、处理暂停与 resume，最终汇总结果生成 summary。

## 详细步骤（执行与动态生成流程）
1. 初始请求进入 `OrchestratorAgent.stream(query, context_id, task_id)`。  
   - 如果 `self.graph` 为 None，就创建新的图：`self.graph = WorkflowGraph()`，并调用 `add_graph_node(..., node_key='planner', node_label='Planner')` 来插入 planner 节点（graph 初始仅含此节点）。
2. 节点属性设置：节点在图中存储属性 `query`（通过 `graph.add_node(node.id, query=node.task)`）以及随后通过 `set_node_attributes` 设置 `task_id`、`context_id`、和可能更新的 `query`。
3. 运行图：`OrchestratorAgent` 调用 `self.graph.run_workflow(start_node_id=...)`。  
   - `WorkflowGraph.run_workflow`：选取起始子图（从 start node 的所有后代），按拓扑排序遍历这些节点，调用每个节点的 `run_node(query, task_id, context_id)` 来执行任务，并以流（async generator）逐块 yield 执行结果。
4. 节点执行细节（`WorkflowNode.run_node`）：
   - 若 `node_key == 'planner'`，通过 MCP client 获取 planner 的 `AgentCard`（resource://agent_cards/planner_agent），否则通过 `find_agent_for_task()` 按 `task` 查找合适 agent。
   - 使用 `A2AClient` 发起 `SendStreamingMessageRequest`（把 `message` 中的 `query`、`taskId`、`contextId` 放入），以流式接收返回的 chunk。
   - 当 chunk 类型为 `TaskArtifactUpdateEvent` 时，将 `artifact` 存到 `self.results`（节点级别），并把 chunk 传回上层（yield）。
5. Planner 返回后（在 `OrchestratorAgent.stream()` 的循环中被发现）：
   - 当接收到 `TaskArtifactUpdateEvent` 并且 `artifact.name == 'PlannerAgent-result'` 时，读取 `artifact.parts[0].root.data`，该数据包含 `tasks`（列表）和可选的 `trip_info`。
   - 对 `artifact_data['tasks']` 做循环：为每个 task 创建一个新的 `WorkflowNode`（`add_graph_node(...)`），并通过提供 `node_id=current_node_id` 把新节点连接为当前子图的后继（`add_edge` 在内部被调用）；更新 `current_node_id` 为最新节点 id。
   - 第一个新节点会触发 `should_resume_workflow = True` 并把 `start_node_id` 指向该新节点，这样外层的 while loop 会重新启动 `run_workflow`，从新插入的节点开始执行。
6. 暂停与恢复：
   - 在节点执行中，如果服务返回 `TaskStatusUpdateEvent`，且状态为 `TaskState.input_required` 且包含 `context_id`，则 `WorkflowGraph.run_workflow` 会把该节点 `node.state = Status.PAUSED` 并设置 `self.paused_node_id = node.id`，同时 `self.state = Status.PAUSED`，并中断当前 run（外层会停止遍历）。
   - 在后续的 `stream()` 调用里，如果 `self.graph.state == Status.PAUSED`，`start_node_id` 被设置成 `self.graph.paused_node_id`，并把新的用户 `query` 写入该节点的属性（`set_node_attributes`），然后再次调用 `run_workflow` 恢复执行。
   - 如果 orchestrator 能在内部回答所需问题（`answer_user_question` -> 使用生成模型尝试回答），则自动设置该节点的 `query` 并将 `should_resume_workflow = True` 来自动恢复。
7. 完成与汇总：
   - 当所有节点都进入 `COMPLETED` 且图运行结束，`OrchestratorAgent` 调用 `generate_summary()`（用 GenAI 模型基于 `self.results` 生成摘要），然后 `clear_state()` 并把摘要作为最终结果 yield 回去。

## 数据形状与属性传播（重要字段）
- 每个图节点在 `nx` 节点属性里含：
  - `query`（用于发消息的文本）  
  - `task_id`（外部任务 id，用于调用 agent）  
  - `context_id`（会话/上下文 id，用于 agent 间关联）
- Planner artifact：`artifact.parts[0].root.data` 预期包含：
  - `tasks`: 列表，每个元素至少包含 `description`（用于创建任务节点的 `query`）  
  - 可选 `trip_info`（被保存在 `self.travel_context`）
- Messaging payload: `message` 包含 `role`, `parts`（文本），`messageId`, `taskId`, `contextId`（见 `WorkflowNode.run_node` 构建的 `payload`）。

## 关键类型/事件（控制流依据）
- `TaskArtifactUpdateEvent`：planner 返回任务列表时使用，触发图扩展。
- `TaskStatusUpdateEvent`：用于检测 `TaskState.input_required`（暂停）或 `TaskState.completed`（继续/完成）。
- `TaskState.input_required`：触发暂停并记录 `paused_node_id`。
- `A2AClient` 的 streaming chunks：驱动协程式逐块返回并让 orchestrator 能流式处理。

## 输入/输出契约（2-4 行）
- 输入：用户 query（文本）、`context_id`（会话）、`task_id`（任务上下文）。  
- 输出：流式的执行 chunk（由 agent 返回的事件），最终返回一个汇总文本（当图完成时）。  
- 错误模式：找不到 agent/resource 会导致 `find_agent_for_task` 或 `get_planner_resource` 抛错或返回 None（当前代码没有显式重试策略）。

## 常见边界/注意点
- Planner artifact 必须包含 `tasks`，否则不会插入新节点。artifact 数据格式不匹配会被忽略或导致异常解析。  
- graph 在 paused 状态下不会继续 yield 节点执行的 chunk，直到外部再次调用 `stream` 并传入新的 query 来 resume。  
- 若 planner 本身不是图的一部分（已在注释中提到的 TODO），可以把 planner 作为外部步骤来生成静态子图，这会简化重启逻辑。  
- 并发调用同一 `context_id` 的 `stream` 需要注意竞态（当前实现没有锁）。

## 结论与下一步建议
- 当前动态生成工作流的核心在于：planner 节点产生描述任务的 artifact -> orchestrator 解析 artifact 并动态插入节点与边 -> 从第一个新节点继续执行。  
- 若你想我做进一步工作，我可以：
  - 把 planner artifact 的预期 JSON schema 抽取并写成断言/验证代码（降低解析出错概率）。  
  - 添加重试或错误处理（找不到 agent、null artifact）。  
  - 为 pause/resume 增加并发保护（锁或队列）。  

如果你希望，我现在可以：1) 提取 planner artifact 的示例结构并生成验证函数；或 2) 注释代码中关键点并生成一份简短的流程图（文本形式）。你想先做哪一个？