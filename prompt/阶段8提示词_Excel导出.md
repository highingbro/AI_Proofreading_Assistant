# 阶段8 开发提示词:Excel 导出(core/exporter.py)

> 使用方法:将分隔线内的全部内容粘贴给 Claude Code。
> 前置状态:阶段1~6 已完成并验收。阶段5 的 `classify_issues()` 产出 `ClassifiedResult`(已分层、已排序、stats 与 records 表字段对齐)。阶段6 的 `core/workflow.py::persist_result()` 已经把一轮校对的 record(含全部统计字段)与逐条 issue 写入 SQLite(`records`/`issues` 两张表),采纳/拒绝状态由 `set_issue_status()` 实时回写 `issues.status`/`reject_reason`。`core/exporter.py` 目前只有函数签名(`export_issues_to_excel(record_id) -> Path`,函数体 `raise NotImplementedError`)。
> **开发顺序说明**:本项目对框架文档默认的 6→7→8 顺序做了调整,阶段8(Excel导出)排在阶段7(对话追问)之前先做——因为项目定位"替代手动整理Excel"这句话,阶段8 才是真正落地的那一半(阶段7 的追问是锦上添花)。这一调整已在 CLAUDE.md 记录,与本阶段无冲突:框架文档里阶段8 本就只依赖阶段6,不依赖阶段7。

---

接上一阶段。标准校对流已能把一轮结果落库(record + 逐条 issue,含采纳/拒绝状态)。现在实现**阶段8:Excel 导出模块(core/exporter.py)**,参见框架文档第 8 章(输出格式规范)、第 7 章(流程记录字段表)、第 10 章阶段8 行验收标准。本阶段是纯本地文件生成,不调用 LLM、不耗 API 额度。

## 模块定位(为什么这层是"替代手工"的关键一半)

项目定位是替代"手动拖文档进大模型网页版 + **手动整理 Excel**"。阶段6 解决了前半句(网页展示、逐条标记),本阶段解决后半句:把库里已标好分层/优先级/处理状态的问题清单,一键导出成符合出版校对习惯的 Excel(按页码排序、高优先级标红、引文类标色),直接可交付归档,不用再人肉抄进表格。

## 关键设计决策(实现前先对齐,不要臆测)

### 决策1:导出数据从数据库读,不从内存 `ClassifiedResult` 读

`export_issues_to_excel(record_id)` 的唯一入参是 `record_id`,数据全部通过 `db.models.get_issues(record_id)` + `db.models.get_records()` 从 SQLite 查。**原因**:Excel 的"处理状态"列要反映用户最新的采纳/拒绝决策,而这个状态只存在于数据库(`set_issue_status` 写的),内存里的 `ClassifiedResult` 不含它。从库读能保证导出的是用户操作后的最终状态,也让本函数自包含、可被"历史记录页"对任意历史 record 复用(阶段10)。

### 决策2:record 与统计字段阶段6 已写好,本阶段只回填 `result_path`

阶段6 的 `persist_result` 已经 `create_record(...)` 把 `total_issues`/`count_confirmed`/... 等全部统计字段写进 `records` 表了(这是阶段6 提前做了框架文档归给阶段8 的"建record",CLAUDE.md 有记录)。所以**本阶段不要再建 record、不要重写那些计数字段**,只需在 Excel 文件生成后,用 `update_record_stats(record_id, result_path=str(路径))` 把导出文件路径回填。框架文档阶段8 验收里的"流程记录写入"在本项目里就退化成这一步 `result_path` 回填。

### 决策3:issue 顺序直接用 `get_issues` 返回的 issue_id 升序,不按 `page_location` 字符串重排

框架文档要求"按页码升序排列"。`issues` 是阶段6 按 `ClassifiedResult.issues` 的顺序逐条 `add_issue` 写入的,而阶段5 的分层模块已经把该列表排好序(页码升序、同页按 block_index、未定位条目排最后),所以 `issues.issue_id` 的自增顺序 = 分层模块排好的阅读顺序。`get_issues` 默认 `ORDER BY issue_id`,直接用即可。**不要**去解析 `page_location`(它是"第N页"之类的 TEXT,字符串排序不可靠,且未定位条目为 NULL)来重排——那反而会把已经排好的顺序打乱。

## 本阶段任务

### 1. 依赖:新增 openpyxl

框架文档第 9 章技术选型指定 Excel 导出用 **openpyxl**(支持按行/单元格设色)。`requirements.txt` 追加 `openpyxl` 一行(带简短中文注释说明用途)。这是本阶段唯一新增依赖。

### 2. 函数签名(相对占位签名的必要调整)

占位签名是 `export_issues_to_excel(record_id: int) -> Path`。实现时**增加一个 `db_path=None` 参数**:

```python
def export_issues_to_excel(record_id: int, db_path=None) -> Path:
    """把指定 record 下的问题导出为 Excel,回填 result_path,返回文件路径。"""
```

原因:全仓库所有 `db.models` 函数都支持 `db_path`(测试传临时库,不碰 `config.DB_PATH`),本函数内部要调 `get_issues`/`get_records`/`update_record_stats`,必须把 `db_path` 透传下去,否则测试没法用临时库验证。这与阶段5/6 里"提示词/框架文档是设计稿,实际数据结构/测试惯例优先"是同一类调整,不是自由发挥。

### 3. 列结构(严格对齐框架文档第 8 章,7 列,顺序固定)

| Excel 列名 | 数据来源(issues 表字段) | 备注 |
|---|---|---|
| 页码/位置 | `page_location` | NULL(未定位)显示为"未定位" |
| 原文 | `original_text` | |
| 问题类型 | `issue_type` | 十类维度之一,原样 |
| 优先级 | `priority` | 高/中/低/可选 |
| 分层标注 | `layer` | 确定性错误/存疑待核实/引文类/风格可选 |
| 修改建议 | `suggestion` | 已是阶段5 改写后的值(引文类此列已是"原文照录…"话术),原样导出,**不要再改写** |
| 处理状态 | `status`(+ `reject_reason`) | 见下方格式 |

**处理状态列格式**:`status` 为"已拒绝"且 `reject_reason` 非空时,单元格文本为 `已拒绝(理由:{reject_reason})`;其余情况直接是 `status` 值(待处理/已采纳/已拒绝但无理由)。

### 4. 格式规则(框架文档第 8 章格式规则 1~3)

1. **首行表头**:7 个列名,加粗、可加浅灰底,冻结首行(`freeze_panes`)方便滚动。
2. **高优先级整行标红**:`priority == config.PRIORITY_HIGH`(即"高")的问题,**整行 7 个单元格**填红色底(如浅红 `FFC7CE` 配深红字,或纯红底白字,取一种清晰的即可)。
3. **引文类整行标浅黄**:`layer == config.LAYER_QUOTATION`(即"引文类")的问题,整行填浅黄底(如 `FFF2CC`)。
4. **高优先级与引文类冲突时的优先级**:同一条 issue 可能既是引文类、又因 `issue_type` 属于政治敏感/民族地名被强制标为高优先级(阶段5 的优先级覆盖规则会这么判)。**这种情况按标红处理**(红色是更强的警示信号,优先级高于引文浅黄),并在实现里用注释写明这个取舍。若你认为浅黄该优先,先说明理由让我定,不要默默选另一种。
5. 列宽:给"原文""修改建议"两列设较宽的列宽(如 40~60),其余按内容适当;开启自动换行(`wrap_text`)让长文本可读。以上样式常量(颜色值等)硬编码在 `exporter.py` 即可,**不要**新增 config 配置项(非必要不加可调项,与阶段6 约定一致)。

### 5. 文件落盘位置与命名

- 目录:`config.EXPORTS_DIR`(已在 config.py 定义并自动创建)。
- 文件名:`{时间戳}_{doc_name}_校对清单.xlsx`,时间戳用 `datetime.now().strftime("%Y%m%d%H%M%S")` 前缀避免覆盖;`doc_name` 从 `get_records()` 查该 record 拿到(注意清洗文件名里的非法字符如 `/ \ : * ? " < > |`,替换为下划线,避免 Windows 下写盘失败)。
- 生成后 `update_record_stats(record_id, result_path=str(path), db_path=db_path)` 回填,返回该 `Path`。

### 6. 边界情况

- **该 record 下 0 条 issue**:仍生成一个只有表头的合法 xlsx(不要抛异常),正常回填 result_path 返回路径。
- **record_id 不存在**(`get_records` 里查无此 id):抛一个清晰的异常(如 `ValueError(f"record_id {record_id} 不存在")`),不要生成空文件。

### 7. app.py 接线(把阶段6 的占位按钮接上)

阶段6 在"标准校对"结果页底部留了"导出Excel"按钮,当前只 `st.info("Excel导出将在阶段8实现")`。本阶段把它接上真实导出:

- 点击"导出Excel" → 调 `core.exporter.export_issues_to_excel(record_id, db_path=None)`(生产用默认库) → 拿到路径后,用 `st.download_button` 提供下载(读取生成文件的 bytes,`mime="application/vnd.openpyxl-...spreadsheetml.sheet"`,`file_name` 用生成的文件名)。
- 导出成功给 `st.success` 提示并显示落盘路径;导出过程异常用 `st.error` 兜住不崩页面(与阶段6 的异常处理风格一致)。
- **只改"标准校对"结果页底部这一处按钮逻辑**,不动阶段6 的上传/校对/问题卡/session_state 生命周期。注意:导出是纯读库操作,点它触发的 rerun 不应重跑校对(校对结果仍只从 session_state 缓存读,与阶段6 一致)。

### 8. 测试(tests/test_stage8.py)——全部不耗额度

沿用现有测试文件惯例(无 conftest.py,文件头手动 `sys.path.insert`;`db_path` fixture 用 `tmp_path` + `database.init_db(path)`,不碰 `config.DB_PATH`)。

**8.1 core/exporter.py 单元测试(用临时库 + 临时导出目录,`monkeypatch` 掉 `config.EXPORTS_DIR` 指向 `tmp_path`,和阶段6 测试 monkeypatch `UPLOADS_DIR` 同一手法)**:

手工用 `create_record` + `add_issue` 造一条 record + 若干 issue,覆盖:四个分层各至少一条、至少一条高优先级、至少一条引文类、至少一条已采纳、至少一条带理由的已拒绝。然后调 `export_issues_to_excel(record_id, db_path=临时库)`,再用 `openpyxl.load_workbook` 打开生成文件断言:

- 文件确实存在于 `tmp_path`(即 monkeypatch 后的 EXPORTS_DIR),返回的 Path 指向它。
- 首行 7 个表头文本与规范列名逐一相等、顺序正确。
- 数据行数 == issue 条数;行顺序与 `get_issues(record_id)` 的 issue_id 顺序一致(逐行核对"原文"列)。
- **高优先级行**:该行单元格填充色 == 约定的红色(读 `cell.fill.start_color.rgb` 断言)。
- **引文类行**:填充色 == 约定的浅黄。
- **高优先级 ∩ 引文类** 那条(专门造一条:引文类 + priority=高):填充色是红,不是黄(验证决策4 的冲突取舍)。
- 已拒绝行的"处理状态"单元格文本包含拒绝理由原文。
- 未定位(page_location=NULL)行的"页码/位置"单元格显示"未定位",不是空/None。
- 导出后 `get_records()` 里该 record 的 `result_path` == 返回路径的字符串。

**8.2 边界**:
- 0 条 issue 的 record → 生成只有表头的文件、不抛异常、result_path 正常回填。
- 不存在的 record_id → 抛预期异常(`pytest.raises`)。

**8.3 app.py UI 冒烟(streamlit.testing.v1.AppTest,沿用阶段6 手法)**:
`patch("core.exporter.export_issues_to_excel", return_value=造一个真实存在的临时 xlsx 路径)`,驱动到有结果的状态(可复用阶段6 测试里 mock `run_standard_proofread`/`persist_result` 得到结果页的套路),点"导出Excel"按钮,断言不抛异常、出现下载按钮(`at.download_button` 非空)/成功提示。**若 AppTest 对 download_button 支持不足导致断言不稳,允许退化为"点击导出后页面不崩溃、export 函数被调用一次"这一较弱断言**,并在完成说明里如实标注覆盖到哪一步。

## 完成标准

1. `pytest tests/test_stage8.py` 全部通过(不耗额度)。
2. `pytest tests/`(不带 `-m integration`)全绿,确认阶段1~6 无回归。
3. `streamlit run app.py` 手动实测:跑一轮标准校对(可复用阶段6 的样本)→ 标几条采纳/拒绝 → 点导出 → 下载打开 xlsx,肉眼核对:按页码顺序、高优先级整行红、引文类整行浅黄、修改建议列引文类是"原文照录"话术、处理状态列采纳/拒绝(含理由)正确。如实记录观感,不美化。
4. 打开数据库确认该 record 的 `result_path` 已回填到导出文件。

## 约束

- 只新建/修改:`core/exporter.py`(实现)、`app.py`(仅"标准校对"结果页底部导出按钮那一处)、`tests/test_stage8.py`(新建)、`requirements.txt`(加 openpyxl)。不动 `core/parser.py`/`chunker.py`/`llm_client.py`/`proofreader.py`/`classifier.py`/`workflow.py`(阶段2~6 已验收)、不动 `db/`(现有 CRUD 已够用,`get_issues`/`get_records`/`update_record_stats` 组合足以支撑)、不碰 `core/followup.py`/`comparer.py`(保持 NotImplementedError 占位)。
- 不新增 config 配置项(颜色等样式常量硬编码在 exporter.py)。
- 导出数据一律从数据库(`get_issues`/`get_records`)读,不依赖内存 `ClassifiedResult`(见决策1)。
- 本阶段不重建 record、不重写统计计数字段,只回填 `result_path`(见决策2)。
- 不引入 pandas 等重依赖来写 Excel——openpyxl 直接够用,且能精细控制单元格颜色(pandas.to_excel 设行/单元格色反而绕)。

---

## 附:给你自己的提醒(不粘给 Claude Code)

1. **本阶段的灵魂是"从库读、还原用户最终决策"**:测试里"已拒绝行含拒绝理由""采纳状态正确"这些断言,验证的是导出反映的是用户在阶段6 界面上的真实操作结果,不是 LLM 原始输出。这是"替代手工整理 Excel"能真正省事的前提——导出的表拿去就能交,不用再核对状态。
2. **颜色冲突(高优先级引文类)那条测试是有意为之**:阶段5 的优先级覆盖规则(政治敏感/民族地名强制高优先)会真的造出"既是引文类又是高优先级"的条目,导出时两种底色规则会打架。先在提示词里把取舍定死(红优先),再用一条测试钉住,避免以后改样式时不小心让它变成随机结果。
3. **完成后更新 CLAUDE.md**:按阶段4/5/6 的惯例,在"当前进度"表把阶段8 标 ✅,补一段"core/exporter.py 关键点(阶段8,已实现)"记录三条关键设计决策(从库读/只回填result_path/按issue_id排序)和相对占位签名多的 `db_path` 参数,方便下次回看。
4. **阶段8 之后**:标准校对主线(解析→分块→校对→分层→界面→导出)就完整闭环了,项目定位那句话两半都落地。接下来按调整后的顺序回去做阶段7(对话追问),或视需要做阶段9(原稿比对)/阶段10(收尾)。
