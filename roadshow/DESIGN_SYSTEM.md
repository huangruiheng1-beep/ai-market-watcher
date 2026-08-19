# 市场观察助手 · 马赛克设计系统 v1

> 本文件是 `roadshow/index.html` 的设计语言提炼。下一个网页照此执行，保证视觉统一。
> 设计驱动：Impeccable 前端设计原则 · 马赛克（Mosaic）美学方向。

---

## 一、设计 DNA：马赛克

**核心隐喻**：界面是一面由色块瓦片拼成的马赛克墙，不是一张张飘浮的卡片。

落点（必须做到）：
- **2px 间隙拼色块**：所有多元素容器用 `gap: 2px` + 深色底色（`--ink`），让相邻色块之间露出一条细缝，像瓦片之间的勾缝。
- **色块即容器**：分区/卡片直接用 4 色之一作整块背景，不要白底卡片 + 阴影。
- **非对称网格**：避免均匀的 3×3 卡片墙。用 `grid-column: span N` 让大小不一的色块拼出节奏。
- **强对比、克制数量**：只有 4 个颜色，靠面积比例和位置制造张力，不靠多色堆砌。

**不要做**：
- 毛玻璃 / 玻璃拟态（glassmorphism）
- 紫蓝渐变、霓虹点缀、发光描边（AI slop）
- 圆角卡片 + 通用阴影
- 居中一切、英雄指标模板

---

## 二、颜色系统（4 色，不可增减）

### 色板

| 角色 | 名称 | HEX | OKLCH 近似 | 用途 |
|------|------|-----|-----------|------|
| **暗锚** | Ink 墨 | `#0B1120` | `oklch(16% 0.03 264)` | 深色底、文字（浅底上）、勾缝底色 |
| **浅底** | Bone 米 | `#F5EFE0` | `oklch(94% 0.02 80)` | 浅色底、文字（深底上） |
| **主强调** | Cobalt 钴蓝 | `#1E40AF` | `oklch(45% 0.19 264)` | 数据/技术感强调、主 CTA 配色 |
| **次强调** | Vermillion 朱砂 | `#DC2626` | `oklch(56% 0.22 27)` | 金融"红涨"、警示、关键动作 |

辅助色阶（同色微调，不算新色）：
- `--ink-2: #161E33`（墨的提亮一档，用于深色容器分层）
- `--bone-2: #EDE4D0`（米的压暗一档，用于浅色容器分层）
- `--cobalt-2: #2952E0`（钴蓝提亮，用于深底上的可读蓝）
- `--vermillion-2: #B91C1C`（朱砂压暗，hover 态）

### CSS 变量（直接复制）

```css
:root {
  --ink: #0B1120;
  --ink-2: #161E33;
  --bone: #F5EFE0;
  --bone-2: #EDE4D0;
  --cobalt: #1E40AF;
  --cobalt-2: #2952E0;
  --vermillion: #DC2626;
  --vermillion-2: #B91C1C;

  --text-on-ink: var(--bone);
  --text-on-bone: var(--ink);
  --text-on-cobalt: var(--bone);
  --text-on-vermillion: var(--bone);
}
```

### 用色规则

1. **60-30-10**：Bone/Ink 占 60%（大面积底），Cobalt 30%（强调容器），Vermillion 10%（关键动作/警示）。朱砂因为醒目，越少越有力。
2. **深浅交替**：相邻 section 强制交替 Ink ↔ Bone，用 Cobalt/Vermillion 打破节奏。避免连续两个同色底。
3. **文字色 = 底色的反色**：Ink 底上用 `--text-on-ink`（Bone），Bone 底上用 `--text-on-bone`（Ink），依此类推。**永远不在彩色底上用灰色文字**（会发死）。
4. **不用纯黑 #000 / 纯白 #fff**。最暗是 Ink，最亮是 Bone。
5. **红色 = 涨/正向/警示**（中国习惯），不要用红表示"下跌/负面"。

---

## 三、字体系统（系统字体，断网可用）

**不引外部字体**。整站用系统字体栈，保证本地双击打开和断网都正常。

```css
--font-display: "SF Pro Display", -apple-system, BlinkMacSystemFont, "Segoe UI",
                "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", system-ui, sans-serif;
--font-body:    -apple-system, BlinkMacSystemFont, "Segoe UI",
                "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", system-ui, sans-serif;
--font-mono:    ui-monospace, "SF Mono", "Cascadia Mono", "Roboto Mono", Menlo, Consolas, monospace;
```

### 字号阶梯（流体 clamp）

| 角色 | clamp | 用途 |
|------|-------|------|
| Hero H1 | `clamp(2.6rem, 6vw, 4.8rem)` | 首屏大标题 |
| Section 标题 | `clamp(2rem, 4.5vw, 3.4rem)` | 各板块 H2 |
| CTA 大字 | `2.2rem`（固定）/ 手机 `1.7rem` | GitHub 行动区 |
| 副标题/lead | `clamp(1rem, 1.4vw, 1.18rem)` | 板块导语 |
| 正文 | `clamp(16px, 1.05vw, 18px)`（body） | 段落 |
| 小标签 | `0.72–0.82rem` + `letter-spacing: 0.15–0.2em` + uppercase | sec-label、step-num、role |
| 代码 | `0.78–0.88rem` mono | 命令块、URL |

### 字重对比

- **标题用 800–900**（display），**正文用 400**。差距要大，不要 600 配 400。
- 小标签用 600–700 + 大字距 + uppercase，制造"技术感"而不靠等宽字体装。

### 关键排版细节

- 标题 `letter-spacing: -0.02em`（收紧），小标签 `letter-spacing: 0.15–0.2em`（撑开）。
- 标题 `line-height: 1.0–1.1`，正文 `line-height: 1.5–1.6`。
- 代码块 `white-space: pre` + `overflow-x: auto`（横向滚动，不换行）。

---

## 四、间距与布局

### 间距 token（流体）

```css
--pad-x: clamp(1.5rem, 6vw, 5rem);   /* 左右边距 */
--pad-y: clamp(3rem, 8vw, 7rem);     /* 板块上下 */
--gap: clamp(1.2rem, 2.5vw, 2.4rem);
--gap-sm: clamp(0.6rem, 1.2vw, 1.1rem);
```

### 容器

- 板块内层 `.sec-inner { max-width: 1200px; margin: 0 auto; }`
- 导航内层 `max-width: 1400px`

### 马赛克网格三式（核心布局手法）

**式一：勾缝拼色块**（多元素容器）
```css
.grid {
  display: grid;
  gap: 2px;              /* 关键：2px 缝 */
  background: var(--ink);/* 缝的颜色 */
  border: 2px solid var(--ink);
}
.grid > .tile { background: <4色之一>; padding: 1.5–2rem; }
```

**式二：非对称跨列**（五工具那种）
```css
grid-template-columns: repeat(6, 1fr);
grid-auto-rows: minmax(180px, auto);
.tool-1 { grid-column: 1 / 3; grid-row: 1; }   /* 跨2列 */
.tool-2 { grid-column: 3 / 5; grid-row: 1; }
.tool-3 { grid-column: 5 / 7; grid-row: 1; }
.tool-4 { grid-column: 1 / 4; grid-row: 2; }   /* 跨3列 */
.tool-5 { grid-column: 4 / 7; grid-row: 2; }
```

**式三：横向步进流**（工作流五步）
```css
grid-template-columns: repeat(5, 1fr);
/* 相邻步用 border-right: 2px 分隔 */
/* 奇数/特定步用强调色填充打破节奏 */
```

---

## 五、组件库

### 1. 顶部导航（固定 + 马赛克标记）
- 固定顶部，`background: --ink`，`border-bottom: 3px solid --vermillion`。
- 品牌标记：22×22 朱砂方块 + 11×22 钴蓝方块拼成双色 mark（马赛克 logo）。
- 桌面：链接横排，hover 变钴蓝底。
- 手机（≤720px）：汉堡按钮（3 条线 → X 动画），下拉面板 max-height 过渡，点链接收起。

### 2. 按钮
```css
.btn {
  display: inline-flex; align-items: center; gap: 0.5rem;
  padding: 0.9rem 1.5rem;
  font-family: var(--font-mono); font-size: 0.85rem;
  font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase;
  border: 2px solid currentColor;
}
.btn-primary { background: var(--vermillion); color: var(--bone); border-color: var(--vermillion); }
.btn-ghost   { background: transparent; color: var(--bone); border-color: var(--bone); }
```
- **主按钮 = 朱砂底**（关键动作），**次按钮 = 透明 + 描边**。
- 按钮文字用 mono 字体 + uppercase + 大字距，制造"命令"感。

### 3. 小标签（sec-label / role / step-num）
- mono 字体，0.72–0.82rem，700 字重，`letter-spacing: 0.15–0.2em`，uppercase。
- 背景取对比色（深底上放钴蓝/朱砂小块，浅底上放墨块）。
- 格式：`01 · 问题` / `TOOL 03` / `STEP 02` / `DAY 01`。

### 4. 代码块
```css
.code-block {
  font-family: var(--font-mono); font-size: 0.85rem;
  background: var(--ink); border-left: 3px solid var(--vermillion);
  padding: 0.9rem 1rem; white-space: pre; overflow-x: auto;
}
```
- 左侧 3px 朱砂竖线是签名特征。
- prompt 用钴蓝、flag 用琥珀黄（仅这一处破例用第 5 色 `#FBBF24` 作语法高亮，不算调色板色）。

### 5. 勾缝色块卡（problem-tile / day / rule）
- 无圆角、无阴影，纯色块 + 2px 缝。
- 顶部 4px 强调色条（rule 用，区分不同规则）。
- 内部：小标签编号 → 大标题 → 说明，纵向排列。

### 6. 视频占位框
- 16:9，棋盘格背景（CSS 双向渐变模拟马赛克纹理）+ 2px Bone 描边。
- 中央朱砂播放方块（hover 放大 1.08）。
- **占位符必须诚实**：写"地址待发布"，不编造链接/播放量。

---

## 六、动效

- **滚动揭示**：IntersectionObserver，元素进视口加 `.in` 类，`opacity 0→1` + `translateY(24px→0)`，缓动 `cubic-bezier(0.16, 1, 0.3, 1)`（ease-out-quart），0.6s。
- **汉堡菜单**：max-height 过渡 0.28s ease-out-quart；三线 → X 用 transform 旋转。
- **按钮/链接 hover**：0.15–0.18s 颜色/背景切换。
- **禁用**：bounce/elastic 回弹、布局属性动画、自动播放轮播。
- **尊重** `prefers-reduced-motion`：关掉所有过渡和滚动平滑。

---

## 七、中英文切换（i18n）

**双轨方案**（断网可用，无 i18n 库依赖）：

1. **整段切换**：`<span class="t-zh">中文</span><span class="t-en">English</span>` 并排，CSS 按当前语言只显示一条。
2. **语言状态**：`<html lang="zh-CN">` / `lang="en"`，JS 切换 + `localStorage` 记忆偏好。
3. **切换器**：右上 `中 / EN` 双按钮，active 态朱砂底。

```css
.t-zh, .t-en { display: none; }
html[lang="zh-CN"] .t-zh { display: inline; }
html[lang="en"] .t-en { display: inline; }
```

---

## 八、响应式策略

| 断点 | 行为 |
|------|------|
| > 820px | 桌面完整网格 |
| ≤ 820px | 双列网格退单列（tools 退 2 列） |
| ≤ 720px | 汉堡菜单启用；hero 马赛克改全宽背景纹理（opacity 0.32）；flow 改横向滚动；字号/padding 收紧 |
| ≤ 380px | 导航只留 mark 色块；CTA 纵向拉伸 |

原则：
- 用 `clamp()` 做流体尺寸，少用硬断点跳变。
- **手机不藏关键功能**，只重排。
- 代码块永远横向滚动，不换行不撑爆。
- hero 在手机上：装饰让位于内容（马赛克降透明度当背景）。

---

## 九、内容纪律（来自 PPT 计划，不可破）

- **不预测涨跌、不连券商、不自动下单**——必须写明边界。
- **占位符诚实**：未确认的 GitHub URL / 视频地址一律占位，不编造链接、播放量、用户数、收益率、胜率、媒体评价。
- **不展示**：API Key、本地绝对路径、`.env`、缓存、SQLite、真实输出截图。
- **红涨绿跌**（中国习惯），金融语境下红色 = 正向。

---

## 十、速查：新建一个页面要做什么

1. 复制 `:root` 的 4 色变量 + 3 个字体栈 + 间距 token。
2. body：`background: --bone; color: --text-on-bone; font-family: --font-body`。
3. 第一个 section 用 Ink 底，第二个用 Bone，交替下去，Cobalt/Vermillion 打断节奏。
4. 任何多元素容器用「式一：勾缝拼色块」（gap:2px + ink 底）。
5. 标题 800–900 + mono 小标签（编号 + uppercase）。
6. 主按钮朱砂、次按钮描边；代码块左侧朱砂竖线。
7. 加中英双轨 span + 右上切换器 + localStorage。
8. 加 IntersectionObserver 滚动揭示 + prefers-reduced-motion 兜底。
9. ≤720px 汉堡菜单 + 网格退列 + hero 装饰降级。
10. 自检：没有圆角卡片阴影、没有紫蓝渐变、没有毛玻璃、没有纯黑纯白。
