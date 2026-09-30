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

# 附录 A：在 stock-advisor 上的落地路线（2026-09-30 起）

> **为什么有这份附录**：上面 v1.0 的设计是"从零建一个 LifeReflector"。
> 实际情况是它被**并进了已有的 stock-advisor**（FastAPI + 云 PostgreSQL +
> 微信 iLink 通道 + 现成 LLM 配置），而不是另起一个项目。已落地的部分和
> 接下来的 C / D 阶段记录在这里，避免重复造轮子。

## A.1 已完成：B —— 微信入站 + LLM 对话

**位置**：`stock-advisor/wx_inbound.py`（收）、`wx_chat.py`（回）

```
微信 App ──getUpdates 35s长轮询──► wx_inbound.poll_once
                                        │  1. 解析 item_list 取 type=1 文本
                                        │  2. record_inbound_context() 缓存 context_token
                                        │  3. 存游标 get_updates_buf → sa_wx_ilink
                                        ▼
                                   wx_chat.reply_fn
                                        │  合并窗口(默认60s)内攒多条
                                        │  → llm_advisor.ask(SYSTEM, 历史+本次)
                                        ▼
                                   ilink/bot/sendmessage（带 context_token）
```

**为什么要 B 打底**：C（遥控指令）和 D（情绪记录）都只是"换一个
`reply_fn`"。传输层（长轮询/游标/context_token/保活）一行都不用改。
反过来先做 C，`reply_fn` 会被业务逻辑占死，B 就得重写。

**iLink 踩坑记录（都抄自官方 `@tencent-weixin/openclaw-weixin` 2.4.9）**：

| 坑 | 真相 | 出处 |
|---|---|---|
| "必须用户先给 bot 发一条消息才能推" | **错**。`notifystart` 声明在线即可，绑定后自动调 | `src/channel.ts::startAccount` |
| 推送全灭，报 `ret=-2 prepare failed` | 同上，客户端没声明在线 | 同上 |
| context_token 是出站必需 | **错**，缺了只是 warn 一句照发 | `src/messaging/send.ts:109` |
| 服务停久了 token 会失效 | **对**。长轮询就是保活，第三方 bridge README 也这么说 | `src/monitor/monitor.ts` |
| 消息反复重收 / 收不到 | 游标 `get_updates_buf` 必须持久化 | `src/storage/sync-buf.ts` |

**其余照抄项**：35s 长轮询、连续 3 次失败退避 30s、否则 2s 重试、
`errcode -14`(token 过期) 冷却 1 小时（`src/api/session-guard.ts`）、
服务端 `longpolling_timeout_ms` 动态调间隔。

---

## A.2 已完成：C —— 微信当系统遥控器

**位置**：`stock-advisor/wx_commands.py`。传输层（`wx_inbound`）与指令层完全解耦，
落地时一行都没改 —— 这就是 B 先打底的回报。

**指令按副作用分级**（这是 C 的关键设计，不是事后补的）：

| 级别 | 指令 | 副作用 | 处理 |
|---|---|---|---|
| **只读** | 持仓 / 自选 / 状态 / 帮助 | 拉行情、读库 | 直接执行 |
| **有代价** | 复盘 / 简报 | **调 Claude（花钱）+ 写 reports + 推微信** | 必须回一次性确认码 |

「复盘」看起来像查询，实际会烧一次 LLM 调用。用户在微信里随手打两个字就
触发一整轮生成、还往 reports 落文件、当成"已推送"通知出去 —— 所以一律
二次确认。设计稿原本把这条标成待定，落地时取**保守默认**：宁可多问一句。

确认码用 `secrets.token_hex(3)`（6 位十六进制）而不是「确认」二字或时间戳：
- 纯文本「确认」太容易手滑
- 时间戳可预测，别人猜出来就能替你触发报告
- 曾把命令名编进 token（`postmarket71092` 共 14 字符），而确认消息正则只允许
  `\S{4,12}` —— **bot 自己发出去的确认码自己认不出来**，永远执行不了

数据源全部复用现有函数（`_holdings_with_pnl` / `fetch_quotes` /
`daily_reports`），不重写一份 —— 微信里看到的持仓必须和「我的持仓」页
逐位一致，重写只会让两边算出不同的数字。

| 指令 | 动作 | 复用 |
|---|---|---|
| `持仓` | 持仓 + 市值 + 浮动盈亏（按市值降序） | `_holdings_with_pnl` / `_derive_holdings` |
| `自选` | 自选股 + 今日涨跌（默认前 15 只） | `sa_watchlist` + `fetch_quotes` |
| `状态` | 各守护线程最近一次运行时间 | `wechat_mp.get_status` + 5 个 `*_state` |
| `帮助` | 列出全部指令与代价提示 | 静态 |
| `复盘` / `简报` | 触发 Claude 生成（二次确认后） | `daily_reports.GENERATORS` |

`状态` 只列**确实存在**的 state。曾用 `hasattr` 兜底一个 app.py 里根本没定义的
`_alerts_state`，结果那条永远显示"未跑过"—— 看起来正常，其实是在骗人。

**别名单一真源**：别名表只在 `wx_commands.ALIASES` 一处，`wx_chat.match_command`
转发过去。原先两边各写一份，`handle()` 拿原始文本查自己的表、忽略了归一化结果，
于是最自然的写法「复盘」（别名）匹配不到规范名「盘后复盘」，回复变成"指令没接上"。
这种接缝 bug 两边单测各自都过，只有端到端才暴露。

---

## A.3 待做：D —— 日记 / 情绪洞察 / 人际变化

**目标**：记日记或直接聊天，让系统看见**情绪**和**身边人交互的变化**，
并在冲动做决定时拦一下。

> 2026-09-30 曾实现过一版完整方案（`mood.py`，800 行 + 4 张表 + 13 个 API），
> **主动撤回了** —— 需求太模糊、且有 5 个不该由实现方替用户拍板的问题
> （见 A.4）。传输层和 LLM 层（B）已经就位，重做时直接接。

### 三个能力，两层实现

| | 本地规则（零费用、可离线、行为可预测） | LLM（可选） |
|---|---|---|
| 情绪分/标签/涉及的人 | 词典 + 规则 | 补语义（反讽、双关） |
| 冲动识别 | 触发词表 | 补无关键词的隐含表达 |
| 趋势/复现/人际变化 | SQL 聚合 | 归因与解释 |
| 对话回应 | ❌ | ✅（= 已有的 B） |

**本地层不是"凑数的降级"** —— "今天第二次因为他说的话想砸东西"这种模式，
规则表就能抓到；LLM 负责规则表看不见的部分（语气、隐含动机）。

### 「意气用事」怎么判

不用单点打分，用**三个信号叠加**（任一都不可靠，叠加才可信）：

1. **情绪高唤醒** —— 词典命中（气死/愤怒/受不了/凭什么/恶心/想砸…）
2. **决策意图** —— 动作词 × **时效词**。时效词是关键：同样"我想清仓"，
   「想了三晚还是想清仓」和「现在就想清仓」不该同等对待
   （实测这两个分别打 34 分和 95 分）
3. **不可逆性** —— 清仓/辞职/拉黑/离婚这类没法撤销的权重更高

命中后**不算"你错了"，只把决定推迟**：把"想做的事 + 理由"存进冷静期表，
锁 N 小时（默认 24）后推送一次「你后来做了吗？结果如何？」。

**为什么"锁一段时间"而不是"直接禁止"**：冲动决定的伤害几乎全部来自
"立刻执行"，而延迟本身就是干预。这一问是整个模块的复利来源 ——
只记冲动不记结局，三个月后你只会记得"我情绪波动大"，
不会知道这些冲动到底靠不靠谱。

### 数据模型（草图）

```sql
sa_mood_entries (kind, text, mood, arousal, tags[], people[], triggers[],
                 urge, urge_want, analysis, urge_fired)   -- 日记/聊天统一
sa_mood_people  (name, relation, mention, pos/neg_count, mood_sum,
                 last_seen)                                -- 身边人档案
sa_mood_urges   (entry_id, want, reason, domain, irreversibility,
                 remind_at, outcome[did|didnt|regret], outcome_note)  -- 冷静期
sa_mood_insights(scope, ref, title, body, source[rule|llm])          -- 洞察留档
```

`sa_mood_people` 需人工登记（无 NER 依赖，**宁可漏也不猜** —— 正则猜出来的
"小王/老李"会污染整份人际统计）。这条取舍要 A.4 第 2 条确认。

---

## A.4 重做 D 之前必须先答的 5 个问题

上次就是跳过这 5 条直接写了 800 行，然后被要求全部撤掉。**不要再犯。**

1. **私密内容存哪？** 日记写的是人和关系，比持仓数据敏感一个量级。
   - [ ] 云 PostgreSQL（现状：与 dify 144 张表同库）
   - [ ] 本地 SQLite，永不出机器
   - [ ] 文本列加密存储
   - [ ] 心情表单独拆一个库
   - [ ] 开了 LLM 后内容会发给**第三方网关**（非 Anthropic 官端），网关运营方可见 —— 接受吗？

2. **"身边人"怎么识别？** 决定"人际变化"这个核心价值能不能立住。
   - [ ] LLM 每次从正文抽人名（零摩擦，花钱 + 出网）
   - [ ] 人工登记（私密，摩擦大）
   - [ ] 内置角色词表（父母/老婆/老板/同事…）+ 叠加人工名单
   - [ ] 首次一次性列出常来往的人，之后增量维护

3. **「别意气用事」要多硬？** 软拦（记录 + 冷静期 + 回访）还是硬拦
   （高冲动状态下**阻止**记录卖出/加自选/让 LLM 动仓）？
   硬拦会和现有的止损/模拟盘流程打架。

4. **危机表述（自伤念头等）怎么处理？** 上次擅自做了"短路 + 给 12356 热线"，
   默认开。可能很对，也可能在多数时候让人反感。应做成开关由用户定。

5. **范围**：一次做完还是先只做"记录 + 情绪曲线"（最省、风险最低），
   把"人际变化"和"冲动拦截"留到确认后再加？

---

## A.5 与 v1.0 设计的偏差

| v1.0 设计 | 实际落地 | 原因 |
|---|---|---|
| 独立项目 lifereflector | 并入 stock-advisor | 已有 FastAPI/PG/微信/LLM，重复造没有意义 |
| 多 Agent 架构（LangGraph） | 单函数 + 长轮询 | 场景是"个人自用 + 单渠道"，多 Agent 是过度设计 |
| 5 个渠道（H5/公众号/企业微信/邮箱） | 只做微信 iLink | 零成本、零注册；其它渠道各要资质 |
| React + Ant Design Mobile | 沿用 stock-advisor 的单文件 HTML | 同一个项目不引构建链 |
| ChromaDB 向量检索 | 暂不需要 | 日记量级用 SQL 聚合 + `ILIKE` 足够 |
| Redis 会话管理 | 无 | 单用户单进程，PG 足够 |

**保留的**：LLM 配置化（`llm` 段，已在用）、PostgreSQL、FastAPI、
微信作为主要渠道、以及上面「生活记录 / 智能反思 / 情感陪伴」三块的功能意图。

---

**附录创建时间**: 2026-09-30
**状态**: B、C 已完成并验证；D 待排期（先答 A.4 的 5 个问题）