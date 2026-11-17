# 评测方案：TruLens 适配性评估与分层评测计划

研究日期：2026-11-17。资料范围：本仓库源码与现有研究报告，以及 TruLens、Ragas、DeepEval、RAGChecker、Phoenix、promptfoo 的上游 README 和 PyPI 元数据（均在 2026-11-17 读取）。本稿区分“已核实事实”“工程推论”“建议”。研究阶段没有安装评测依赖、没有调用付费模型、没有读取任何凭据，也没有改动代码。第 2 节描述的是研究当时的代码状态。

**术语假设：** 需求中的“turlens”按 TruLens（`truera/trulens`）理解。如果指的是其他工具，请告知后重新评估。

**进展（同日更新）：** 已按推荐方案实施 Phase 0 的前三项，用法和边界见 [评测](../evaluation.md)。

| Phase 0 项目 | 状态 |
|---|---|
| 1. `eval` 以受限身份运行（`--data-domain`，写入报告的 `parameters`） | 已完成；不传时仍是不受限 |
| 2. 权限矩阵 | 已完成：`adaptive-rag eval-permissions`，合成语料 10 份文档 × 9 种身份，另有 9 项整体检查，一处泄漏或缺失即退出码 3 |
| 3. 报告含 p50、p95 | 已完成：每题记录墙钟耗时，最近秩法汇总 |
| 4. 约 100 题的题库与双人标注 | **未开始**，等待你指定语料和标注人员（第 7 节） |
| 报告展示 | 新增只读页面 `/evaluation`，管理员从顶栏进入 |

Phase 1 起需要模型调用，仍未开始，也没有安装任何评测依赖。

## 1. 结论

1. **TruLens 技术上可以接入，但不适合作为本项目的评测主干。** 它擅长用 LLM 评审给“回答层”打分（上游 README 提到 groundedness、context relevance 与 relevance 评审），并提供 OpenTelemetry 追踪和批量评测接口。本项目当前最需要的几类评测不属于这一层：有 gold 的检索指标（已有）、按数据分类的越权断言、拒答质量、延迟分布。越权属于确定性不变量，应当用断言检查，不应交给 LLM 打分。
2. **更合适的做法是分层评测**，沿用 [RAG流程与实现实践报告](RAG流程与实现实践报告.md) 第 8 节的设计：确定性层（零成本）、规则打分层、可选的 LLM 评审层、人工抽检。TruLens 只作为 LLM 评审层的一个候选实现，先做隔离试点，用中文业务数据上的人工标注验证评审是否可信，再决定是否保留。
3. **现在不引入任何评测依赖，也不发起付费调用。** 第 6 节的 Phase 0 不需要模型调用，可以直接开始；Phase 1 和 Phase 2 需要你先确认预算和数据外发范围（第 7 节）。

## 2. 项目已有的评测能力（以源码为准）

| 能力 | 现状 | 位置 |
|---|---|---|
| 检索指标 | `adaptive-rag eval` 固定快照，对有 gold 的题计算 Hit@K、Recall@K、MRR@K、NDCG@K（二元相关）；支持文档级或片段级 gold，二选一；无 gold 的题只统计空检索率，并明确“不是拒答质量” | `adaptive_rag/evaluation.py` |
| 样例集 | 3 题，其中 1 题无 gold；只验证流程，不代表业务质量 | `examples/eval.jsonl` |
| 模型调用统计 | 每题记录 `model_calls`，报告汇总 | `adaptive_rag/evaluation.py` |
| 步骤耗时 | trace 内每个步骤有 `duration_ms`，整条查询也有总耗时；评测报告没有汇总 p50/p95（研究当时。现在报告按题记录墙钟耗时并汇总 p50/p95，口径是整条查询，不是 trace 的步骤耗时） | `adaptive_rag/pipeline.py:185`、`:516` |
| 运行期质量复核 | 覆盖度和支持度由配置的生成端点复核，属于流水线内部环节，不是离线评测；复核结果“有误差，且不构成访问授权” | `adaptive_rag/quality.py` |
| 可开关的环节 | `rewrite`、`multi_query`、`hyde`、`dense`、`gap`、`generator` 可单独停用，适合做消融 | `adaptive_rag/plugins.py:10-17` |

**缺口，均已在源码中核实：**

- **`eval` 无法以受限身份运行（研究当时；现已补上 `--data-domain`）。** 命令行构造 `AccessContext(args.tenant, args.subject, tuple(args.group))`，没有传 `data_domains`（`adaptive_rag/cli.py:99`）。`data_domains=None` 在 `domains_allowed` 中表示不受限（`adaptive_rag/data_access.py:21-23`）。因此现有评测只能看到管理员视角，不能验证“运营身份看不到财务资料”。Python API 的 `run_evaluation` 接受任意 `AccessContext`，只是命令行没有暴露。
- 没有回答层指标：事实支持率、引用精确率、要点正确率、拒答 precision/recall。当前默认的 `model_free` 模式也不生成答案。
- 没有评测题库。3 题不足以得出任何质量结论。

## 3. 候选工具对照

| 工具 | 上游文档的安装方式 | 版本与许可 | 对本项目的主要问题 |
|---|---|---|---|
| TruLens | `pip install trulens`，另装 `trulens-providers-openai`、`trulens-providers-litellm` 等评审供应商包 | 2.16.0（2026-10-19），MIT，Python ≥3.10 | 元包依赖 `trulens-dashboard`（含 streamlit、jupyter、plotly）和 `trulens-feedback`（含 scikit-learn、scipy、nltk）；`trulens-core` 另带 pandas、sqlalchemy、alembic、opentelemetry-sdk。供应商列表没有百炼。README 的准确率数据来自厂商和第三方基准，不是中文业务语料 |
| Ragas | `pip install ragas` | 0.4.4（2026-11-14），Apache-2.0；仓库已迁到 `vibrantlabsai/ragas` | PyPI 直接依赖 21 个，含 langchain、langchain-community、datasets、instructor。附带测试集生成功能（推论：生成题目需要把文档内容发给模型） |
| DeepEval | `pip install -U deepeval` | PyPI 4.3.4（2026-11-11），Apache-2.0，Python ≥3.10。GitHub 的“最新 release”标签是 python-v4.2.4，落后于 PyPI | pytest 风格，容易进 CI，G-Eval 可写自定义 rubric。但 README 说明 `deepeval login` 之后“所有测试用例自动记录”到其云平台。PyPI 直接依赖 29 个，含 pytest 系列插件、posthog、grpcio。默认是否上报分析数据 README 未说明 |
| RAGChecker | `pip install ragchecker`，另需 `python -m spacy download en_core_web_sm` | v0.1.9（2024-09-25），最后一次推送 2024-12-13，Apache-2.0 | claim 级蕴含诊断与本项目的 claims 加 evidence 结构最接近。但每题需要标准答案，示例使用 Bedrock 上的 Llama3 70B，流程偏英文，已近两年没有更新。适合借鉴思路，不适合引入依赖 |
| Phoenix | `pip install arize-phoenix`，或 Docker 镜像自托管 | Elastic License 2.0，README 带专利声明 | 是带服务端和界面的可观测平台，与现有 Trace 页面重叠；ELv2 不是 OSI 开源许可。默认收集基础网页分析，可用 `PHOENIX_TELEMETRY_ENABLED=false` 关闭 |
| promptfoo | `npm install -g promptfoo`，README 也列出 `pip install promptfoo` 与 `brew install promptfoo` | 0.125.1（2026-10-25），MIT。README 称已并入 OpenAI，仍保持 MIT 开源 | 适合对 HTTP 接口做黑盒回归和红队断言。会引入 Node 工具链，而确定性断言用现有 pytest 即可。可选 |

上表的安装命令均取自各项目 README，不是从注册表搜索结果反推。以后真正安装前，按依赖核验流程再核对一次：仓库链接、PyPI 的验证信息和当时的最新版本。

## 4. 为什么 LLM 评审不能当主干

1. **越权是确定性不变量。** “返回的每条证据必须属于该身份的授权集合”可以直接断言，零成本且无歧义。LLM 评审只看问题、证据和答案，无从判断一条证据“不该出现”。
2. **有 gold 的检索指标更便宜也更准确。** 不需要模型调用，结果可复现。TruLens 的 context relevance 是模型打分，不依赖 gold。它可以补充，不能替代。
3. **评审在中文业务数据上是否可信，没有证据。** TruLens README 引用的 groundedness 数据来自 LLM-AggreFact，context relevance 数据来自第三方文章（均为上游自述，我没有复现）。现有研究报告也要求“核对模型评审与人的一致性”。这一步必须在本项目数据上自己做。
4. **数据外发和副本问题。** 评审要把问题、证据、答案发给评审模型。运行期已经会把证据发给百炼（生成与复核），但评测会按整个题库批量重复，而且默认以管理员视角运行，证据覆盖全部分类，包括财务、人事。另外 TruLens 的依赖含 sqlalchemy 和 alembic，说明它用关系数据库保存记录；默认存放位置和保存内容我没有核实，需要试点前确认。如果它保存输入、输出和检索到的上下文原文，就会在权限体系之外多出一份分类数据的副本。现有 `eval` 的输出只含问题、ID 和指标，不含证据原文，TruLens 会改变这一点。
5. **评审有成本和波动。** 它适合看趋势，不适合作为发布门禁。门禁仍应以人工标注的指标和零越权为准，与现有研究报告第 8.3 节一致。

## 5. 推荐方案：四层评测

| 层 | 内容 | 是否调用模型 | 成本 |
|---|---|---|---|
| L0 确定性 | **权限矩阵**：身份（管理员、运营、财务、仅公共、未分类账号、已撤销资料）乘以问题集，断言每条证据的文档属于该身份的授权集合，一例越权即失败。**检索 gold 指标**（已有）。**引用结构**：每个 claim 的 `evidence_ids` 都在返回的证据中。**延迟**：按 trace 汇总 p50 和 p95 | 否 | 零 |
| L1 规则打分 | **要点命中**：题目标注 `required_points`（例如“7 天”“12 个月”这类数字、单位和条件），在答案中做字符串或数字匹配。**拒答 precision 和 recall**：区分“知识缺失”与“系统故障” | 是（只需被测系统自身的生成调用） | 与题量成正比 |
| L2 LLM 评审（可选） | 事实支持率、回答相关性。先在约 50 条人工标注回答上校准评审与人的一致率，达标后再用作趋势信号。TruLens 是候选实现之一，也可以复用 `quality.py` 的复核方式写最小评审 | 是 | 题量乘以指标数 |
| L3 人工抽检 | 失败案例复盘、评审与人工分歧仲裁、关键政策事实逐条核对 | 否 | 人力 |

沿用现有研究报告的口径：检索指标只在有 gold 的题上平均，无 gold 的题单独报告；越权、错 SKU、高风险政策事实要求零错误，一例即阻断发布；首轮验收目标属于待业务负责人确认的工程目标，不是已达到的指标。

## 6. 实施计划

| 阶段 | 内容 | 前置条件 | 验收 |
|---|---|---|---|
| Phase 0：确定性层（无模型调用、无数据外发） | 1. `eval` 增加可重复的 `--data-domain`，并把 `data_domains` 写入报告的 `parameters`；不传时保持现有“不受限”行为。2. 增加权限矩阵测试：用夹具语料（覆盖六类分类、未分类、已撤销、ACL）和多种身份，把现有用例级断言扩展到整个题集。3. 报告增加 p50、p95。4. 扩充首批题库至约 100 题，人工双人标注，按简单参数、口语指代、售后、多意图、精确型号、无答案、权限等分层 | 题库内容和标注人员由你指定。语料用演示或脱敏资料 | 矩阵测试在全部身份上零越权；报告含分层指标和延迟分布；题库有版本号和标注说明 |
| Phase 1：回答层规则打分（需你批准预算和外发） | 在云端模式下对题库跑 L1：要点命中、拒答 precision 和 recall、引用精确率。先跑 20 题，读取报告里的 `model_calls` 均值再外推总量，不凭估计 | 预算上限；使用测试租户；确认哪些语料允许发给模型 | 得到带样本数和差异数的基线，失败案例逐条归因 |
| Phase 2：评审试点（需批准；可选） | 在独立子项目（例如 `evals/`，自己的虚拟环境和锁文件）中评估 TruLens 评审：包装 `application.query`，对 Phase 1 中已人工标注的约 50 条回答比较评审与人工结论。主项目的 `uv.lock` 和生产镜像不变 | Phase 0 与 Phase 1 完成；评审通道已确认；一致率阈值在试点前写下（示例值 85%，待你确认） | 一致率达标则保留为趋势信号，否则放弃并记录原因 |
| Phase 3：消融 | 按研究报告第 8.3 节的实验 A 到 H，一次只改一个开关，用同题配对比较并报告样本数。`model_free` 模式只有 BM25 单路，稠密检索与重排的对比需要云端模型 | Phase 1 基线 | 每个开关给出收益、延迟和成本，不用单次小样本下结论 |

**隔离要求：** 评测依赖不进入生产镜像（镜像用 `uv sync --frozen --no-dev --extra remote` 构建）。如果把它们写进主项目的 `uv.lock`，依赖锁就会变化。部署记录显示，依赖锁变化后不能再用“复用上一镜像、只替换源码”的离线构建（`docs/deployment-server.md:96`、`:172`），发布成本明显增加。评测库（pandas、numpy、sqlalchemy 等）还可能与生产依赖产生版本约束冲突。独立子项目可以避免这两点。评测产物视同最高分类数据，放在已被 `.gitignore` 忽略的受控目录，不提交。

## 7. 需要你确认的事项

1. 是否同意“TruLens 只作为可选试点，不作主干”。
2. 评测语料：生产环境现有的 2 份文档目前未分类，只有管理员可见，建议不要用于外发评审。是否使用演示或脱敏语料？真实业务语料要发给模型，需要你明确授权。
3. Phase 1 和 Phase 2 的预算上限，以及评审模型：沿用百炼的 OpenAI 兼容接口，还是另选。
4. 谁来做 gold 标注，是否能安排双人标注和分歧仲裁。

## 8. 未核实事项

- TruLens 官网文档（trulens.org）本次无法访问，RAG triad 的完整定义、默认数据库位置、保存哪些字段、对 async 调用的支持，均未核对。
- TruLens 能否经 OpenAI 兼容供应商指向百炼，评审提示词对中文的适配程度，需要 Phase 2 的试点验证。
- Ragas、DeepEval 对中文的表现没有评估。
- 各工具 README 取自主分支，是可变文档，只代表 2026-11-17 的状态。

## 9. 来源

均为上游自述或注册表元数据，2026-11-17 读取。

- TruLens：[GitHub README](https://github.com/truera/trulens)、[PyPI trulens](https://pypi.org/project/trulens/)、[trulens-dashboard](https://pypi.org/project/trulens-dashboard/)、[trulens-feedback](https://pypi.org/project/trulens-feedback/)、[trulens-core](https://pypi.org/project/trulens-core/)
- Ragas：[GitHub README](https://github.com/vibrantlabsai/ragas)、[PyPI](https://pypi.org/project/ragas/)
- DeepEval：[GitHub README](https://github.com/confident-ai/deepeval)、[PyPI](https://pypi.org/project/deepeval/)
- RAGChecker：[GitHub README](https://github.com/amazon-science/RAGChecker)
- Phoenix：[GitHub README](https://github.com/Arize-ai/phoenix)
- promptfoo：[GitHub README](https://github.com/promptfoo/promptfoo)
- 本仓库：`adaptive_rag/evaluation.py`、`adaptive_rag/cli.py`、`adaptive_rag/data_access.py`、`adaptive_rag/plugins.py`、`adaptive_rag/pipeline.py`、`adaptive_rag/quality.py`、`docs/research/RAG流程与实现实践报告.md`
