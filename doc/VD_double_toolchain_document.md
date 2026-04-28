# VD_double 工具链说明与测试记录

## 1. 文档目的

本文档用于说明 `VD_double_operations` 例子在 ECOA 工具链中的处理过程，重点包括：

- 各工具的作用
- 各工具的输入与输出
- `ASCTG` 的作用、设计意义和输出用途
- 本次 5 步测试的实际执行方式与结果


## 2. 工具链整体说明

本次涉及的工具链包括：

1. `EXVT`
2. `MSCIGT`
3. `ASCTG`
4. `CSMGVT`
5. `LDP`

它们不是完全同一层级的工具。

- `EXVT` 是校验工具，负责检查 ECOA XML 工程是否合法。
- `MSCIGT` 是骨架代码生成工具，负责生成模块骨架和接口。
- `ASCTG` 是测试工程生成工具，负责从原始工程派生出一个 harness 测试工程。
- `CSMGVT` 是测试框架生成工具，可基于原始工程或 harness 工程生成桌面侧测试框架。
- `LDP` 是平台代码生成工具，可基于原始工程或 harness 工程生成完整平台代码。

因此，从流程上看：

- `EXVT`、`MSCIGT`、`LDP` 可以直接作用于原始工程。
- `ASCTG` 不是主流水线必经步骤，更适合看作一个测试分支入口。
- `ASCTG` 的输出可以继续交给 `CSMGVT` 或 `LDP`。


## 3. 各工具作用、输入与输出

### 3.1 EXVT

#### 作用

`EXVT` 用于检查 ECOA XML 工程是否合法。

它主要检查：

- XML 语法是否正确
- `project.xml` 中引用的文件是否存在
- types、services、component definitions、implementations、assembly、deployment、logical system 是否能互相对应
- final assembly、wire、deployment、logical system 之间是否一致

#### 输入

- `project.xml`
- `project.xml` 引用的整套工程文件

本次输入工程：

- `projects/VD_double_operations/VD_double_operations.project.xml`

#### 输出

`EXVT` 主要输出日志和返回码，不以生成代码为主。

本次测试输出目录：

- `projects/VD_double_runs/01-exvt`

主要内容：

- `input/VD_double_operations`
- `logs/exvt.stdout.log`
- `logs/exvt.stderr.log`
- `logs/return_code.txt`


### 3.2 MSCIGT

#### 作用

`MSCIGT` 用于生成模块 skeleton、容器接口和基础类型文件。

它更偏向开发前期代码骨架生成。

#### 输入

- `project.xml`
- checker，一般为 `ecoa-exvt`
- 原始工程中引用的所有 ECOA 文件

本次输入工程：

- `projects/VD_double_operations/VD_double_operations.project.xml`

#### 输出

主要输出包括：

- `0-Types/inc/*.h`
- `0-Types/inc/*.hpp`
- 模块实现目录中的 `inc`、`inc-gen`、`src`、`tests`

本次测试输出目录：

- `projects/VD_double_runs/02-mscigt`

需要注意：

- `MSCIGT` 虽然支持 `-o` 输出目录，但它仍可能同时改写工程内部的模块目录。
- 因此在测试流程中，后续步骤不建议继续直接复用被改写过的原始工程。


### 3.3 ASCTG

#### 作用

`ASCTG` 是 Application Software Components Test Generator。

它的核心作用不是校验工程，也不是直接生成平台，而是：

- 根据配置文件中选中的组件实例
- 生成一个新的 harness 测试工程
- 保留被测组件
- 用 HARNESS 替代其余组件或为其补充测试连接

#### 输入

- `project.xml`
- `config.xml`
- checker，一般为 `ecoa-exvt`

本次输入工程：

- `projects/VD_double_runs/03-asctg/input/VD_double_operations/VD_double_operations.project.xml`

本次使用的配置文件：

- `projects/VD_double_runs/03-asctg/input/VD_double_operations/ecoa_config.xml`

配置文件内容如下：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<asctg>
  <components>
    <componentInstance>compReader0</componentInstance>
    <componentInstance>compReader1</componentInstance>
  </components>
</asctg>
```

这里的 `componentInstance` 必须填写**最终装配中的组件实例名**，不是组件类型名，也不是实现名。

本例中可选实例来自：

- `compReader0`
- `compReader1`
- `compWriter`
- `compFinisher`

对应文件：

- `projects/VD_double_operations/5-Integration/demo.impl.composite`

#### 输出

`ASCTG` 输出的是一个新的测试工程文件集，核心新文件包括：

- `HARNESS_type.componentType`
- `HARNESS.impl.xml`
- `VD_double_operations-harness.project.xml`
- `demo-harness.impl.composite`
- `demo-harness.deployment.xml`

本次测试输出目录：

- `projects/VD_double_runs/03-asctg/output`


### 3.4 CSMGVT

#### 作用

`CSMGVT` 用于生成桌面侧功能测试框架。

它可以基于：

- 原始工程
- 或 `ASCTG` 生成出来的 harness 工程

本次测试中，我们使用的是 **ASCTG 产出的 harness 工程**。

#### 输入

- harness `project.xml`
- checker，一般为 `ecoa-exvt`

本次输入工程：

- `projects/VD_double_runs/04-csmgvt/input/VD_double_operations-harness.project.xml`

#### 输出

主要输出包括：

- 顶层 `CMakeLists.txt`
- `src/main.cpp`
- `src/CSM_*.cpp`
- 组件相关测试框架目录
- `0-Types/inc/*.h`

本次测试输出目录：

- `projects/VD_double_runs/04-csmgvt/output`


### 3.5 LDP

#### 作用

`LDP` 用于生成完整平台代码。

它可以基于：

- 原始工程
- 或 harness 工程

本次测试中，我们使用的是 **ASCTG 产出的 harness 工程**。

#### 输入

- harness `project.xml`
- checker，一般为 `ecoa-exvt`

本次输入工程：

- `projects/VD_double_runs/05-ldp/input/VD_double_operations-harness.project.xml`

#### 输出

主要输出包括：

- 顶层 `CMakeLists.txt`
- `CMakeModules`
- `platform/`
- `platform/main.c`
- `platform/PD_*.c`
- `platform/route.h`
- `HARNESS/`
- 被保留组件的组件代码目录

本次测试输出目录：

- `projects/VD_double_runs/05-ldp/output`


## 4. ASCTG 说明

### 4.1 为什么要有 ASCTG

`ASCTG` 的意义在于把“完整工程”转成“测试工程”。

在真实测试场景中，我们通常不会一次测试整套系统，而是想：

- 只保留某一个或某几个组件作为被测对象
- 其余组件由测试壳、桩或 HARNESS 代替

这样做的好处是：

- 降低测试对象范围
- 更容易控制输入输出
- 更容易定位某个组件的问题
- 可以把工程转换成更适合后续测试工具处理的形式


### 4.2 ASCTG 为什么不是流水线必经步骤

`ASCTG` 不是每次都必须执行。

如果目标是：

- 校验工程是否合法
- 生成模块骨架
- 直接生成平台代码

那么原始工程可以直接进入：

- `EXVT`
- `MSCIGT`
- `LDP`

但如果目标是：

- 做被测组件筛选
- 生成测试工程
- 让后续测试围绕特定组件展开

那么就应该走：

- 原始工程 -> `ASCTG` -> harness 工程 -> `CSMGVT` / `LDP`

因此 `ASCTG` 更适合作为一个**测试分支入口**。


### 4.3 ASCTG 的输出可以做什么

`ASCTG` 的输出不是最终报告，而是一份新的工程。

它可以继续用于：

- 在前端作为一个新的派生工程进行保存或渲染
- 交给 `CSMGVT` 生成测试框架
- 交给 `LDP` 生成基于 harness 工程的平台代码

也就是说，`ASCTG` 的输出可以看作：

- 原工程的测试分支版本
- 后续测试工具的输入源


## 5. 本次 5 步测试过程

### 5.1 测试原则

本次测试采用“一步一目录、输入输出隔离”的方式执行。

测试目录统一放在：

- `projects/VD_double_runs`

每一步都有独立目录：

1. `01-exvt`
2. `02-mscigt`
3. `03-asctg`
4. `04-csmgvt`
5. `05-ldp`

这样做的目的：

- 避免不同工具互相覆盖结果
- 保留每一步的输入快照
- 保留每一步的日志与返回码
- 方便后续分析和展示


### 5.2 第 1 步：EXVT

目录：

- `projects/VD_double_runs/01-exvt`

目的：

- 校验 `VD_double_operations` 工程是否合法

结果：

- 成功
- 返回码 `0`
- 无 error / warning


### 5.3 第 2 步：MSCIGT

目录：

- `projects/VD_double_runs/02-mscigt`

目的：

- 生成模块骨架和类型头文件

结果：

- 成功
- 返回码 `0`
- 生成 `0-Types/inc/*.h`、`*.hpp`
- 生成模块相关目录结构

说明：

- 此工具可能同时改写工程内部目录，因此后续步骤改为使用独立输入副本


### 5.4 第 3 步：ASCTG

目录：

- `projects/VD_double_runs/03-asctg`

目的：

- 基于 `compReader0` 和 `compReader1` 生成 harness 测试工程

结果：

- 成功
- 返回码 `0`
- 生成新的 harness project/composite/deployment
- 生成 HARNESS component type 和 implementation

关键输出：

- `VD_double_operations-harness.project.xml`
- `demo-harness.impl.composite`
- `demo-harness.deployment.xml`


### 5.5 第 4 步：CSMGVT

目录：

- `projects/VD_double_runs/04-csmgvt`

目的：

- 基于 harness 工程生成桌面侧测试框架

输入：

- `03-asctg` 产出的 harness 工程

结果：

- 成功
- 返回码 `0`
- 有 1 个 warning
- warning 为 harness composite 文件名规范提示，不影响生成

关键输出：

- `CMakeLists.txt`
- `src/main.cpp`
- `src/CSM_VD_double_operations-harness.cpp`


### 5.6 第 5 步：LDP

目录：

- `projects/VD_double_runs/05-ldp`

目的：

- 基于 harness 工程生成完整平台代码

输入：

- `04-csmgvt/input` 中的 harness 工程副本

结果：

- 成功
- 返回码 `0`
- 有 1 个 warning
- warning 与 harness composite 文件名规范有关，不影响平台生成

关键输出：

- `output/platform`
- `output/platform/main.c`
- `output/platform/PD_HARNESS_PD.c`
- `output/platform/PD_Reader_PD.c`
- `output/platform/PD_Writer_Reader_PD.c`
- `output/HARNESS`
- `output/mycompReader`


## 6. 本次测试结论

本次测试验证了以下结论：

1. `VD_double_operations` 原始工程可以通过 `EXVT` 校验。
2. `MSCIGT` 可以正常生成骨架和类型文件。
3. `ASCTG` 可以根据组件实例清单生成新的 harness 测试工程。
4. `ASCTG` 的输出可以继续作为 `CSMGVT` 和 `LDP` 的输入。
5. `ASCTG` 更适合作为测试分支入口，而不是主流水线必经步骤。
6. 对于前端设计，可以考虑把 `ASCTG` 设计成一个“按组件实例生成测试工程”的分流按钮。



