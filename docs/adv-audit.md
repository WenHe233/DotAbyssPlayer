# AdvPlayer 对照审计报告：复刻实现 vs 原游戏 ADV 逻辑

> 对照基准：原游戏本体 `dotabyss_x_cl/`（《ドットアビスX》，Unity **6000.3.8f1** / IL2CPP，metadata v39）。
> 反编译工具：**Cpp2IL 2022.1.0-pre-release.21**（Il2CppDumper 发行版不支持 v39）。
> 复刻实现：`src/AdvPlayer/src/app.js`（单文件 ~5600 行）。
> 数据基准：`src/AdvPlayer/data_r18_all/stories/*/story.json`（已提取 983 条脚本，325k 条命令）。
> 生成日期：2026-07-06。

## 摘要（结论先行）

- **有大量功能未实现，且被静默丢弃**：app.js 的命令注册表覆盖 156 个命令；原游戏 ADV 系统共 **172 个命令类**（`Project.Novel.NovelCmd*`）。已提取数据里实际出现、但 app.js **未实现**的命令有 **54 个（按去 async 归并 31 个基名）、共 6877 次**。`NovelCommandRegistry.execute` 对未知命令兜底 `new NoopCommand()`（`app.js` 内），**无任何效果、也不报错**。
- **波及面**：**238 / 983 个剧情（24%）** 至少用到一个未实现命令。
- **两大缺失子系统**：`dot*`（点阵/像素场景：相机/背景/prefab/object 的加载与运动）和 `object*`（通用精灵）。二者**连资源都没提取**（`tools/` 无对应提取器，`data/` 无对应目录）——补齐＝提取器新工作 + 渲染实现。
- **还有玩家级功能整块缺失**：选项分支（Select）、跳过/快进（Skip/FastPlay）、历史回看（Backlog Log）、变量系统（Val/setval）、选项弹窗（Option）、Ruby 注音。这些多数在 r18 线性剧情里用不到，但属于原 ADV 引擎的能力。
- **实现有误（已确认 1 项）**：原版参数取值 `NovelArguments.GetFloat/GetInt` 会用 `GlobalCalculator` 把字符串当**数学表达式求值**；app.js 的 `float()/int()` 只做 `parseFloat/parseInt`，遇到表达式/变量参数会错。
- **好消息**：占绝大多数的对话/演出命令（message、Live2D、charastand、bgm/se/voice、fade/crossfade/transition、camera、chara\*、still/subimage/prefab、shake、jump 弹跳、wait/auto、labeljump 控制流）**忠实还原且工作正常**（前序任务已实拖验证）。

---

## 实施进展（2026-07-06 更新）

- **✅ 便宜修复已完成并验证（Playwright/Edge 18/18）**：
  - **参数表达式求值**（审计 C-1）：`NovelArguments.float()/int()` 增加轻量表达式求值器（`+ - * / ()`、数字、变量引用），对齐原版 `GlobalCalculator`。**防回归**：仅在非纯数值且含运算符/引用已知变量时求值，`Set`/`on`/`black` 等仍回退。
  - **变量系统 + `setval`**（C-2）：模块级 `novelVars` 变量表（对应 `NovelModelVal`），`setval(key,valueExpr)` 写入；实拖 `miA.delay = miA.delay+0.5` 正确算得 `0.5`。
  - **`popupbgfade`**（145 剧情）：全屏 overlay 淡入到 `rgba(r,g,b,a)`，复用 `screen.colorFade`。
  - **`silhouette`**：角色剪影（`characterFilter` 加 `brightness()`）。
  - **`sectionbreak`**：显式注册为 no-op（消除静默丢弃）。
- **❌ dot / object 子系统：确认在本 Web 播放器不可行，不实现**。可行性尖刺（Cpp2IL 反编译 + UnityPy 探查）证实：`dotbgload(folder,path)` 加载的是 Addressables 预制体 `Assets/Project/LazyAssets/General/Ingame/StageAssets/{folder}/Prefabs/{folder}{path}.prefab`，**不是平面图**——单个场景预制体含 **939 GameObject / 518 MeshRenderer / 106 ParticleSystem / 51 Animation / 35 Mesh**（网格+粒子+动画的完整 Unity 场景）；dot 对象含 `MeshRenderer`/`NovelDotSpineComponent`(Spine)。忠实还原＝把 Unity 网格/粒子场景渲染搬进浏览器，成本≈移植关卡渲染，不切实际。**如需 dot 场景，唯一现实途径是用 Unity 工程离线渲染成图片/视频再喂给播放器**（失去镜头交互）。

---

## A. 覆盖率缺口：未实现的命令（核心发现）

判定方法：数据中出现的命令名（去 `async` 归并）若不在 `buildCommandRegistry`（`app.js`）注册表内，即命中 `NoopCommand`。计数格式 `命令(总次数/波及剧情数)`。

### A-1. `dot*` 点阵/像素场景子系统 —— 35 个命令名、5080 次、约 86 个剧情
> 游戏名"Dot Abyss（ドットアビス）"的核心像素表现层。对应原游戏类 `NovelCmdDot*` + 视图 `NovelDotBGView / NovelDotObjectView / NovelDotCameraView / NovelDotPrefabView / NovelDotBalloonView` 等一整套。

高频项：`dotbgshow(450/86st)` `dotbgload(449/86st)` `dotbgblur(701/72st)` `dotbgdelete(253/53st)` `dotcamerazoom(1161/77st)` `asyncdotcameramove(1132/84st)` `dotcameramove` `dotcameralensmove` `dotcamerainit` `dotcameratargetobject/off` `dotmove/asyncdotmove(249/10st)` `dotscale` `dotprefabload(131/8st)` `dotprefabshow/delete/rotate/scale/arcmove` `dotassetload/show` `asyncdotshake/shakeall` `asyncdotarcmove` `asyncdotjump` `asyncdotobjectskilltrigger/rotate`。
- **参数语义示例**（取自反编译）：`dotbgload` → `(folder, path, tag)`；`dotcamerazoom` → `(zoom, time, moveType)`。
- **可见影响**：这些剧情的像素场景（像素背景、镜头推拉/移动、像素小人/道具运动）**完全不显示**，只剩对话文本。
- **补齐成本：高**。需要：(1) 新提取器——把 `dotbgload(folder,path,...)` 等寻址映射到 bundle 并提取像素背景/精灵；(2) 在 app.js 新建一套 dot 场景渲染（相机变换 + 像素图层）。

### A-2. `object*` 通用精灵子系统 —— 13 个命令名、1599 次、约 11 个剧情
> 对应原游戏 `NovelCmdDotObject*`（注意：类名带 `Dot`，但脚本命令名不带，如类 `NovelCmdDotObjectLoad` ↔ 命令 `objectload`）。

项：`asyncobjectmotion(634)` `objectmotion(304)` `objectdirect(168)` `objectemo/asyncobjectemo` `objectload(104/11st)` `objectshow/hide` `objectdelete` `asyncobjectshadow` `asyncobjectdirect`。
- **参数语义示例**：`objectload` → `(tag, talker, assetId)`。
- **可见影响**：这 11 个剧情里的精灵对象（含其 motion/表情/朝向/阴影）不显示。集中在少数剧情，但单个剧情内出现极密（objectmotion 单剧情数百次）。
- **补齐成本：高**（同样需要资源提取 + 渲染，与 dot 子系统共享底层）。

### A-3. 零散未实现 —— 6 个、198 次
| 命令 | 次数/剧情 | 原游戏作用 | 影响 | 是否需实现 |
|---|---|---|---|---|
| `popupbgfade` | 145/145st | 弹窗背景淡入淡出（`NovelPopupBgFade`） | 覆盖面最广的单项；缺一个过渡淡化效果 | 建议（低成本、纯视觉） |
| `setval` | 27/5st | 设置运行时变量（`NovelModelVal.SetVal`） | 变量/条件内容失效 | 视是否有条件分支 |
| `silhouette` | 13/6st | 剪影效果 | 少数剧情剪影缺失 | 可选 |
| `balloonload` | 10/10st | 像素对话气泡（`NovelDotBalloonView`） | 属 dot 子系统 | 随 dot 子系统 |
| `changematerial` | 2/1st | 换材质/shader | 极少 | 可选 |
| `sectionbreak` | 1/1st | 章节分隔标记（`NovelCmdSectionBreak`） | 结构标记，**无视觉效果，Noop 兜底可接受** | 否 |

---

## B. 缺失的玩家级子系统/功能（原引擎有、app.js 无）

以下由原游戏 `Project.Novel.Novel*` 类集反推。多数在 r18 线性剧情里用不到，但如果目标是"完整还原播放器"则属缺口：

| 功能 | 原游戏类 | 对应命令 | 在 r18 数据中 | 说明 |
|---|---|---|---|---|
| **选项分支** | `NovelModelSelect` `NovelSelectView` `NovelSelectButtonComponent` | `select` `selectwait` `inputwait` | 未出现 | 分支选择；r18 剧情线性，故未用 |
| **跳过/快进** | `NovelSkipPopup` `NovelSkipPopupController` | `fastplaycheck` `fastplaystart` | 未出现 | app.js 只有 Auto，无 Skip |
| **历史回看** | `NovelModelMessageLog` `NovelLogPopup*` `NovelLogItem*` `NovelLogPopupVoiceMessage` | —（UI 功能） | — | 台词 backlog + 回看语音重播；app.js 无 |
| **变量系统** | `NovelModelVal`（`GetVal/SetVal`） | `setval` | 5 剧情 | 见 A-3 / C-2 |
| **选项/设置弹窗** | `NovelOptionPopup` `NovelPauseManager` | —（UI） | — | 播放中暂停/设置 |
| **Ruby 注音** | `Ruby` `NovelLetter` `NovelLetterPool` | —（文本渲染） | — | 原版逐字排版+注音；app.js 直接 innerHTML |
| **消息插入/打断** | `NovelModelMessageInterrupt` | — | — | 打断当前台词的机制 |

> 备注：`select/skip/backlog/option` 属"是否要把它做成完整 ADV 播放器"的产品决策，不是 bug。列出供取舍。

---

## C. 实现有误 / 与原版差异

### C-1. 参数不支持表达式求值（**已确认**，反汇编证据）
- **原版**：`Absf.Novel.NovelArguments.GetFloat(index,default)` / `GetInt` 的方法体（ISIL @ RVA `0x89BFA0`）逻辑为：取 `args[index]` → `TryParse` 为数值；**失败则调用 `GlobalCalculator`（`Func<string,float>`，对象偏移 `0xB8` 的委托）把字符串当表达式求值**；再失败才返回 default。即参数可以写成表达式/变量。
- **app.js**：`NovelArguments.float()`（`app.js:607`）只 `Number.parseFloat`，`int()` 只 `parseInt`——**遇到表达式或变量名会得到 NaN → 回退 default**，行为与原版不一致。
- **影响**：取决于 r18 脚本是否使用表达式参数（估计少见，但存在即错）。**严重度：中低**。

### C-2. 变量系统整体缺失（结构性）
- 原版有 `NovelModelVal`（键→float 变量表）+ `setval` 写入。app.js 无变量表，`setval` 被 Noop。
- 任何"依据变量决定显示/分支"的内容在 app.js 中都不生效。r18 数据里 `setval` 仅 5 个剧情、27 次，配合无 `select`，**实际影响很小**，但属功能缺口。

### C-3. 未做穷尽的逐命令正确性核对（范围说明）
本次对**控制流做了抽查**并未发现问题：`labeljump`（`app.js:3114`）为无条件跳转，与原版 `NovelCmdLabelJump` 一致；`jump`（`app.js:4343`）是**角色/精灵弹跳动画**（非 goto），与原版语义一致；`plot`/`endof`/`sectionbreak` 为结构标记，Noop 兜底可接受。
- 其余已实现命令（如各 chara\*/still/subimage/prefab 的逐参数下标与时序）**未逐条比对方法体**。完整的 ISIL 方法体已落盘（见附录），可作为后续逐命令核对的依据。

---

## D. 忠实还原、工作正常的部分（占绝大多数）

以下命令/子系统 app.js 已实现且经前序任务实拖验证：对话（`message/l2dmessage/dotmessage/messagetextcenter/messagetextunder/title/window`）、charastand 立绘 + face 差分、Live2D（`live2dinit/l2dshow/l2dmotion/l2dmessage` + 口型）、音频（`bgmplay/bgmstop/bgmfade/bgvplay/seplay/seplayingame/sestop/voice/loadvoice`）、画面演出（`fade/crossfade/transition*/blur/screeneffect/colorfade/linework`）、相机（`cameramove/camerazoom`）、角色变换（`chara{load,move,scale,face,emo,focus*,pose,mask,reaction,color,show,hide,item}`）、`still*/subimage*/prefab*`、`shake*`、`jump`(弹跳)、`wait/waitorclick/auto`、`labeljump` 控制流。**这部分是播放器的主体，功能正常。**

---

## E. 建议路线（按 影响×成本 排序，供决策，本报告不实现）

1. **`popupbgfade`（低成本 / 145 剧情）**：纯视觉过渡，最划算的单项补齐。
2. **dot 背景层（中高成本 / ~86 剧情）**：先做 `dotbgload/dotbgshow/dotbgdelete/dotbgblur` + `dotcamera*`（像素背景 + 镜头变换），覆盖 dot 子系统里波及面最大的部分。需先解决 dot 背景的资源提取。
3. **dot/object 精灵与 prefab（高成本 / 少数剧情但密集）**：`object*` + `dotprefab*` + `dotobject*`，资源提取 + 渲染工程量大，波及剧情少（~11–15 个），优先级可后置。
4. **参数表达式求值（C-1，低成本）**：给 app.js 的 `float()/int()` 加一个轻量表达式求值兜底，贴近原版。
5. **玩家级功能（Skip/Backlog/Select，产品决策）**：按是否要做成完整 ADV 播放器再定。

---

## 附录

### 三方对照（命令名，去 async 归并）
- 原游戏命令类：**172**（`re/cs_out/DiffableCs/Project/Project/Novel/NovelCmd*`）。
- app.js 注册基名：**101**（`buildCommandRegistry`）。
- r18 数据实际使用基名：**129**。
- 数据用到但 app.js 未实现（含 async 变体展开）：**54 个命令名 / 6877 次**（详见 A 节）。
- 原游戏有、但 r18 数据未使用、app.js 也未实现（引擎能力，非本仓库缺陷）：`select` `selectwait` `inputwait` `fastplaycheck` `fastplaystart` `tonecover` `uimode` `list` `file` `rotate` `arcmove` `focuson/focusout` `emotiondelete` 及多种 dot 变体等。

### 命名注意
原游戏**类名 ≠ 脚本命令名**：如类 `NovelCmdDotObjectLoad` 对应命令 `objectload`（不带 `dot`）、类 `NovelCmdBGMPlay` 对应 `bgmplay`。命令字符串由运行期注册（非类名直接小写）。

### 复现 / 后续核对的工具产物（scratchpad，未入库）
- `re/Cpp2IL.exe`（2022.1.0-pre-release.21）+ `re/ga.dll`(GameAssembly) + `re/gm.dat`(metadata)。
- `re/cs_out/DiffableCs/`：全类结构 + 字段 + 枚举 + 方法 RVA（26017 文件）。命令类在 `.../Project/Project/Novel/`。
- `re/isil_out/IsilDump/`：全方法体 ISIL/x86 反汇编（708MB）。命令方法体在 `.../Project/Project/Novel/NovelCmd*.txt`；`NovelArguments` 在 `.../Absf/Absf/Novel/NovelArguments.txt`。
- 重跑：`Cpp2IL.exe --force-binary-path ga.dll --force-metadata-path gm.dat --force-unity-version 6000.3.8 --use-processor attributeinjector --output-as <diffable-cs|isil> --output-to <dir>`。
