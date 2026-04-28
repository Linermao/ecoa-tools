# AS6 工具链流水线改造清单

## 1. 文档目的

本文基于以下两类官方材料，整理当前系统中 ECOA AS6 工具链流水线的改造清单，用于指导后续前端、Java 后端、Python 编排层和联调验证工作。

- 官方 GitHub 仓库：`https://github.com/ecoa-tools/as6-tools`
- 官方资料：`doc/ECOA_AS6_Tooling.pdf`

本文目标不是重复介绍各工具本身，而是回答以下问题：

- 当前统一流水线与官方工作流相比，哪些地方不一致
- 应该改造成什么样的目标流程
- 每一层代码需要改什么
- 怎么判断改造完成

## 2. 官方依据

## 2.1 工具职责

官方仓库和配套文档对工具职责的描述可以归纳为：

- `EXVT`：ECOA XML 校验工具
- `ASCTG`：生成组件级测试 harness，对原 ECOA 模型生成新的测试模型
- `MSCIGT`：生成模块实现与测试所需骨架
- `CSMGVT`：生成桌面环境下的功能测试框架与 stub
- `LDP`：生成 ECOA 中间件平台

其中：

- GitHub 仓库更适合确认**工具职责**
- PDF 更适合确认**工具之间的推荐工作流**

## 2.2 官方工作流

`ECOA_AS6_Tooling.pdf` 的 “Possible Workflow using Tools” 给出了明确工作流：

### 组件级开发 / 测试工作流（For each stakeholder）

1. `ASCTG` 生成 component-level test harness
2. 组件设计（breakdown into ECOA modules）
3. `MSCIGT` 生成模块实现与测试工件
4. 在 IDE 中补齐模块和 harness 代码骨架
5. `CSMGVT` 做功能测试
6. `LDP` 做 ECOA 环境测试

### 应用级集成工作流（Application-level integration）

1. 在 EDT 中聚合各部分装配和实现
2. 可选 `CSMGVT`
3. `LDP` 做最终 ECOA 环境测试

## 2.3 对当前产品设计的含义

根据官方 workflow，可以得出两个重要结论：

- `ASCTG` 不是所有场景都必须经过的“总是默认阶段”，它属于**组件级 / stakeholder 级测试流**
- `ASCTG` 与 `MSCIGT` 的关系不是并列可交换，而是官方工作流中 `ASCTG` 在前、`MSCIGT` 在后

因此，当前系统把所有工具固定为一条线性主链的做法，与官方 workflow 不完全一致。

## 3. 当前实现问题确认

## 3.1 阶段顺序不一致

当前 Python 编排层的阶段顺序是：

- `EXVT -> MSCIGT -> ASCTG -> CSMGVT -> LDP`

但：

- 前端说明认为 `ASCTG` 在 `MSCIGT` 前
- Java 后端默认阶段顺序也将 `ASCTG` 放在 `MSCIGT` 前
- PDF 官方 workflow 明确也是 `ASCTG -> MSCIGT`

这说明当前系统在“阶段定义”上已经发生三处不一致。

## 3.2 统一流水线把两类官方工作流混成了一条

官方 workflow 至少包含两类不同场景：

- 组件级开发/测试流
- 应用级集成流

但当前产品交互和后端默认值倾向于把它们压成一条总流水线，结果是：

- `ASCTG` 被放进了通用代码生成路径
- `CSMGVT` 和 `LDP` 被同时当成后续执行分支
- 用户无法明确区分“我是在做组件级 harness 测试”还是“我是在做应用级集成”

## 3.3 ASCTG 产物没有成为后续阶段的输入

官方资料说明 `ASCTG` 生成的是**新的 ECOA 模型**。  
这意味着如果后续还要继续跑 `MSCIGT` / `CSMGVT` / `LDP`，应当以新的 harness project 作为输入。

当前实现中的问题是：

- `ASCTG` 虽然被执行了
- 但后续执行器仍继续使用初始 `project.xml`
- 没有显式切换到 `*-harness.project.xml`

结果就是：

- `ASCTG` 的语义没有真正贯穿到后续阶段
- 流水线名义上包含 `ASCTG`，实际上后续未消费其核心产物

## 3.4 默认阶段策略不符合官方 workflow

当前后端默认会把五个阶段都作为候选默认集。  
这不符合 PDF 中“中间存在 IDE 开发步骤”的工作流表达。

更合理的方式应当是：

- 对不同工作模式给不同默认阶段
- 不再把所有工具默认视为一条自动串行链

## 3.5 AWAITING_CODE 状态语义过宽

当前实现只要命中建模阶段且未进入执行分支，就可能进入 `AWAITING_CODE`。  
但按照官方 workflow，真正需要进入 IDE 填代码的时点应当是在 `MSCIGT` 之后。

因此：

- 仅执行 `ASCTG` 后不应提示“骨架代码已就绪”
- `AWAITING_CODE` 应当与 `MSCIGT` 成功完成强绑定

## 4. 目标流水线

建议将当前“单一流水线”重构为两个明确模式。

## 4.1 模式 A：组件级开发 / 测试模式（Harness Mode）

适用场景：

- 某个 stakeholder / 组件负责人
- 希望围绕“待测组件 + harness”构造测试环境
- 后续继续生成骨架、写代码、做功能测试

目标流程：

1. `EXVT`
2. `ASCTG`
3. 切换当前 project 为新生成的 `*-harness.project.xml`
4. `MSCIGT`
5. `AWAITING_CODE`
6. 用户进入 Code Server / IDE 补齐业务逻辑与 harness 代码
7. `CSMGVT`
8. 可选 `LDP`

说明：

- 这是最贴近 PDF 中 “For each stakeholder” 的流程
- `ASCTG` 在这里是必要阶段，不应被静默跳过

## 4.2 模式 B：应用级集成模式（Application Integration Mode）

适用场景：

- 系统已完成各组件实现
- 当前关注的是整体装配与集成验证
- 不需要额外生成组件级 harness

目标流程：

1. `EXVT`
2. 可选 `CSMGVT`
3. `LDP`

说明：

- 这是最贴近 PDF 中 “Application-level integration” 的流程
- 该模式下默认不应引入 `ASCTG`

## 4.3 模式 C：当前产品兼容模式（过渡期）

如果产品短期不方便大改交互，可先保留一个“兼容模式”，但需要满足：

- UI 上明确标识这是“旧统一流程”
- 不再默认勾选不适用于当前模式的阶段
- 后端和 Python 层至少先修正 `ASCTG -> MSCIGT` 顺序与 harness project 切换问题

## 5. 改造项清单

以下按优先级整理。

## 5.1 P0：统一阶段语义与工作模式

目标：

- 不再将官方两类 workflow 混为一条线性总流水线

改造项：

- 前端增加“工作模式”概念，至少区分：
  - 组件级开发 / 测试模式
  - 应用级集成模式
- Java 后端请求体中增加 `workflowMode` 或等价字段
- Python 编排层根据 `workflowMode` 决定默认阶段与状态机

涉及范围：

- 前端对话框
- Java 控制器请求 DTO
- Python `/api/generate` 入口与状态流转

## 5.2 P0：修正 ASCTG 与 MSCIGT 的顺序

目标：

- 在组件级开发 / 测试模式中，将 `ASCTG` 放在 `MSCIGT` 前

改造项：

- Python `PHASE_STEPS` 顺序调整
- Java 默认阶段顺序和前端阶段说明保持一致
- 日志与历史记录中的阶段编号同步调整

验收要点：

- 组件级模式下，运行日志中必须先出现 `ASCTG`，再出现 `MSCIGT`

## 5.3 P0：让 ASCTG 输出成为后续阶段输入

目标：

- `ASCTG` 生成的新模型必须成为后续 `MSCIGT` / `CSMGVT` / `LDP` 的输入

改造项：

- 在 `ASCTG` 执行完成后，显式解析新生成的 `*-harness.project.xml`
- 编排层维护一个“当前活动 project file”变量
- 后续工具执行改为读取“当前活动 project file”，而不是固定使用初始导出的 `project.xml`
- 任务回调日志中输出当前正在使用的 project file

验收要点：

- `ASCTG` 成功后，后续阶段日志中可见 harness project 路径
- 人工检查工作区时，后续阶段确实基于 harness project 执行

## 5.4 P0：收紧 AWAITING_CODE 状态

目标：

- `AWAITING_CODE` 只出现在真正已生成代码骨架之后

改造项：

- 将 `AWAITING_CODE` 触发条件改为：
  - 当前模式允许代码补写
  - `MSCIGT` 已成功完成
  - 后续执行阶段尚未开始
- 如果只执行了 `ASCTG`，不要进入 `AWAITING_CODE`

验收要点：

- 仅跑 `ASCTG` 时，状态不得显示“等待编写业务逻辑”
- 跑完 `MSCIGT` 后，才出现 Code Server 引导

## 5.5 P0：取消 ASCTG 的静默跳过

目标：

- 用户显式选择了 `ASCTG` 时，缺少配置必须失败，不应悄悄 skip

改造项：

- 若组件级模式下选中 `ASCTG`，但：
  - 没有 `selected_components`
  - 且找不到可用 `config.xml`
  - 则直接返回明确错误
- 前端给出配置缺失提示

验收要点：

- 缺少 ASCTG 配置时，任务状态为 `FAILED`，错误信息可读
- 不再出现“用户选了 ASCTG，但系统只是 warning 后跳过”的行为

## 5.6 P1：调整前端默认勾选与交互文案

目标：

- 让 UI 默认行为与工作模式一致

改造项：

- 组件级开发 / 测试模式默认：
  - `EXVT`
  - `ASCTG`
  - `MSCIGT`
- 应用级集成模式默认：
  - `EXVT`
  - 可选 `CSMGVT`
  - `LDP`
- 文案中不再把所有阶段说成同一条固定流水线
- “继续执行”按钮语义按模式区分

验收要点：

- 用户一眼能区分当前是在做 harness 测试还是应用级集成

## 5.7 P1：梳理任务状态与历史记录模型

目标：

- 历史记录能反映“模式”和“当前 project file”的变化

改造项：

- 任务记录新增或补充字段：
  - `workflowMode`
  - `activeProjectFile`
  - `baseProjectFile`
  - `harnessProjectFile`（如适用）
- 历史记录页面展示任务模式和关键 project 路径

验收要点：

- 用户能从历史记录中区分一次任务是组件级还是应用级
- 能定位某次任务到底跑的是原 project 还是 harness project

## 5.8 P1：增加回归测试与联调样例

目标：

- 防止再次出现“阶段顺序一致但语义不一致”的问题

改造项：

- Python 编排层增加模式级测试：
  - Harness Mode 顺序测试
  - Integration Mode 顺序测试
  - `ASCTG` 输出切换 project 测试
  - `AWAITING_CODE` 触发条件测试
- Java 层增加默认参数测试
- 前端增加默认模式与默认勾选测试

验收要点：

- 至少覆盖以下场景：
  - Harness Mode：`EXVT -> ASCTG -> MSCIGT -> AWAITING_CODE`
  - Integration Mode：`EXVT -> CSMGVT? -> LDP`

## 5.9 P2：补充用户文档与运维说明

目标：

- 让用户知道何时应该选 `ASCTG`
- 让运维知道怎样排查 “没切换到 harness model” 这类问题

改造项：

- 更新产品说明
- 更新开发者接入文档
- 更新 FAQ / 排障说明

## 6. 影响范围

## 6.1 前端

- 生成对话框的模式选择
- 默认勾选逻辑
- 阶段说明文案
- `AWAITING_CODE` 提示逻辑
- 历史记录展示

## 6.2 Java 后端

- 默认阶段设置
- 请求 DTO 与继续执行接口
- 历史任务模型与存储字段

## 6.3 Python 编排层

- 阶段顺序
- 模式分流
- `ASCTG` 成果接续
- 错误处理和状态机

## 6.4 文档与联调

- 工具链使用说明
- 测试用例
- 联调验收脚本

## 7. 验收标准

改造完成后，至少应满足以下标准：

1. 组件级开发 / 测试模式严格符合官方 workflow 的主线语义：
   `ASCTG -> MSCIGT -> IDE 开发 -> CSMGVT -> LDP`
2. 应用级集成模式不再强制包含 `ASCTG`
3. `ASCTG` 成功后，后续阶段明确使用 harness project
4. `AWAITING_CODE` 仅在 `MSCIGT` 成功后出现
5. 用户选择 `ASCTG` 但缺少配置时，系统明确失败而不是静默跳过
6. 前端、Java、Python 三层对阶段顺序和模式定义一致

## 8. 风险与待确认项

以下问题建议在正式改造前确认：

- `LDP` 是否需要在 Harness Mode 下默认开放，还是只保留为可选高级步骤
- 当前产品入口是否要拆成两个按钮，还是保留一个入口加模式切换
- `ASCTG` 生成的新 harness project 文件命名是否稳定，是否需要统一解析规则
- 是否需要保留旧统一流水线作为兼容模式

## 9. 推荐实施顺序

建议分三步落地：

### 第一步：先修正语义错误

- 修正 `ASCTG -> MSCIGT`
- 修正 harness project 切换
- 修正 `AWAITING_CODE`
- 修正 ASCTG 配置缺失的错误处理

### 第二步：引入工作模式

- 前端模式选择
- 后端模式字段
- Python 模式分流

### 第三步：完善历史记录、测试与文档

- 增加任务模式字段
- 增加回归测试
- 更新说明文档

