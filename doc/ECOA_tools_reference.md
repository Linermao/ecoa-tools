# ECOA 工具说明手册

## 1. 文档目的

本文档用于对当前使用的 ECOA 工具进行统一说明，重点介绍：

- 每个工具的定位
- 每个工具的主要作用
- 每个工具的输入
- 每个工具的输出
- 每个工具输出的用途

本文档覆盖的对象包括：

- `ecoa-toolset`
- `ecoa-exvt`
- `ecoa-mscigt`
- `ecoa-asctg`
- `ecoa-csmgvt`
- `ecoa-ldp`


## 2. 工具总体关系

这些工具并不是完全并列的。

- `ecoa-toolset` 是基础公共库，不是最终用户直接使用的主工具。
- `ecoa-exvt` 是合法性校验工具。
- `ecoa-mscigt` 是模块骨架与接口生成工具。
- `ecoa-asctg` 是测试工程生成工具。
- `ecoa-csmgvt` 是测试框架生成工具。
- `ecoa-ldp` 是平台代码生成工具。

从实际使用上看：

- `EXVT` 往往是进入其他工具前的检查闸门。
- `MSCIGT` 更偏向开发准备阶段。
- `ASCTG` 更偏向测试分支生成。
- `CSMGVT` 更偏向基于工程生成测试框架。
- `LDP` 更偏向生成完整平台代码。


## 3. ecoa-toolset

### 3.1 工具定位

`ecoa-toolset` 是基础公共库。

它本身不是一个独立的业务工具，而是给其他 ECOA 工具提供：

- 参数处理
- 通用模型解析
- XML 检查
- 代码生成公共逻辑
- 类型生成工具
- 日志输出机制

### 3.2 作用

它的作用是把各个工具的公共部分抽出来，避免：

- 每个工具重复实现解析逻辑
- 每个工具重复实现参数检查
- 每个工具重复实现基础生成器

### 3.3 输入

`ecoa-toolset` 作为库，本身没有单独的用户输入界面。  
它的输入来自调用它的其他工具，例如：

- `project.xml`
- checker
- 模板目录
- 输出目录

### 3.4 输出

它本身不面向最终用户单独输出业务文件。  
它的输出通常表现为：

- 被其他工具调用后生成的头文件
- 生成器使用的中间模型
- 通用日志

### 3.5 输出用途

它的用途是支撑上层工具：

- `MSCIGT`
- `CSMGVT`

等工具正常工作。


## 4. ecoa-exvt

### 4.1 工具定位

`ecoa-exvt` 是 ECOA XML Validation Tool。

它是整个工具链里最基础的合法性校验工具。

### 4.2 作用

它负责判断一个 ECOA 工程是否成立，主要检查：

- XML 文件语法是否正确
- `project.xml` 中引用的文件是否存在
- 各类 ECOA 文件能否被正确解析
- component type、component implementation、assembly、deployment、logical system 是否一致
- wire 和 logical system 映射是否一致

### 4.3 输入

最核心输入是：

- `project.xml`

但严格来说，它会递归读取 `project.xml` 指向的整套工程文件，例如：

- `0-Types/*.xml`
- `1-Services/*.xml`
- `2-ComponentDefinitions/*`
- `4-ComponentImplementations/*`
- `5-Integration/*`

常用命令形态：

```bash
ecoa-exvt -p <project.xml> -v <level>
```

### 4.4 输出

`EXVT` 不以生成代码为目标。  
它的主要输出是：

- 标准输出日志
- 标准错误日志
- 返回码

### 4.5 输出用途

它的输出主要用于：

- 判断工程是否合法
- 为后续工具提供前置通过条件
- 定位工程结构问题

一般来说：

- 返回码 `0` 表示通过
- 非 `0` 表示存在严重问题


## 5. ecoa-mscigt

### 5.1 工具定位

`ecoa-mscigt` 是 Module Skeletons and Container Interfaces Generator Tool。

它是面向开发的骨架生成工具。

### 5.2 作用

它主要负责：

- 生成模块 skeleton
- 生成容器接口
- 生成基础类型头文件

这个工具更适合在开发初期使用，用来快速补齐：

- 模块目录结构
- 用户实现文件骨架
- 类型头文件

### 5.3 输入

主要输入包括：

- `project.xml`
- checker，通常是 `ecoa-exvt`
- 可选输出目录
- 可选模板目录

常用命令形态：

```bash
ecoa-mscigt -p <project.xml> -k ecoa-exvt -o <output> -f -v
```

### 5.4 输出

`MSCIGT` 输出通常包括两类：

第一类是类型文件，例如：

- `0-Types/inc/ECOA.h`
- `0-Types/inc/*.h`
- `0-Types/inc/*.hpp`

第二类是组件实现骨架目录，例如：

- `inc`
- `inc-gen`
- `src`
- `tests`

### 5.5 输出用途

这些输出的用途是：

- 为业务开发人员提供模块开发起点
- 为后续实现补齐目录结构
- 为编译或集成提供基础头文件

需要注意的是：

- `MSCIGT` 不只是简单在独立输出目录里写文件
- 某些情况下它也会改动工程内部组件实现目录

因此在测试或自动化流程中，推荐使用输入副本而不是直接作用于唯一原工程。


## 6. ecoa-asctg

### 6.1 工具定位

`ecoa-asctg` 是 Application Software Components Test Generator。

它不是主平台代码生成器，而是一个**测试工程生成器**。

### 6.2 作用

它的核心作用是：

- 根据一份组件实例清单
- 从原始 ECOA 工程派生出新的 harness 工程
- 保留指定组件作为被测对象
- 用 HARNESS 接管其余组件或补齐测试连接

它的重点不是直接测试业务代码，而是生成一份适合后续测试的工程。

### 6.3 输入

`ASCTG` 的关键输入有 3 个：

- `project.xml`
- `config.xml`
- checker，通常是 `ecoa-exvt`

其中：

- `project.xml` 定义完整工程
- `config.xml` 定义“哪些组件实例保留为被测对象”
- checker 用来保证输入工程本身合法

常用命令形态：

```bash
ecoa-asctg -p <project.xml> -c <config.xml> -k ecoa-exvt -o <output> -f -v 3
```

### 6.4 config.xml 是什么

`config.xml` 是一份组件实例清单，而不是部署配置或平台配置。

典型结构如下：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<asctg>
  <components>
    <componentInstance>compA</componentInstance>
    <componentInstance>compB</componentInstance>
  </components>
</asctg>
```

需要特别强调：

- 这里写的是组件实例名
- 不是 component type 名
- 也不是 implementation 名

### 6.5 输出

`ASCTG` 会生成一套新的 harness 工程文件集，常见输出包括：

- `*-harness.project.xml`
- `*-harness.impl.composite`
- `*-harness.deployment.xml`
- `HARNESS_type.componentType`
- `HARNESS.impl.xml`
- 拷贝后的完整输出工程目录

### 6.6 输出用途

这些输出可以用于：

- 保存为新的派生工程
- 在前端渲染为新的测试工程
- 作为 `CSMGVT` 的输入
- 作为 `LDP` 的输入

因此，`ASCTG` 的输出不是终点，而是一个新的测试分支起点。

### 6.7 为什么重要

`ASCTG` 的意义在于：

- 把完整工程转换成更聚焦的测试工程
- 让测试只围绕指定组件展开
- 明确区分原工程与测试工程

在产品设计上，它更像一个：

- 测试分流按钮
- 组件级测试入口

而不是默认主流水线中的每次必经步骤。


## 7. ecoa-csmgvt

### 7.1 工具定位

`ecoa-csmgvt` 是 Connected System Model Generator and Verification Tool。

它是面向桌面环境的测试框架生成工具。

### 7.2 作用

它主要负责：

- 基于工程生成桌面侧测试框架
- 生成可构建的测试工程结构
- 生成主程序、组件相关代码和类型文件

### 7.3 输入

主要输入包括：

- `project.xml`
- checker，通常是 `ecoa-exvt`
- 可选输出目录

它既可以吃：

- 原始工程
- 也可以吃 `ASCTG` 产出的 harness 工程

常用命令形态：

```bash
ecoa-csmgvt -p <project.xml> -k ecoa-exvt -o <output> -f -v
```

### 7.4 输出

常见输出包括：

- 顶层 `CMakeLists.txt`
- `src/main.cpp`
- `src/CSM_*.cpp`
- 各组件测试框架目录
- `0-Types/inc/*.h`

### 7.5 输出用途

这些输出可用于：

- 生成桌面侧功能测试工程
- 为后续编译测试提供基础
- 在 harness 工程基础上构造更明确的测试环境


## 8. ecoa-ldp

### 8.1 工具定位

`ecoa-ldp` 是 Lightweight Development Platform Tool。

它是整条工具链里最接近“完整平台代码生成”的工具。

### 8.2 作用

它主要负责：

- 生成平台目录
- 生成组件代码包装
- 生成 protection domain 代码
- 生成路由
- 生成平台运行库
- 生成构建文件

### 8.3 输入

主要输入包括：

- `project.xml`
- checker，通常是 `ecoa-exvt`
- 可选输出目录

它可以基于：

- 原始工程
- harness 工程

常用命令形态：

```bash
ecoa-ldp -p <project.xml> -k ecoa-exvt -o <output> -f -v 3
```

### 8.4 输出

典型输出包括：

- 顶层 `CMakeLists.txt`
- `CMakeModules`
- `platform/`
- `platform/main.c`
- `platform/PD_*.c`
- `platform/route.h`
- 组件代码目录
- 平台运行库代码

### 8.5 输出用途

这些输出主要用于：

- 生成完整平台代码基线
- 为后续编译和运行准备源码
- 支撑平台集成和系统级执行

如果进一步接上编译阶段，还可以继续得到：

- build 目录
- 可执行文件
- 编译日志


## 9. 工具选型建议

### 9.1 如果你的目标是检查工程是否成立

优先使用：

- `EXVT`

### 9.2 如果你的目标是快速生成模块骨架

优先使用：

- `MSCIGT`

### 9.3 如果你的目标是围绕某几个组件生成测试工程

优先使用：

- `ASCTG`

### 9.4 如果你的目标是生成桌面测试框架

优先使用：

- `CSMGVT`

### 9.5 如果你的目标是生成完整平台代码

优先使用：

- `LDP`


## 10. 推荐理解方式

可以把这些工具理解成三类：

### 10.1 检查类

- `EXVT`

### 10.2 开发准备类

- `MSCIGT`
- `LDP`

### 10.3 测试分支类

- `ASCTG`
- `CSMGVT`

其中最重要的链路分叉点是：

- 原始工程主线
- harness 测试工程主线

而 `ASCTG` 就是把主线切到测试工程主线的关键工具。

