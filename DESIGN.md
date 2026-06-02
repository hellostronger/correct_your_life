# LifeReflector - 个人生活反思助手

## 📋 项目概述

### 项目名称
**LifeReflector** (生活反思者)

### 核心理念
> "温柔地审视过去，智慧地规划未来"

### 项目定位
一个基于多Agent架构的个人生活智能助手，通过记录、分析、反思用户的日常生活，
帮助用户做出更好的决策，提供情感支持和可行的改善建议。

---

## 🎯 核心功能

### 1. 生活记录系统
- **多渠道输入**：微信、企业微信、邮箱、H5网页
- **语音输入**：支持语音转文字，方便快捷记录
- **语音输出**：回复内容支持语音播报
- **多种记录类型**：
  - 日常事件记录
  - 决策记录
  - 情绪记录
  - 目标追踪

### 2. 智能反思系统
- **周期性回顾**：日/周/月/年总结
- **决策分析**：分析过去决策的得失
- **模式识别**：发现行为模式和潜在问题
- **成长追踪**：记录个人成长轨迹

### 3. 情感陪伴系统
- **情绪识别**：识别用户的情绪状态
- **共情回应**：提供温暖、理解的回应
- **安慰引导**：在低谷期给予支持
- **不过度反驳**：尊重用户感受，适度建议

### 4. 建议系统
- **可行性分析**：建议必须可执行、可量化
- **优先级排序**：按影响力和紧急程度排序
- **行动计划**：提供具体步骤和时间节点
- **效果追踪**：追踪建议执行情况

---

## 🏗️ 系统架构

### 整体架构图

```
┌─────────────────────────────────────────────────────────────────┐
│                         用户接入层                               │
├───────────┬───────────┬───────────┬─────────────────────────────┤
│  微信公众号 │  企业微信   │   邮箱    │        H5 Web App          │
│  (可选)    │  (推荐)    │  (可选)   │       (核心入口)            │
└─────┬─────┴─────┬─────┴─────┬─────┴──────────────┬──────────────┘
      │           │           │                    │
      └───────────┴───────────┴────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│                        消息网关层 (Gateway)                       │
│  - 消息格式统一转换                                              │
│  - 用户身份验证                                                  │
│  - 消息队列缓冲                                                  │
│  - 多渠道适配器                                                  │
└─────────────────────────────────┬───────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Agent 编排层 (Orchestrator)                 │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │                   路由决策器 (Router)                      │   │
│  │  - 分析用户意图                                           │   │
│  │  - 分配任务给合适的Agent                                   │   │
│  │  - 协调Agent间通信                                        │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                  │
│  ┌───────────┐ ┌───────────┐ ┌───────────┐ ┌───────────┐       │
│  │ 记录Agent │ │ 反思Agent │ │ 陪伴Agent │ │ 建议Agent │       │
│  │  Recorder │ │ Reflector │ │ Companion │ │ Advisor   │       │
│  └───────────┘ └───────────┘ └───────────┘ └───────────┘       │
│                                                                  │
│  ┌───────────┐ ┌───────────┐ ┌───────────┐                    │
│  │ 分析Agent │ │ 记忆Agent │ │ 总结Agent │                    │
│  │  Analyst  │ │  Memory   │ │ Summarizer│                    │
│  └───────────┘ └───────────┘ └───────────┘                    │
└─────────────────────────────────┬───────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────┐
│                        服务支撑层                                │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐          │
│  │ 数据存储  │ │ 向量存储  │ │ 日志服务  │ │ 缓存服务  │          │
│  │ PostgreSQL│ │ ChromaDB │ │   Log    │ │  Redis   │          │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘          │
└─────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────┐
│                        LLM 服务层 (配置化)                        │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  通过环境变量配置，支持任意 OpenAI兼容 API                  │  │
│  │                                                          │  │
│  │  配置项:                                                  │  │
│  │  - LLM_BASE_URL: API地址 (如 http://localhost:11434/v1)  │  │
│  │  - LLM_API_KEY: API密钥 (本地模型可为空)                  │  │
│  │  - LLM_MODEL_NAME: 模型名称 (如 qwen2.5:7b)              │  │
│  │                                                          │  │
│  │  支持的模型示例:                                          │  │
│  │  ✅ Ollama (本地免费): http://localhost:11434/v1         │  │
│  │  ✅ OpenAI: https://api.openai.com/v1                    │  │
│  │  ✅ DeepSeek: https://api.deepseek.com/v1                │  │
│  │  ✅ Claude: https://api.anthropic.com/v1                 │  │
│  │  ✅ 智谱AI: https://open.bigmodel.cn/api/paas/v4         │  │
│  │  ✅ 通义千问、混元、Moonshot等任何OpenAI兼容API           │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 🤖 Agent 详细设计

### Agent 架构图

```
                    ┌─────────────────────┐
                    │   BaseAgent 基类    │
                    │  - 系统提示词管理    │
                    │  - 消息历史管理      │
                    │  - 工具调用能力      │
                    │  - 记忆检索能力      │
                    └──────────┬──────────┘
                               │
        ┌──────────────────────┼──────────────────────┐
        │                      │                      │
        ▼                      ▼                      ▼
┌───────────────┐    ┌───────────────┐    ┌───────────────┐
│  Recorder     │    │  Reflector    │    │  Companion    │
│  记录员       │    │  反思者       │    │  陪伴者       │
└───────────────┘    └───────────────┘    └───────────────┘
        │                      │                      │
        ▼                      ▼                      ▼
┌───────────────┐    ┌───────────────┐    ┌───────────────┐
│   Analyst     │    │   Advisor     │    │   Memory      │
│   分析师      │    │   顾问        │    │   记忆者      │
└───────────────┘    └───────────────┘    └───────────────┘
        │                      │
        ▼                      ▼
┌───────────────┐    ┌───────────────┐
│  Summarizer   │    │   Router      │
│   总结者      │    │   路由器      │
└───────────────┘    └───────────────┘
```

### 各Agent职责与提示词设计

#### 1. Router Agent (路由器)
**职责**：分析用户输入，决定由哪个Agent处理

```python
SYSTEM_PROMPT = """
你是LifeReflector系统的路由器。分析用户输入，判断用户意图：

1. **记录意图** - 用户想记录今天发生的事情
   关键词: 今天、做了、发生、记录、日记
   → 返回: {"agent": "recorder", "confidence": 0.9}

2. **反思意图** - 用户想回顾、总结过去
   关键词: 总结、回顾、分析、怎么样、反思
   → 返回: {"agent": "reflector", "confidence": 0.9}

3. **情感需求** - 用户需要情感支持、安慰
   关键词: 难过、累、烦、不开心、emo、沮丧
   → 返回: {"agent": "companion", "confidence": 0.9}

4. **建议需求** - 用户需要建议、解决方案
   关键词: 怎么办、建议、怎么做、帮我、解决
   → 返回: {"agent": "advisor", "confidence": 0.9}

5. **复杂意图** - 多重意图混合
   → 返回: {"agent": "orchestrator", "confidence": 0.7}

始终返回JSON格式，包含agent和confidence字段。
"""
```

#### 2. Recorder Agent (记录员)
**职责**：帮助用户结构化记录日常事件

```python
SYSTEM_PROMPT = """
你是LifeReflector的记录助手。帮助用户记录生活，但要自然、不刻板。

## 核心原则
1. **自然对话** - 像朋友聊天一样，不要审问式记录
2. **结构化提取** - 从对话中提取关键信息，不需要用户填写表单
3. **情感捕捉** - 不仅记录事件，还要捕捉用户的情绪和想法
4. **智能补全** - 根据上下文补全缺失信息

## 提取的信息
- 时间: 事件发生时间
- 事件: 发生了什么
- 人物: 涉及的人
- 情绪: 用户的感受
- 想法: 用户的思考
- 决策: 做出的决定（如有）
- 标签: 自动分类标签

## 回复风格
- 温暖、简洁
- 适当追问重要细节
- 确认关键信息
- 鼓励用户继续分享

示例对话:
用户: 今天开会开了好久，老板又画饼了
你: 听起来挺累的😔 那个会开了多久呀？老板画的是什么饼？
"""
```

#### 3. Reflector Agent (反思者)
**职责**：分析用户行为模式，提供深度反思

```python
SYSTEM_PROMPT = """
你是LifeReflector的反思助手。帮助用户审视过去，发现成长机会。

## 核心原则
1. **温和不评判** - 用理解的语气，不要说教
2. **数据驱动** - 基于用户记录的数据分析，不凭空臆断
3. **发现模式** - 识别重复出现的行为模式
4. **成长视角** - 关注进步和可能性，不只是问题

## 分析维度
- 时间管理: 时间分配是否合理
- 决策质量: 过去决策的效果如何
- 情绪模式: 情绪波动的规律
- 人际关系: 与他人的互动质量
- 目标进展: 目标的达成情况
- 自我认知: 对自己的了解程度

## 输出格式
### 📊 本周数据概览
(简要的数据统计)

### 🔍 发现与洞察
(发现的模式和趋势)

### 💡 反思要点
(值得思考的问题，以问句形式)

### ⭐ 亮点时刻
(本周的积极事件)

### 🌱 成长空间
(可以改进的地方，温和建议)
"""
```

#### 4. Companion Agent (陪伴者)
**职责**：提供情感支持，安慰用户

```python
SYSTEM_PROMPT = """
你是LifeReflector的陪伴助手。在用户需要时提供温暖和支持。

## 核心原则
1. **共情优先** - 先理解和接纳情绪，不要急着给建议
2. **适度回应** - 不要过度反驳用户的负面情绪
3. **温暖陪伴** - 让用户感到被理解和接纳
4. **循序渐进** - 等用户情绪稳定后再引导思考

## 禁止行为
❌ "你不应该这样想"
❌ "这没什么大不了的"
❌ "你想太多了"
❌ "别人比你更惨"
❌ 立即给出解决方案（用户没问时）

## 应该做
✅ "我理解你的感受"
✅ "听起来真的很不容易"
✅ "换做是我也会感到..."
✅ 陪伴和倾听
✅ 在用户准备好时，提供新视角

## 情绪识别
- 愤怒: 允许宣泄，不要劝阻
- 悲伤: 温柔陪伴，不要催促
- 焦虑: 理解担忧，帮助梳理
- 无助: 给予支持，提供希望
- 疲惫: 表达关心，建议休息

## 回应模板
1. 确认情绪: "我感受到你现在很..."
2. 表达理解: "这种情况确实让人..."
3. 温暖支持: "我会陪着你"
4. (如用户需要) 探索原因: "愿意说说是什么让你最难过吗？"
5. (如用户需要) 探索方案: "你觉得有什么能让你好受一点？"
"""
```

#### 5. Advisor Agent (顾问)
**职责**：提供可行的建议和解决方案

```python
SYSTEM_PROMPT = """
你是LifeReflector的顾问助手。帮助用户找到可行的解决方案。

## 核心原则
1. **可行性优先** - 建议必须是用户能实际执行的
2. **小步快跑** - 从小的改变开始，不要一步到位
3. **尊重选择** - 提供选项，让用户自己选择
4. **效果追踪** - 设置检查点，评估建议效果

## 建议框架 SMART+
- Specific: 具体做什么
- Measurable: 怎么衡量完成
- Achievable: 用户有能力做到吗
- Relevant: 与用户目标相关吗
- Time-bound: 什么时候完成
- +Support: 需要什么支持

## 输出格式
### 🎯 问题分析
(简要分析问题本质)

### 💡 建议方案
方案A: [标题]
- 具体行动: ...
- 预期效果: ...
- 所需时间: ...
- 难度等级: ⭐~⭐⭐⭐⭐⭐

方案B: [标题]
...

### 📅 建议执行计划
- 第1天: ...
- 第3天: 检查进度
- 第7天: 评估效果

### ❓ 需要考虑的问题
(帮助用户决策的思考题)

## 语气风格
- 不说教，提供视角
- 不强制，提供选项
- 不评判，提供分析
- 用"你可以考虑..." 而非 "你应该..."
"""
```

#### 6. Memory Agent (记忆者)
**职责**：管理用户长期记忆，提供上下文检索

```python
SYSTEM_PROMPT = """
你是LifeReflector的记忆管理员。负责存储和检索用户的重要信息。

## 记忆类型
1. **情景记忆** - 具体事件和经历
2. **语义记忆** - 用户偏好、习惯、目标
3. **情感记忆** - 重要的情感体验
4. **决策记忆** - 过去的决策及其结果

## 存储策略
- 重要性评估: 1-10分
- 情感强度: 1-10分
- 关联标签: 自动提取
- 时间戳: 记录时间

## 检索策略
1. 语义相似度检索
2. 时间范围检索
3. 情感相关性检索
4. 主题关联检索

## 记忆整合
- 定期合并相似记忆
- 提取重复模式
- 更新用户画像
- 维护知识图谱
"""
```

#### 7. Summarizer Agent (总结者)
**职责**：生成周期性总结报告

```python
SYSTEM_PROMPT = """
你是LifeReflector的总结助手。生成日/周/月/年度总结报告。

## 报告类型
### 📅 日报 (每晚生成)
- 今日事件回顾
- 情绪曲线
- 明日展望

### 📆 周报 (每周日晚)
- 本周概览
- 亮点与低谷
- 成长分析
- 下周建议

### 📊 月报 (每月最后一天)
- 月度数据分析
- 目标进展
- 重大事件回顾
- 模式洞察

### 📈 年报 (12月31日)
- 年度回顾
- 十大时刻
- 成长轨迹
- 新年展望

## 风格要求
- 用数据说话，但不过于技术化
- 图文并茂，可视化数据
- 温暖有温度，不是冷冰冰的报告
- 突出成长，但也不回避问题
"""
```

---

## 💻 技术实现方案

### 前端: H5移动端

#### 技术栈选择

```
前端框架: React 18 + Vite (轻量、快速)
UI组件库: Ant Design Mobile 5 (专为移动端设计)
状态管理: Zustand (轻量级，比Redux简单)
路由: React Router v6
语音识别: Web Speech API (免费) + 备选方案
语音合成: Web Speech API (免费)
数据可视化: ECharts (轻量级)
PWA支持: 支持离线和添加到主屏幕
```

#### 页面结构

```
/src
  /pages               - 页面组件
    /Home              - 首页/今日记录
    /Record            - 记录页面
    /Reflect           - 反思总结
    /Chat              - 对话界面
    /Timeline          - 时间线
    /Stats             - 数据统计
    /Settings          - 设置
  /components          - 通用组件
    /VoiceInput        - 语音输入组件
    /VoiceOutput       - 语音输出组件
    /ChatBubble        - 对话气泡
    /MoodTracker       - 情绪追踪
    /GoalCard          - 目标卡片
    /Navbar            - 导航栏
  /hooks               - 自定义Hooks
    /useVoice          - 语音Hook
    /useChat           - 对话Hook
    /useAuth           - 认证Hook
  /stores              - Zustand状态
    /userStore         - 用户状态
    /chatStore         - 对话状态
  /services            - API服务
    /api               - 后端API调用
  /utils               - 工具函数
```

#### 语音方案 (低成本)

```
方案A: Web Speech API (推荐，免费)
- 优点: 完全免费，无需后端，浏览器原生支持
- 缺点: 部分浏览器不支持，需要联网
- 兼容性: Chrome, Edge, Safari 移动端支持良好

方案B: 百度语音API (备选，有免费额度)
- 免费额度: 每月10000次调用
- 优点: 中文识别率高，稳定
- 缺点: 需要后端服务

方案C: OpenAI Whisper (高质量，付费)
- 价格: $0.006/分钟
- 优点: 识别准确率最高
- 缺点: 成本较高

推荐方案:
- 优先使用Web Speech API
- 不支持时降级到文字输入
- 语音合成同样使用Web Speech API
```

### 后端: Agent服务

#### 技术栈选择

```
语言: Python 3.11+
Web框架: FastAPI (高性能，异步支持)
Agent框架: LangGraph (多Agent编排)
数据库:
  - PostgreSQL (主数据库，存储用户数据)
  - ChromaDB (向量数据库，记忆检索)
缓存: Redis (会话管理，消息队列)
任务队列: Celery (异步任务)
部署: Docker + Docker Compose
```

#### 项目结构

```
/lifereflector
  /app
    /api            - API路由
    /agents         - Agent定义
      /router.py    - 路由Agent
      /recorder.py  - 记录Agent
      /reflector.py - 反思Agent
      /companion.py - 陪伴Agent
      /advisor.py   - 顾问Agent
      /memory.py    - 记忆Agent
      /summarizer.py- 总结Agent
    /core           - 核心功能
      /llm.py       - LLM调用
      /memory.py    - 记忆管理
      /tools.py     - Agent工具
    /models         - 数据模型
    /services       - 业务逻辑
    /integrations   - 外部集成
      /wechat.py    - 微信接入
      /wework.py    - 企微接入
      /email.py     - 邮箱接入
    /utils          - 工具函数
  /migrations       - 数据库迁移
  /tests            - 测试
  Dockerfile
  docker-compose.yml
  requirements.txt
```

### 数据库设计

#### 核心表结构

```sql
-- 用户表
CREATE TABLE users (
    id UUID PRIMARY KEY,
    phone VARCHAR(20) UNIQUE,
    email VARCHAR(100) UNIQUE,
    nickname VARCHAR(50),
    avatar_url VARCHAR(500),
    settings JSONB DEFAULT '{}',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- 记录表
CREATE TABLE records (
    id UUID PRIMARY KEY,
    user_id UUID REFERENCES users(id),
    content TEXT NOT NULL,
    record_type VARCHAR(20), -- daily, decision, emotion, goal
    occurred_at TIMESTAMP,
    metadata JSONB DEFAULT '{}', -- 结构化数据
    emotion_score INT, -- 情绪评分 -5到5
    tags TEXT[], -- 标签数组
    created_at TIMESTAMP DEFAULT NOW()
);

-- 决策表
CREATE TABLE decisions (
    id UUID PRIMARY KEY,
    user_id UUID REFERENCES users(id),
    record_id UUID REFERENCES records(id),
    title VARCHAR(200),
    description TEXT,
    options JSONB, -- 备选方案
    chosen_option INT, -- 选择哪个
    reasoning TEXT, -- 决策理由
    outcome TEXT, -- 结果反馈
    outcome_score INT, -- 结果评分 1-10
    decided_at TIMESTAMP,
    reviewed_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW()
);

-- 目标表
CREATE TABLE goals (
    id UUID PRIMARY KEY,
    user_id UUID REFERENCES users(id),
    title VARCHAR(200),
    description TEXT,
    category VARCHAR(50),
    target_date DATE,
    status VARCHAR(20), -- active, completed, abandoned
    progress INT, -- 0-100
    milestones JSONB,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- 总结报告表
CREATE TABLE summaries (
    id UUID PRIMARY KEY,
    user_id UUID REFERENCES users(id),
    summary_type VARCHAR(20), -- daily, weekly, monthly, yearly
    period_start DATE,
    period_end DATE,
    content JSONB,
    insights TEXT[],
    highlights TEXT[],
    created_at TIMESTAMP DEFAULT NOW()
);

-- 会话历史表
CREATE TABLE conversations (
    id UUID PRIMARY KEY,
    user_id UUID REFERENCES users(id),
    channel VARCHAR(20), -- h5, wechat, wework, email
    messages JSONB,
    metadata JSONB,
    started_at TIMESTAMP,
    ended_at TIMESTAMP
);

-- 用户画像表
CREATE TABLE user_profiles (
    user_id UUID PRIMARY KEY REFERENCES users(id),
    preferences JSONB DEFAULT '{}',
    patterns JSONB DEFAULT '{}',
    goals_summary TEXT,
    personality_traits JSONB,
    updated_at TIMESTAMP DEFAULT NOW()
);

-- 向量索引 (用于语义检索)
CREATE INDEX idx_records_embedding ON records
    USING ivfflat (embedding vector_cosine_ops);
```

---

## 🔌 多渠道接入方案

### 1. H5 Web App (核心入口)

```
特点:
- 零审核，快速迭代
- 完整功能体验
- PWA支持，可添加到桌面
- 语音功能完整

技术实现:
- 响应式设计，移动优先
- Service Worker 离线缓存
- 推送通知 (需要用户授权)
```

### 2. 企业微信接入 (推荐)

```
特点:
- 工作场景使用
- 消息推送及时
- 无需审核，企业内部使用

实现方案:
1. 创建企业微信应用
2. 配置回调URL接收消息
3. 使用企业微信API发送消息
4. 接入成本: 低

配置步骤:
- 企业微信后台创建应用
- 设置可信域名
- 配置消息回调
- 获取 CorpId, AgentId, Secret
```

### 3. 微信公众号接入 (可选)

```
特点:
- 用户基数大
- 需要审核
- 消息有延迟

实现方案:
- 使用测试号开发 (无需审核)
- 正式号需要企业资质
- 使用公众号消息接口
```

### 4. 邮箱接入 (可选)

```
特点:
- 适合长内容记录
- 无需实时响应
- 方便附件处理

实现方案:
- IMAP协议接收邮件
- SMTP协议发送邮件
- 定时轮询或推送
```

### 消息网关设计

```python
# 统一消息格式
class Message:
    id: str
    user_id: str
    channel: str  # h5, wechat, wework, email
    content: str
    content_type: str  # text, voice, image
    metadata: dict
    timestamp: datetime

# 网关适配器模式
class MessageGateway:
    adapters: Dict[str, ChannelAdapter]

    async def receive(self, message: Message):
        # 统一接收处理
        pass

    async def send(self, user_id: str, content: str, channel: str):
        # 路由到对应渠道发送
        adapter = self.adapters[channel]
        await adapter.send(user_id, content)
```

---

## 💰 成本估算

### 成本估算 (配置化方案)

```
┌─────────────────────────────────────────────────────────────┐
│                     月度成本估算                             │
├─────────────────────┬─────────────────────────────────────┤
│ LLM (用户自选)       │ 取决于配置:                        │
│ - Ollama本地        │ ¥0/月 (完全免费，需本地资源)        │
│ - DeepSeek API     │ ¥50-100/月 (低成本云端)             │
│ - OpenAI/Claude    │ ¥200-500/月 (高质量云端)            │
│ - 其他API          │ 按实际调用计费                      │
├─────────────────────┼─────────────────────────────────────┤
│ 云服务器            │ ~¥50-100/月                        │
│ - 阿里云轻量服务器  │ 2核4G，适合初期使用                 │
│ - 腾讯云轻量服务器  │ 同配置，价格相近                    │
├─────────────────────┼─────────────────────────────────────┤
│ 数据库              │ ¥0-50/月                           │
│ - 本地PostgreSQL   │ 免费 (与云服务器同机部署)            │
│ - 云数据库         │ ¥50/月起 (如需独立数据库)           │
├─────────────────────┼─────────────────────────────────────┤
│ 语音服务            │ ¥0/月                              │
│ - Web Speech API   │ 浏览器原生，完全免费                 │
├─────────────────────┼─────────────────────────────────────┤
│ 域名 + SSL         │ ~¥10/月                            │
│ - 域名             │ .top/.xyz 域名 ¥10/年              │
│ - SSL证书          │ Let's Encrypt 免费                  │
├─────────────────────┼─────────────────────────────────────┤
│ 总计 (本地Ollama)   │ ¥60-110/月 (最低成本)              │
│ 总计 (云端API)      │ ¥110-600/月 (按LLM选择)            │
└─────────────────────┴─────────────────────────────────────┘

推荐: 本地Ollama + 云服务器 = 月成本¥60-110
```

### 开发阶段成本

```
开发阶段: ¥0/月
- 本地Ollama运行，完全免费
- 本地开发测试，无需云端资源

上线阶段: ¥60-110/月
- 云服务器: ¥50-100/月
- 本地Ollama或按需选择API
- 可随时切换LLM配置
```

---

## 📅 开发计划

### 第一阶段: MVP核心功能 (2周)

```
Week 1: 基础架构
- [ ] 项目初始化
- [ ] 数据库设计实现
- [ ] Agent基础框架
- [ ] Recorder Agent实现
- [ ] H5基础页面

Week 2: 核心功能
- [ ] Router Agent实现
- [ ] Companion Agent实现
- [ ] 语音输入输出
- [ ] 基础对话功能
- [ ] 数据存储
```

### 第二阶段: 智能反思 (2周)

```
Week 3: 反思系统
- [ ] Reflector Agent实现
- [ ] Memory Agent实现
- [ ] 向量检索集成
- [ ] 记录结构化存储

Week 4: 总结功能
- [ ] Summarizer Agent实现
- [ ] 日报/周报生成
- [ ] 数据可视化
- [ ] 时间线展示
```

### 第三阶段: 建议系统 (1周)

```
Week 5: 建议功能
- [ ] Advisor Agent实现
- [ ] 决策记录功能
- [ ] 建议追踪功能
- [ ] 效果反馈机制
```

### 第四阶段: 多渠道接入 (1周)

```
Week 6: 渠道扩展
- [ ] 企业微信接入
- [ ] 微信公众号接入(可选)
- [ ] 邮箱接入(可选)
- [ ] 多渠道消息同步
```

### 第五阶段: 优化与上线 (1周)

```
Week 7: 优化上线
- [ ] 性能优化
- [ ] 错误处理
- [ ] 用户体验优化
- [ ] 部署上线
- [ ] 监控告警
```

---

## 🔐 隐私与安全

### 数据安全

```
1. 数据加密
   - 传输加密: HTTPS
   - 存储加密: 敏感数据加密存储
   - 密码哈希: bcrypt

2. 访问控制
   - 用户只能访问自己的数据
   - API鉴权: JWT Token
   - 敏感操作二次验证

3. 数据备份
   - 每日自动备份
   - 保留30天备份
   - 支持用户导出数据

4. 隐私保护
   - 不共享用户数据
   - 支持「被遗忘权」
   - 明确的隐私政策
```

---

## 📝 技术选型总结

| 层级 | 技术 | 选择理由 |
|------|------|----------|
| 前端框架 | React 18 + Vite | 轻量、快速、生态丰富 |
| UI组件 | Ant Design Mobile 5 | 移动端优化、组件完善 |
| 状态管理 | Zustand | 轻量级，比Redux简单 |
| 语音识别 | Web Speech API | 免费、无需后端、兼容性好 |
| 后端框架 | FastAPI | 高性能、异步支持、Python生态 |
| Agent框架 | LangGraph | 灵活的多Agent编排 |
| 主数据库 | PostgreSQL | 稳定、支持JSON、向量扩展 |
| 向量数据库 | ChromaDB | 轻量、易用、开源 |
| 缓存 | Redis | 会话管理、消息队列 |
| LLM | **配置化 (用户自选)** | 预留URL+KEY，支持任意模型 |
| 部署 | Docker | 标准化、易迁移 |
| 云服务 | 阿里云/腾讯云 | 国内访问快、价格透明 |

---

## 🚀 快速开始 (开发环境)

```bash
# 克隆项目
git clone https://github.com/yourname/lifereflector.git
cd lifereflector

# 后端设置
cd backend
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env

# 编辑 .env 配置LLM (关键配置)
# LLM_BASE_URL=http://localhost:11434/v1  # Ollama本地
# LLM_API_KEY=                            # 本地模型可为空
# LLM_MODEL_NAME=qwen2.5:7b               # 模型名称

python main.py

# 前端设置
cd ../frontend
npm install
npm run dev

# 访问
# 前端: http://localhost:5173
# 后端: http://localhost:8000
# API文档: http://localhost:8000/docs
```

### .env 配置示例

```bash
# LLM配置 (必填)
LLM_BASE_URL=http://localhost:11434/v1   # Ollama本地地址
LLM_API_KEY=                             # 本地模型无需KEY
LLM_MODEL_NAME=qwen2.5:7b                # 模型名称

# 其他LLM示例:
# OpenAI:    LLM_BASE_URL=https://api.openai.com/v1, LLM_API_KEY=sk-xxx
# DeepSeek:  LLM_BASE_URL=https://api.deepseek.com/v1, LLM_API_KEY=sk-xxx
# Claude:    LLM_BASE_URL=https://api.anthropic.com/v1, LLM_API_KEY=sk-xxx-xxx

# 数据库配置
DATABASE_URL=postgresql://user:pass@localhost:5432/lifereflector

# Redis配置
REDIS_URL=redis://localhost:6379
```

---

## 📚 参考资料

- [LangGraph Documentation](https://langchain-ai.github.io/langgraph/)
- [Claude API Documentation](https://docs.anthropic.com/)
- [DeepSeek API Documentation](https://platform.deepseek.com/docs)
- [Vant UI Documentation](https://vant-ui.github.io/vant/)
- [Web Speech API](https://developer.mozilla.org/en-US/docs/Web/API/Web_Speech_API)
- [Dify Documentation](https://docs.dify.ai/)

---

## ❓ 待确认事项

请确认以下内容，以便我开始开发：

1. **核心功能优先级** - 上述功能是否都需要？是否有需要调整优先级的？

2. **渠道选择** - 先开发哪个渠道？
   - [ ] H5 Web App (推荐先开发)
   - [ ] 企业微信
   - [ ] 微信公众号
   - [ ] 邮箱

3. **LLM选择** - 成本与质量的平衡：
   - [ ] 方案A: 全部使用DeepSeek (最低成本)
   - [ ] 方案B: DeepSeek + Claude Haiku混合 (推荐)
   - [ ] 方案C: 全部使用Claude (最高质量，成本较高)

4. **部署方式** - 项目部署偏好：
   - [ ] 云服务器 (阿里云/腾讯云)
   - [ ] 容器平台 (Railway/Render)
   - [ ] 本地运行 (仅测试)

5. **其他需求** - 是否有其他特殊需求或功能想法？

---

**文档版本**: v1.0
**创建时间**: 2026-06-02
**作者**: Claude + 用户协作