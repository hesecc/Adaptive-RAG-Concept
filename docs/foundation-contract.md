# 无模型基座实施契约

本轮只实现可运行基座，不连接云平台、模型API或Laya，不读取模型密钥，不下载模型。默认输出evidence_preview，answer=null，model_calls=0。

核心使用Python 3.11+，本地SQLite与内容寻址文件对象存储。HTTP使用FastAPI，CLI和HTTP调用同一应用接口。后续MySQL/Milvus/对象存储及云模型通过角色适配器替换，不提前实现云客户端。

## 当前共享接口

数据以adaptive_rag/domain.py为准，协议以ports.py为准。Snapshot含tenant、generation和document→revision不可变清单；发布使用expected_generation进行CAS，首次发布预期None。stage只暂存；publish事务合并最新清单中的单个文档版本，旧快照保留。删除产生新代并实时撤销历史文档访问；rollback创建新代但不能恢复撤销权限。ACL空集合表示租户内公开；主体或groups命中可读取。存储接口必须按实时ACL及tenant复核，调用者伪造Snapshot也不得扩大权限。

同一原文+元数据+处理配置得到相同revision/job。相同source version改变处理配置可以重新构建。失败不能推进当前发布指针；所有SQLite操作使用独立连接和事务，不跨线程共享连接。

## Application公共接口（API/CLI调用）

- RagApplication(settings: Settings, registry: PluginRegistry | None = None)
- ingest(request: IngestRequest) -> IngestResult
- publish(tenant_id: str, job_id: str, expected_generation: str | None) -> Snapshot
- delete_document(tenant_id: str, document_id: str, expected_generation: str | None) -> Snapshot
- rollback(tenant_id: str, generation: str, expected_generation: str | None) -> Snapshot
- set_access(tenant_id: str, document_id: str, allowed_subjects: tuple[str,...], revoked: bool=False) -> None
- async query(request: QueryRequest) -> QueryResult
- get_evidence(access: AccessContext, generation: str, evidence_id: str) -> Evidence
- get_job(tenant_id: str, job_id: str) -> dict
- get_trace(tenant_id: str, trace_id: str) -> dict
- capabilities() -> dict

Settings位于adaptive_rag/config.py，可Settings(data_dir=Path(...))，load_settings(path: str|Path|None=None)；没有配置使用安全本地默认。配置未知键必须报错。app对象持有store、objects、trace_sink三个可调用的存储实现。注册插件按角色明确选择，不从用户数据动态import。

## 运行与验收边界

真实实现文本/Markdown/HTML安全解析、清洗父子块、持久化与快照、BM25、RRF、规则计划、父块证据、出处核验、可持久化trace、离线eval。dense/rewrite/multiquery/HyDE/model rerank/generation/semantic judge/Laya默认disabled。禁用≠空结果≠错误，trace可区分。

API从Bearer token的可信映射获得tenant/subject/groups，禁止请求正文自报身份；写操作要求管理员权限。默认只允许loopback监听并需显式配置token。CLI是可信本地管理入口，tenant参数不等于远程认证。

示例、测试替身不得伪装为已接模型。测试替身仅测试装配注入。最少验证重启、重复导入、并发CAS、旧快照、撤权、跨租户、空文/空检索/错误插件/超时、引用回源、所有插槽替换、零网络/零模型调用、eval无gold分母、CLI/HTTP同逻辑。
