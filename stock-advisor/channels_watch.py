"""微信视频号内容监控：监控博主更新 → 取文本 → 入现有内容监控（sa_news）。

## 为什么不用自己造轮子（AGENTS.md 第 1 条）

视频号**没有公开 API**，网页端也拿不到内容（实测文档原文：「微信视频号内容来自
独立客户端，浏览器不能直接访问内容」）。所以必须靠 MITM 代理 + PC 版微信。
开源方案已调研（2026-10-06，全部 api.github.com 核实）：

| 仓库 | star | License | 最近提交 | 结论 |
|---|---|---|---|---|
| `nobiyou/wx_channel` | 2699 | **MIT** | **2026-10-05** | ✅ **用这个** |
| `ltaoo/wx_channels_download` | 9626 | NOASSERTION(Commons Clause) | 2026-09-30 | 上游鼻祖，License 非 OSI |
| `qiye45/wechatVideoDownload` | 5857 | **NONE** | 2026-10-05 | 无 License = 保留所有权利 |
| `will-17173/electron-...downloader` | 66 | MIT | 2025-07-21 | 停滞 1 年 |
| `KingsleyYau/WeChatChannelsDownloader` | 60 | NONE | 2020-09-27 | 6 年前 |

`wx_channel` 提供 HTTP API（代理 2025 / API **2026**），本模块只依赖它：

| 用途 | 端点 |
|---|---|
| 账号搜索 | `GET /api/channels/contact/search?keyword=` |
| **博主视频列表（监控用）** | `GET /api/channels/contact/feed/list?username=&next_marker=` |
| 视频详情（描述文案/时间/点赞） | `GET /api/channels/feed/profile?object_id=&nonce_id=` |
| **评论列表** | `GET /api/channels/feed/comment/list?object_id=&next_marker=` |
| 评论导出 | `POST /api/channels/feed/comment/export` |
| 下载视频（ASR 前置） | 见 `download_video()` |

**硬前提**：必须有一台装了 **PC 版微信并已登录**的机器跑 `wx_channel`
（还有 `wx_channel_radar.exe` 监控变体，需 `config.yaml` 的 `radar_enabled`，
默认关）。101 是无桌面的 Linux，**跑不了**；本模块因此把 API 地址做成配置项，
默认 `http://127.0.0.1:2026`。

## 文本内容分两层，别混为一谈

- **L1 描述文案**：`feed/profile` 直接给，零成本 —— 本模块的默认产出
- **L2 语音转写**：要下 mp4 再 ASR。**ASR 走可插拔 `AsrProvider`**：

实测（2026-10-06）本项目现有 key **不能直接调 SiliconFlow**：
- `config.yaml` 的 `api_key` 打到官方域名 `api.siliconflow.cn` 返回
  `401 {"code":30014,"message":"Token is invalid."}` → 它是 101 网关的 key，不是硅基的
- 101 网关 `/v1/models` 的 92 个模型里**零个 ASR**；试调返回
  `503 model_not_found: No available channel for model ...`

且 SiliconFlow 官方 `/v1/audio/transcriptions` 的模型枚举是**封闭的两个**：
`FunAudioLLM/SenseVoiceSmall`、`TeleAI/TeleSpeechASR`（付费 key 还可用
`Qwen/Qwen3-Omni-30B-A3B-{Instruct,Thinking}`）。所以配 `asr.model` 时
**不要写不存在模型名** —— 会拿到 503 而不是 404，看起来像服务故障。

## 闭环与落盘纪律

```
订阅(博主 username) → 轮询 feed/list → 按 object_id 判重 → feed/profile 取描述
   → （可选）下载 mp4 + ASR → 情绪判定 → ① JSONL 落盘 ② 写 sa_news（url 唯一去重）
   → 关联股票写 sa_news_related → 复用现有推送/模拟盘上下文
```

**只有真正落盘后才标记 seen**。理由：崩在中间时若已标记 seen，
这条视频就永久丢了 —— 与 AGENTS.md 里「长任务每步落盘」同源。
ASR 失败**不丢条目**，标 `asr_error` 保留 L1 文本。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import subprocess
import time
import wave
from datetime import datetime

import requests

DEFAULT_API = "http://127.0.0.1:2026"
SOURCE = "wx_channels"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def _now():
    return datetime.now()


def _data_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


# ---------------- wx_channel API 客户端 ----------------

class WxChannelError(RuntimeError):
    """wx_channel 接口调用失败。**带上 code 与 message**，别只抛字符串。"""


class WxChannelClient:
    """wx_channel HTTP API 客户端。

    所有响应都带 `code` 字段，**0 才算成功**。非 0 一律抛
    `WxChannelError(code, message)` —— 否则「接口报错」会被当成「没有新视频」，
    于是监控静默失效（AGENTS.md 第 2 条的老坑）。
    """

    def __init__(self, base_url: str = DEFAULT_API, token: str = "",
                 timeout: int = 30):
        self.base = base_url.rstrip("/")
        self.token = token or ""
        self.timeout = timeout

    def _headers(self):
        h = {"User-Agent": UA}
        if self.token:
            h["X-Local-Auth"] = self.token
        return h

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{self.base}{path}"
        try:
            r = requests.get(url, params=params or {}, headers=self._headers(),
                             timeout=self.timeout)
        except requests.RequestException as exc:
            raise WxChannelError("network", f"{url} 请求失败: {exc}") from exc
        if r.status_code >= 500:
            raise WxChannelError("http", f"{url} 返回 {r.status_code}")
        try:
            j = r.json()
        except ValueError as exc:
            raise WxChannelError("bad_json",
                                f"{url} 返回非 JSON（HTTP {r.status_code}）: "
                                f"{r.text[:120]}") from exc
        code = j.get("code", 0)
        if code not in (0, None):
            raise WxChannelError(code, f"{path} code={code} msg={j.get('message') or j.get('msg')}")
        return j

    # -- 具体接口 ----------------------------------------------------------

    def status(self) -> dict:
        return self._get("/api/channels/status")

    def search_contact(self, keyword: str) -> list:
        """账号搜索。返回**扁平化**的 `[{nickname, username, signature, ...}]`。

        真实路径是 `data.data.infoList[]`（2026-10-06 实测，v5.7.10），
        每项把账号信息包在 `.contact` 里 —— 原实现在这里找 `data.list`，
        **恒返回 []**，看起来像「搜不到」，实则路径全错（AGENTS.md 第 2 条
        「200 + 空」同一类陷阱，只是空的是解析结果）。
        """
        j = self._get("/api/channels/contact/search", {"keyword": keyword})
        raw = (_dig(j, ("data", "data", "infoList"))
               or _dig(j, ("data", "infoList"))
               or _dig(j, ("data", "list")) or _dig(j, ("list",)) or [])
        out = []
        for it in raw:
            if not isinstance(it, dict):
                continue
            c = it.get("contact") if isinstance(it.get("contact"), dict) else {}
            flat = dict(it)
            # 把 contact.* 提到顶层，同时保留原字段（高亮字段等上层可能用）
            for k, v in (c or {}).items():
                flat.setdefault(k, v)
            flat.setdefault("nickname", c.get("nickname") or it.get("highlightNickname"))
            out.append(flat)
        return out

    def contact_feed_list(self, username: str, next_marker: str = "") -> dict:
        """博主视频列表。返回 {'items': [...], 'next_marker': str}。

        文档给的参数是 `username`（**不是 nickname**）。
        真实结构（2026-10-06 实测）：列表在 **`data.data.object[]`**，
        分页标记是 **`data.data.lastBuffer`**（不是 next_marker）。
        """
        params = {"username": username}
        if next_marker:
            params["next_marker"] = next_marker
        j = self._get("/api/channels/contact/feed/list", params)
        items = (_dig(j, ("data", "data", "object"))
                 or _dig(j, ("data", "object"))
                 or _dig(j, ("data", "list")) or _dig(j, ("list",))
                 or _dig(j, ("data", "feed_list")) or [])
        marker = (_dig(j, ("data", "data", "lastBuffer"))
                  or _dig(j, ("data", "lastBuffer"))
                  or _dig(j, ("data", "next_marker")) or j.get("next_marker") or "")
        return {"items": items, "next_marker": marker}

    def feed_profile(self, object_id: str, nonce_id: str = "") -> dict:
        """视频详情：描述文案 / 发布时间 / 点赞 / 评论数 等。

        文档强调参数名是 `object_id` / `nonce_id`（不是 objectId/nonceId）。
        真实结构：详情在 **`data.data.object`**（2026-10-06 实测）——
        `data` 那一层只是 `{data, errCode, errMsg, payload}` 外壳，
        拿 `data` 当 profile 会得到一个没有正文的空壳。
        """
        params = {"object_id": object_id}
        if nonce_id:
            params["nonce_id"] = nonce_id
        j = self._get("/api/channels/feed/profile", params)
        return (_dig(j, ("data", "data", "object"))
                or _dig(j, ("data", "object"))
                or _dig(j, ("data", "data"))
                or _dig(j, ("data",)) or _dig(j, ("feed_profile",)) or {})

    def comment_list(self, object_id: str, nonce_id: str = "", comment_id: str = "",
                     next_marker: str = "") -> dict:
        """评论列表。

        真实契约（读 `web/docs/API.md` + `COMMENT_CAPTURE.md`，**不要照 GitHub
        上的示例猜**）：
        - 一级评论要带 `nonce_id`；查某条评论的回复要带 `comment_id`
        - 分页标记字段叫 **`lastBuffer`**（不是 next_marker）
        - 列表在 **`data.data.commentInfo[]`**（两层 data！）
        - 评论项字段：`nickname` / `content` / `commentId` / `replyCommentId` /
          `likeCount` / `expandCommentCount`（回复数）/ `levelTwoComment[]`（二级回复）
          / `createtime`（**秒级**，不是 create_time）
        - 总数在 `data.data.countInfo.commentCount`
        """
        params = {"object_id": object_id}
        if nonce_id:
            params["nonce_id"] = nonce_id
        if comment_id:
            params["comment_id"] = comment_id
        if next_marker:
            params["next_marker"] = next_marker
        j = self._get("/api/channels/feed/comment/list", params)
        inner = _dig(j, ("data", "data")) or {}
        items = inner.get("commentInfo") or _dig(j, ("data", "list")) or []
        marker = inner.get("lastBuffer") or _dig(j, ("data", "next_marker")) or ""
        return {
            "items": items,
            "next_marker": marker,
            "total": (inner.get("countInfo") or {}).get("commentCount", 0),
        }

    # -- 对标雷达：目标管理（2026-10-06 本地 web/js/radar.js 实测路径）--------
    # 注意雷达 API 前缀是 /api/v1/...（与 /api/channels/... 不同）

    def radar_targets(self) -> list:
        j = self._get("/api/v1/radar/targets")
        return j.get("data") or []

    def add_radar_target(self, author_name: str, username: str,
                          interval_minutes: int = 60) -> dict:
        return self._post("/api/v1/radar/targets", {
            "author_name": author_name, "username": username,
            "interval_minutes": int(interval_minutes)})

    def delete_radar_target(self, target_id) -> dict:
        return self._delete(f"/api/v1/radar/targets/{target_id}")

    def _post(self, path: str, payload: dict) -> dict:
        url = f"{self.base}{path}"
        try:
            r = requests.post(url, json=payload, headers=self._headers(),
                              timeout=self.timeout)
        except requests.RequestException as exc:
            raise WxChannelError("network", f"{url} POST 失败: {exc}") from exc
        return self._check(r, path)

    def _delete(self, path: str) -> dict:
        url = f"{self.base}{path}"
        try:
            r = requests.delete(url, headers=self._headers(), timeout=self.timeout)
        except requests.RequestException as exc:
            raise WxChannelError("network", f"{url} DELETE 失败: {exc}") from exc
        return self._check(r, path)

    def _check(self, r, path: str) -> dict:
        if r.status_code >= 500:
            raise WxChannelError("http", f"{path} 返回 {r.status_code}")
        try:
            j = r.json()
        except ValueError as exc:
            raise WxChannelError("bad_json",
                                f"{path} 返回非 JSON（HTTP {r.status_code}）: "
                                f"{r.text[:120]}") from exc
        code = j.get("code", 0)
        if code not in (0, None):
            raise WxChannelError(code, f"{path} code={code} "
                                       f"msg={j.get('message') or j.get('msg')}")
        return j


def _dig(obj, path):
    """按 key 链取值，任一层缺失返回 None（不抛）。"""
    cur = obj
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


# ---------------- ASR（可插拔） ----------------

class AsrProvider:
    name = "base"

    def transcribe(self, path: str) -> str:
        raise NotImplementedError

    def available(self) -> bool:
        return False


class NullAsr(AsrProvider):
    """不转写。只用 L1 描述文案（默认，零成本零依赖）。"""
    name = "null"

    def available(self) -> bool:
        return True

    def transcribe(self, path: str) -> str:
        return ""


class SiliconFlowAsr(AsrProvider):
    """OpenAI 兼容的 `/v1/audio/transcriptions`。

    官方文档：multipart/form-data，字段 `file` + `model`；
    **限制：音频 ≤ 1 小时、≤ 50MB**。响应 `{"text": "..."}`。

    ⚠️ 2026-10-06 实测：**官方文档页的 enum 只列了 2 个模型，是不完整的**。
    用真 key 打 `GET /v1/models` 返回 97 个模型，其中语音类有 9 个：

        XingChenAGI/XingChenASR-V3.2-Ultra      ← 用户指定，实测真实存在
        XingChenAGI/XingChenASR-V3.2
        XingChenAGI/XingChenASR-Diarize-V3.0
        Qwen/Qwen3-ASR-1.7B
        FunAudioLLM/SenseVoiceSmall              （有免费额度）
        TeleAI/TeleSpeechASR
        Qwen/Qwen3-Omni-30B-A3B-Instruct / -Thinking / -Captioner

    **教训**：判断模型是否存在，以 `/v1/models` 的真实返回为准，不要只看文档页
    的枚举 —— 我曾据文档 enum 断言「模型不存在」并写进白名单，被真实请求打脸。

    另一个实测障碍：key 有效、端点通、模型对，但账号**余额不足**时会返回
    `402 {"code":30001,"message":"Sorry, your account balance is insufficient"}`
    —— 401 是 key 问题、402 是欠费，两者要分开报。
    """

    name = "siliconflow"
    KNOWN_MODELS = (
        "FunAudioLLM/SenseVoiceSmall",
        "TeleAI/TeleSpeechASR",
        "Qwen/Qwen3-ASR-1.7B",
        "XingChenAGI/XingChenASR-V3.2-Ultra",
        "XingChenAGI/XingChenASR-V3.2",
        "XingChenAGI/XingChenASR-Diarize-V3.0",
        "Qwen/Qwen3-Omni-30B-A3B-Instruct",
        "Qwen/Qwen3-Omni-30B-A3B-Thinking",
        "Qwen/Qwen3-Omni-30B-A3B-Captioner",
    )

    def __init__(self, api_key: str, model: str = "FunAudioLLM/SenseVoiceSmall",
                 base_url: str = "https://api.siliconflow.cn/v1", timeout: int = 300):
        self.api_key = api_key or ""
        self.model = model or ""
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def available(self) -> bool:
        return bool(self.api_key and self.model)

    def transcribe(self, path: str) -> str:
        if not self.available():
            raise WxChannelError("asr_not_configured", "ASR 未配置 api_key/model")
        if self.model not in self.KNOWN_MODELS:
            # 提前拦：错模型名在服务端是 503 model_not_found，看起来像故障
            raise WxChannelError(
                "asr_bad_model",
                f"模型名 {self.model!r} 不在 SiliconFlow 官方枚举内；"
                f"已知: {', '.join(self.KNOWN_MODELS)}")
        if not os.path.exists(path):
            raise WxChannelError("asr_no_file", f"音频不存在: {path}")
        size = os.path.getsize(path)
        if size > 50 * 1024 * 1024:
            raise WxChannelError("asr_too_big", f"音频 {size/1048576:.1f}MB 超过 50MB 上限")
        try:
            with open(path, "rb") as f:
                r = requests.post(
                    f"{self.base}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    files={"file": (os.path.basename(path), f)},
                    data={"model": self.model},
                    timeout=self.timeout)
        except requests.RequestException as exc:
            raise WxChannelError("asr_network", f"ASR 请求失败: {exc}") from exc
        if r.status_code != 200:
            raise WxChannelError("asr_http",
                                f"ASR {self.classify_http_error(r.status_code, r.text)}"
                                f"（原始 {r.status_code}: {r.text[:160]}）")
        try:
            j = r.json()
        except ValueError as exc:
            raise WxChannelError("asr_bad_json", f"ASR 返回非 JSON: {r.text[:150]}") from exc
        text = (j.get("text") or "").strip()
        if not text:
            raise WxChannelError("asr_empty", "ASR 返回空文本")
        return text

    @staticmethod
    def classify_http_error(status: int, body: str) -> str:
        """把 ASR 的 HTTP 错误翻译成能直接看懂的原因（实测踩过的都在这）。"""
        if status == 401:
            return ("key 无效或无权限 —— 注意项目 config.yaml 里那把是 101 网关的 key，"
                    "不是硅基的；要用硅基官方域名就必须是硅基自己的 key")
        if status == 402:
            return "账号余额不足（SiliconFlow code 30001）—— 充值后再试"
        if status == 404:
            return f"端点或模型不存在（模型名={getattr(self, 'model', '?')}）"
        if status == 429:
            return "被限流"
        if status == 503:
            return ("服务端无该模型的可用渠道。若走 101 网关，说明网关没配 "
                    "SiliconFlow 渠道；若走官方域名，多半是模型名不对或欠费")
        return f"HTTP {status}: {body[:120]}"


class QwenOmniAsr(AsrProvider):
    """Qwen3-Omni 等**多模态对话模型**的音频转写（与 SiliconFlowAsr 完全不同端点）。

    2026-10-06 读 SiliconFlow 官方《Multimodal input》确认：
    - 走 `POST /v1/chat/completions`，**不是** `/v1/audio/transcriptions`
    - 音频作为 content part：`{"type": "audio_url",
      "audio_url": {"url": "data:audio/wav;base64,..."}}`，再跟一个 text part
    - **计费：音频 13 tokens/秒**（官方原文：22.5s 音频 = 292 tokens）
    - 官方没给 omni 的文件大小上限，但 base64 在 JSON body 里（约膨胀 1.33 倍），
      所以这里按分片发送，单片默认 300 秒，避免长视频一次性撑爆 body

    长音频分片：WAV 用 stdlib `wave` 切（不引入依赖、不会重编码）；
    非 WAV（视频号下载下来通常是 mp4/m4a）需要 ffmpeg 先抽音轨，
    没有 ffmpeg 就**明确报错**，不做静默降级。
    """

    name = "qwen_omni"
    # 走 chat 端点的模型（其余走 /audio/transcriptions）
    CHAT_MODELS = (
        "Qwen/Qwen3-Omni-30B-A3B-Instruct",
        "Qwen/Qwen3-Omni-30B-A3B-Thinking",
    )
    TRANSCRIPTION_MODELS = (
        "FunAudioLLM/SenseVoiceSmall",
        "TeleAI/TeleSpeechASR",
        "Qwen/Qwen3-ASR-1.7B",
        "XingChenAGI/XingChenASR-V3.2-Ultra",
        "XingChenAGI/XingChenASR-V3.2",
        "XingChenAGI/XingChenASR-Diarize-V3.0",
    )
    DEFAULT_MODEL = "Qwen/Qwen3-Omni-30B-A3B-Instruct"
    PROMPT = "请把这段音频逐字转写成文字，只输出转写内容本身，不要加解释或引号。"
    # 分片下限：低于 30 秒会把一次转写拆成几十次请求（每次都要付 13 tokens/秒的
    # 音频费 + 冷启动），反而更贵更慢。传更小的值会被夹到这个下限。
    MIN_CHUNK_SECONDS = 30

    def __init__(self, api_key: str = "", model: str = DEFAULT_MODEL,
                 base_url: str = "https://api.siliconflow.cn/v1",
                 timeout: int = 300, chunk_seconds: int = 300):
        self.api_key = api_key or ""
        self.model = model or self.DEFAULT_MODEL
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.chunk_seconds = max(self.MIN_CHUNK_SECONDS,
                                 int(chunk_seconds or 300))

    def available(self) -> bool:
        return bool(self.api_key and self.model)

    # -- 音频切分 ----------------------------------------------------------
    @staticmethod
    def _wav_chunks(path: str, seconds: int):
        """把 WAV 切成不超过 `seconds` 秒的片段（stdlib，不重编码）。

        非 WAV 返回 None —— 调用方据此走 ffmpeg 或报错。
        """
        import wave
        if not path.lower().endswith(".wav"):
            return None
        with wave.open(path, "rb") as w:
            rate, ch, sw, frames = w.getframerate(), w.getnchannels(), \
                w.getsampwidth(), w.getnframes()
            per = max(1, int(rate * seconds))
            out = []
            pos = 0
            while pos < frames:
                w.setpos(pos)
                data = w.readframes(min(per, frames - pos))
                bio = io.BytesIO()
                with wave.open(bio, "wb") as o:
                    o.setnchannels(ch)
                    o.setsampwidth(sw)
                    o.setframerate(rate)
                    o.writeframes(data)
                out.append(bio.getvalue())
                pos += per
            return out or [b""]

    def _ffmpeg_extract(self, path: str) -> str:
        """用 ffmpeg 把视频/音频抽成 16k 单声道 WAV。返回 wav 路径。"""
        exe = shutil.which("ffmpeg")
        if not exe:
            raise WxChannelError(
                "asr_no_ffmpeg",
                f"需要 ffmpeg 把 {os.path.splitext(path)[1] or '该格式'} 转成 WAV，"
                f"本机没找到 ffmpeg（视频号下载通常是 mp4）。装 ffmpeg 或改用 WAV 源")
        out = os.path.join(os.path.dirname(path),
                           os.path.splitext(os.path.basename(path))[0] + "_16k.wav")
        cmd = [exe, "-y", "-i", path, "-vn", "-ac", "1", "-ar", "16000",
               "-c:a", "pcm_s16le", out]
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=600)
        except Exception as exc:
            raise WxChannelError("asr_ffmpeg_failed", f"ffmpeg 执行失败: {exc}") from exc
        if p.returncode != 0 or not os.path.exists(out):
            raise WxChannelError("asr_ffmpeg_failed",
                                f"ffmpeg 退出码 {p.returncode}: "
                                f"{p.stderr[-200:].decode('utf-8', 'ignore')}")
        return out

    # -- 主流程 ------------------------------------------------------------
    def transcribe(self, path: str) -> str:
        if not self.available():
            raise WxChannelError("asr_not_configured", "ASR 未配置 api_key/model")
        if self.model not in self.CHAT_MODELS:
            raise WxChannelError(
                "asr_bad_model",
                f"{self.model!r} 不走 chat 端点。走 chat 的模型: "
                f"{', '.join(self.CHAT_MODELS)}；其余请用 SiliconFlowAsr")
        if not os.path.exists(path):
            raise WxChannelError("asr_no_file", f"音频不存在: {path}")

        src = path
        chunks = self._wav_chunks(path, self.chunk_seconds)
        if chunks is None:
            src = self._ffmpeg_extract(path)
            chunks = self._wav_chunks(src, self.chunk_seconds)

        texts = []
        for i, blob in enumerate(chunks, 1):
            b64 = base64.b64encode(blob).decode()
            payload = {
                "model": self.model,
                "messages": [{"role": "user", "content": [
                    {"type": "audio_url",
                     "audio_url": {"url": f"data:audio/wav;base64,{b64}"}},
                    {"type": "text", "text": self.PROMPT},
                ]}],
                "stream": False,
                "max_tokens": 2000,
            }
            try:
                r = requests.post(f"{self.base}/chat/completions",
                                  headers={"Authorization": f"Bearer {self.api_key}"},
                                  json=payload, timeout=self.timeout)
            except requests.RequestException as exc:
                raise WxChannelError("asr_network", f"ASR 请求失败: {exc}") from exc
            if r.status_code != 200:
                raise WxChannelError(
                    "asr_http",
                    f"第 {i}/{len(chunks)} 片 {SiliconFlowAsr.classify_http_error(r.status_code, r.text)}"
                    f"（原始 {r.status_code}: {r.text[:160]}）")
            try:
                j = r.json()
                text = (j["choices"][0]["message"]["content"] or "").strip()
            except (ValueError, KeyError, IndexError) as exc:
                raise WxChannelError("asr_bad_json",
                                    f"ASR 响应解析失败: {r.text[:150]}") from exc
            if text:
                texts.append(text)
        joined = "\n".join(texts).strip()
        if not joined:
            raise WxChannelError("asr_empty", "ASR 返回空文本")
        return joined


# ---------------------------------------------------------------------------
# 讯飞开放平台 · 语音转写（lfasr，Long Form ASR）
# ---------------------------------------------------------------------------

class XfyunLfasrAsr(AsrProvider):
    """讯飞语音转写：免费额度最大、中文准确率远好于多模态大模型。

    为什么选它而不是别的（2026-10-06 核实）：
    - `语音听写`（流式）**≤60 秒**，视频号视频常常更长，不够用
    - `语音转写 lfasr` 支持 5 小时以内、mp3/m4a/wav/flac/opus，
      免费体验包 **5 小时**（每账户限领 1 次、30 天有效）+ 新用户礼包最高 50 小时/年
    - 支持 **`pd=finance` 金融垂域** 和热词 —— 对财报口播是实打实的准确率提升
    - 网关那个 `nemotron-3-nano-omni` 实测中文覆盖仅 21~54%（音近错），不能用

    **它是异步任务制**（prepare → upload → merge → 轮询 → getResult），
    和 SiliconFlow/Qwen 那种同步一次调用完全不同。所以：
    1. `task_id` 落盘（按音频内容 sha1 索引），重跑**复用**而不是重提 ——
       额度按秒扣，重提一次就是白烧一次；
    2. 同一份音频第二次调用时先查 checkpoint，已完成直接返回文本。

    签名 `signa = base64(HmacSHA1(MD5(app_id + ts), api_secret))`
    —— 已用官方文档给的测试向量逐字验证（见 test_xfyun_signa.py）。
    """

    name = "xfyun_lfasr"
    API = "https://raasr.xfyun.cn/api"
    SLICE_BYTES = 10 * 1024 * 1024     # 官方建议分片 10M
    STATUS_DONE = 9                     # 9=转写结果上传完成，才可 getResult
    STATUS_TEXT = {0: "任务创建成功", 1: "音频上传完成", 2: "音频合并完成",
                   3: "转写中", 4: "转写结果处理中", 5: "转写完成",
                   9: "转写结果上传完成"}
    # 官方错误码 -> 人话（只列我们真会撞到的）
    ERRS = {
        26601: "非法应用信息（app_id / api_secret 不对，或该应用没加「语音转写」服务）",
        26603: "接口频率超限（同一 app_id 每秒不得超过 20 次）",
        26606: "空音频 —— 检查抽出来的音轨是不是静音",
        26607: "语种未授权或已过期",
        26625: "剩余服务时长不足 —— 去 xfyun.cn/service/lfasr 领免费体验包",
        26633: "剩余服务时长不足（同 26625，另一个阶段报）",
        26604: "获取结果次数超限（同一任务最多取 100 次结果）",
        26643: "计算音频时长失败 —— 音频可能加密或损坏",
        26650: "音频格式转换失败 —— 只支持 wav/flac/opus/m4a/mp3",
        26610: "请求参数错误",
    }

    def __init__(self, app_id: str = "", api_secret: str = "",
                 base_url: str = "", pd: str = "finance",
                 hot_words: list | None = None, poll_seconds: int = 15,
                 max_wait: int = 900, data_dir: str = "",
                 target_rate: int = 16000):
        self.app_id = (app_id or "").strip()
        self.api_secret = (api_secret or "").strip()
        self.base = (base_url or self.API).rstrip("/")
        self.pd = pd or ""
        # 热词：单个 ≤16 字、最多 200 个，用 | 分隔
        self.hot_words = [w for w in (hot_words or []) if w][:200]
        self.poll_seconds = max(5, int(poll_seconds or 15))
        self.max_wait = max(60, int(max_wait or 900))
        self.data_dir = data_dir
        self.target_rate = target_rate if target_rate in (8000, 16000) else 16000
        self.task_id = ""

    def available(self) -> bool:
        return bool(self.app_id and self.api_secret)

    # -- 签名 --------------------------------------------------------------
    @staticmethod
    def signa(app_id: str, ts: str, secret: str) -> str:
        """base64(HmacSHA1(MD5(app_id+ts), secret)) —— 文档测试向量已逐字验证。"""
        md5 = hashlib.md5(f"{app_id}{ts}".encode("utf-8")).hexdigest()
        raw = hmac.new(secret.encode("utf-8"), md5.encode("utf-8"),
                       hashlib.sha1).digest()
        return base64.b64encode(raw).decode()

    def _auth(self) -> dict:
        ts = str(int(time.time()))
        return {"app_id": self.app_id, "ts": ts,
                "signa": self.signa(self.app_id, ts, self.api_secret)}

    # -- 音频准备：任意容器 -> 16k 单声道 16bit WAV ------------------------
    def _prep_audio(self, path: str) -> tuple:
        """返回 (wav路径, 时长秒, 要不要删的临时文件)。失败明确报错，不静默降级。"""
        if not os.path.exists(path):
            raise WxChannelError("asr_no_file", f"音频不存在: {path}")
        ext = os.path.splitext(path)[1].lower()
        if ext == ".wav":
            try:
                with wave.open(path, "rb") as w:
                    if (w.getframerate() == self.target_rate
                            and w.getnchannels() == 1
                            and w.getsampwidth() == 2):
                        return path, w.getnframes() / w.getframerate(), False
            except Exception:
                pass                      # 不是合法 WAV，往下走转码
        out = os.path.splitext(path)[0] + f"_xfyun{self.target_rate}.wav"
        if os.path.exists(out) and os.path.getsize(out) > 44:
            with wave.open(out, "rb") as w:
                return out, w.getnframes() / w.getframerate(), False
        if _pyav_available():
            self._to_wav_pyav(path, out)
        elif shutil.which("ffmpeg"):
            self._to_wav_ffmpeg(path, out)
        else:
            raise WxChannelError(
                "asr_no_decoder",
                f"需要把 {ext or '该格式'} 转成 {self.target_rate}Hz 单声道 WAV，"
                f"但既没有 PyAV(av) 也没有 ffmpeg。装 PyAV（pip install av）"
                f"或 ffmpeg，二者有其一即可")
        with wave.open(out, "rb") as w:
            return out, w.getnframes() / w.getframerate(), True

    def _to_wav_pyav(self, src: str, dst: str):
        """PyAV 解码+重采样。

        ⚠️ **`bytes(frame.planes[0])` 带 SIMD 对齐填充**，直接取会虚长约 30%
        （实测 3.0s -> 3.89s，白白多算 28012 字节）。必须按 `samples * 2` 截断，
        或用 `to_ndarray().tobytes()`。
        """
        try:
            import av
        except ImportError as exc:            # pragma: no cover
            raise WxChannelError("asr_no_decoder", f"PyAV 不可用: {exc}") from exc
        try:
            with av.open(src) as inp:
                if not inp.streams.audio:
                    raise WxChannelError("asr_no_audio", f"{src} 里没有音频轨")
                astream = inp.streams.audio[0]
                astream.thread_type = "AUTO"
                res = av.AudioResampler(format="s16", layout="mono",
                                        rate=self.target_rate)
                with wave.open(dst, "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(self.target_rate)

                    def dump(rf):
                        w.writeframes(bytes(rf.planes[0])[: rf.samples * 2])

                    for frame in inp.decode(astream):
                        for rf in res.resample(frame):
                            dump(rf)
                    for rf in res.resample(None):      # flush 缓冲，否则末尾丢一截
                        dump(rf)
        except WxChannelError:
            raise
        except Exception as exc:
            raise WxChannelError("asr_decode_failed",
                                 f"PyAV 解码失败: {type(exc).__name__}: {exc}") from exc

    def _to_wav_ffmpeg(self, src: str, dst: str):
        exe = shutil.which("ffmpeg")
        cmd = [exe, "-y", "-i", src, "-vn", "-ac", "1", "-ar",
               str(self.target_rate), "-c:a", "pcm_s16le", dst]
        p = subprocess.run(cmd, capture_output=True, timeout=900)
        if p.returncode != 0 or not os.path.exists(dst):
            raise WxChannelError("asr_ffmpeg_failed",
                                 f"ffmpeg 退出码 {p.returncode}: "
                                 f"{p.stderr[-200:].decode('utf-8', 'ignore')}")

    # -- checkpoint：同一份音频不重复烧额度 ---------------------------------
    def _ck_path(self) -> str:
        d = self.data_dir or _data_dir()
        return os.path.join(d, "channels_asr_tasks.json")

    def _ck_load(self) -> dict:
        p = self._ck_path()
        if not os.path.exists(p):
            return {}
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f) or {}
        except (OSError, ValueError):
            return {}

    def _ck_save(self, ck: dict):
        p = self._ck_path()
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                # default=str 是必须的：_now() 返回 datetime，直接 dump 会
                # TypeError 把整个转写结果崩掉（第一版就踩了，实测抓到）。
                # checkpoint 写盘失败不该让转写失败，但**写盘时崩溃**必须防住。
                json.dump(ck, f, ensure_ascii=False, indent=1, default=str)
        except (OSError, TypeError, ValueError):
            pass          # checkpoint 写不了不该让转写失败

    @staticmethod
    def _fingerprint(path: str) -> str:
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for blk in iter(lambda: f.read(1 << 20), b""):
                h.update(blk)
        return h.hexdigest()

    # -- 五个 HTTP 步骤 -----------------------------------------------------
    def _call(self, step: str, data: dict) -> dict:
        url = f"{self.base}/{step}"
        try:
            r = requests.post(url, data=data, timeout=120)
        except requests.RequestException as exc:
            raise WxChannelError("asr_network",
                                 f"讯飞 {step} 请求失败: {exc}") from exc
        if r.status_code != 200:
            raise WxChannelError("asr_http",
                                 f"讯飞 {step} HTTP {r.status_code}: {r.text[:160]}")
        try:
            j = r.json()
        except ValueError as exc:
            raise WxChannelError("asr_bad_json",
                                 f"讯飞 {step} 响应非 JSON: {r.text[:150]}") from exc
        if int(j.get("ok", -1)) != 0:
            code = int(j.get("err_no") or 0)
            raise WxChannelError(
                "asr_xfyun_err",
                f"讯飞 {step} 失败 err_no={code} "
                f"{self.ERRS.get(code, j.get('failed') or '未知错误')}")
        return j

    def _prepare(self, wav: str, size: int) -> str:
        d = self._auth()
        d.update({"file_len": str(size),
                  "file_name": os.path.basename(wav),
                  "slice_num": "1",           # 先按单片走；>10M 再自动分片
                  "has_participle": "false",
                  "max_alternatives": "0",
                  "language": "cn",
                  "has_smooth": "true",
                  "lfasr_type": "0",
                  "pd": self.pd,
                  "eng_vad_margin": "0"})
        if self.hot_words:
            d["hotWord"] = "|".join(self.hot_words)
        j = self._call("prepare", d)
        return str(j.get("data") or "")

    @staticmethod
    def _slice_ids(n: int):
        """官方示例的 slice_id 生成器：aaaaaaaaa / aaaaaaaaab / …"""
        cur = "aaaaaaaaa"
        for _ in range(n):
            yield cur
            chars = list(cur)
            j = len(chars) - 1
            while j >= 0:
                if chars[j] != "z":
                    chars[j] = chr(ord(chars[j]) + 1)
                    cur = "".join(chars)
                    break
                chars[j] = "a"
                j -= 1
            else:
                return

    def _upload(self, wav: str, size: int, task_id: str) -> None:
        n = max(1, (size + self.SLICE_BYTES - 1) // self.SLICE_BYTES)
        if n > 1:
            # 官方要求先按真实分片数 prepare；这里只在 >10M 时重做一次
            self._prepare_sliced(wav, size, n)
        ids = list(self._slice_ids(n))
        with open(wav, "rb") as f:
            for i, sid in enumerate(ids):
                auth = self._auth()
                auth.update({"task_id": task_id, "slice_id": sid})
                blob = f.read(self.SLICE_BYTES)
                try:
                    r = requests.post(f"{self.base}/upload", data=auth,
                                      files={"content": (f"part{i}", blob,
                                                         "audio/wav")},
                                      timeout=300)
                except requests.RequestException as exc:
                    raise WxChannelError("asr_network",
                                         f"分片 {i + 1}/{n} 上传失败: {exc}") from exc
                try:
                    j = r.json()
                except ValueError:
                    raise WxChannelError("asr_bad_json",
                                         f"分片 {i + 1} 响应非 JSON: {r.text[:150]}")
                if int(j.get("ok", -1)) != 0:
                    code = int(j.get("err_no") or 0)
                    raise WxChannelError(
                        "asr_xfyun_err",
                        f"分片 {i + 1}/{n} 上传失败 err_no={code} "
                        f"{self.ERRS.get(code, j.get('failed') or '')}")

    def _prepare_sliced(self, wav: str, size: int, n: int) -> None:
        d = self._auth()
        d.update({"file_len": str(size), "file_name": os.path.basename(wav),
                  "slice_num": str(n), "has_participle": "false",
                  "max_alternatives": "0", "language": "cn",
                  "has_smooth": "true", "lfasr_type": "0", "pd": self.pd,
                  "eng_vad_margin": "0"})
        if self.hot_words:
            d["hotWord"] = "|".join(self.hot_words)
        j = self._call("prepare", d)
        self.task_id = str(j.get("data") or "")

    def _merge(self, task_id: str) -> None:
        d = self._auth()
        d["task_id"] = task_id
        self._call("merge", d)

    def _progress(self, task_id: str) -> tuple:
        d = self._auth()
        d["task_id"] = task_id
        j = self._call("getProgress", d)
        raw = j.get("data") or "{}"
        try:
            inner = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            inner = {}
        return int(inner.get("status", -1)), str(inner.get("desc") or "")

    def _result(self, task_id: str) -> list:
        """返回 [{'bg':ms,'ed':ms,'text':str,'speaker':int}, ...]。

        ⚠️ `data` 是**被 JSON 字符串包了一层的数组** —— 必须 parse 两次，
        少一次就会当成 dict 处理、拿到空结果（HTTP 200 但没数据那一类）。
        """
        d = self._auth()
        d["task_id"] = task_id
        j = self._call("getResult", d)
        raw = j.get("data")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = []
        out = []
        for s in (raw or []):
            if not isinstance(s, dict):
                continue
            txt = _flat_text(s.get("onebest"))
            if not txt:
                continue
            out.append({"bg": normalize_ts(s.get("bg")), "ed": normalize_ts(s.get("ed")),
                        "text": txt, "speaker": s.get("speaker", 0)})
        return out

    # -- 对外：同步转写 -----------------------------------------------------
    def transcribe(self, path: str) -> str:
        if not self.available():
            raise WxChannelError("asr_not_configured",
                                 "讯飞未配置 app_id / api_secret")
        wav, dur, is_tmp = self._prep_audio(path)
        if dur <= 0.4:
            raise WxChannelError("asr_empty_audio",
                                 f"音频只有 {dur:.2f} 秒，几乎是静音，不值得提交")
        fp = self._fingerprint(wav)
        ck = self._ck_load()
        rec = ck.get(fp) or {}

        try:
            # 1) 之前已完成 -> 直接复用，不烧额度
            if rec.get("text"):
                return rec["text"]
            # 2) 之前提交过但没跑完 -> 接着轮询，别重新提交
            task_id = rec.get("task_id") or ""
            if task_id and not rec.get("failed"):
                try:
                    return self._await_result(task_id, ck, fp, dur, rec)
                except WxChannelError as exc:
                    if "err_no=26602" in str(exc) or "err_no=26604" in str(exc):
                        task_id = ""          # 任务已失效 -> 重新提交
                    else:
                        raise
            # 3) 新提交
            size = os.path.getsize(wav)
            task_id = self._prepare(wav, size) if size <= self.SLICE_BYTES \
                else (self._prepare_sliced(wav, size,
                                           max(1, (size + self.SLICE_BYTES - 1)
                                               // self.SLICE_BYTES)) or self.task_id)
            if not task_id:
                raise WxChannelError("asr_xfyun_err", "prepare 没返回 task_id")
            ck[fp] = {"task_id": task_id, "src": os.path.basename(path),
                      "duration": round(dur, 1), "ts": _now()}
            self._ck_save(ck)
            self._upload(wav, size, task_id)
            self._merge(task_id)
            return self._await_result(task_id, ck, fp, dur, rec)
        finally:
            if is_tmp:
                try:
                    os.remove(wav)
                except OSError:
                    pass

    def _await_result(self, task_id: str, ck: dict, fp: str, dur: float,
                      rec: dict) -> str:
        deadline = time.time() + self.max_wait
        status, desc = -1, ""
        # 官方建议长音频 10 分钟一轮；短音频（<10 分钟）返回时间 <3 分钟，
        # 所以前 3 分钟按 15 秒探，之后退到 60 秒，别把接口打超 20 次/秒限制。
        fast_until = time.time() + 180
        while time.time() < deadline:
            status, desc = self._progress(task_id)
            if status == self.STATUS_DONE:
                sents = self._result(task_id)
                if not sents:
                    raise WxChannelError("asr_empty", "转写完成但结果为空")
                text = self._join(sents)
                ck[fp] = {**(ck.get(fp) or {}), "task_id": task_id,
                          "text": text, "sentences": len(sents),
                          "duration": round(dur, 1), "done_ts": _now()}
                self._ck_save(ck)
                return text
            time.sleep(self.poll_seconds if time.time() < fast_until
                       else max(30, self.poll_seconds * 2))
        raise WxChannelError(
            "asr_timeout",
            f"等了 {self.max_wait // 60} 分钟还没转写完（最后状态 {status} "
            f"{self.STATUS_TEXT.get(status, desc)}）。task_id={task_id} 已落盘，"
            f"下次调用会接着等，不会重复提交")

    @staticmethod
    def _join(sents: list) -> str:
        """拼句子成一段文本。多说话人时分段标注（speaker > 0 才有意义）。"""
        lines, cur_sp, buf = [], None, []
        for s in sents:
            sp = s.get("speaker", 0)
            if sp != cur_sp and buf:
                lines.append(f"[说话人{cur_sp}] " + "".join(buf) if cur_sp
                             else "".join(buf))
                buf = []
            cur_sp = sp
            buf.append(s.get("text", ""))
        if buf:
            lines.append(f"[说话人{cur_sp}] " + "".join(buf) if cur_sp
                         else "".join(buf))
        return "\n".join(x for x in lines if x.strip()).strip()


def _pyav_available() -> bool:
    try:
        import av          # noqa: F401
        return True
    except Exception:
        return False


def get_asr(conf: dict) -> AsrProvider:
    """按配置选 ASR 实现。**三种形态，别只看 provider 名**：

    | provider | 端点 | 形态 |
    |---|---|---|
    | `xfyun` | `raasr.xfyun.cn/api/{prepare,upload,merge,getProgress,getResult}` | **异步任务制**，需 app_id + api_secret |
    | `auto` + `Qwen3-Omni-*` | `/v1/chat/completions`（音频当 content part）| 同步，13 tokens/秒 |
    | `auto` + 其它语音模型 | `/v1/audio/transcriptions`（multipart ≤1h/≤50MB）| 同步 |

    没配 key/app_id 一律退化成 `NullAsr`（L1 描述文案照常工作），并打印原因。
    """
    c = conf or {}
    if not c.get("enabled", False):
        return NullAsr()
    provider = (c.get("provider") or "auto").lower()
    key = c.get("api_key", "")
    model = c.get("model", "") or QwenOmniAsr.DEFAULT_MODEL
    base = c.get("base_url", "https://api.siliconflow.cn/v1")
    timeout = int(c.get("timeout", 300))

    # 1) 讯飞：自带免费体验包、中文最准、支持金融垂域
    if provider in ("xfyun", "xfyun_lfasr", "lfasr"):
        app_id = c.get("app_id") or ""
        secret = (os.environ.get("XFYUN_API_SECRET", "")
                  or c.get("api_secret", ""))
        if app_id and secret:
            return XfyunLfasrAsr(
                app_id=app_id, api_secret=secret,
                base_url=c.get("xfyun_base_url", ""),
                pd=c.get("pd", "finance"),
                hot_words=c.get("hot_words"),
                poll_seconds=int(c.get("poll_seconds", 15)),
                max_wait=int(c.get("max_wait", 900)),
                data_dir=c.get("data_dir", ""),
                target_rate=int(c.get("target_rate", 16000)))
        print("[channels] 讯飞 ASR 缺 app_id/api_secret"
              "（支持 XFYUN_API_SECRET 环境变量），退化为仅描述文案", flush=True)
        return NullAsr()

    # 2) Qwen3-Omni：多模态对话模型，走 chat 端点
    if provider in ("auto", "qwen_omni", "qwen") and model in QwenOmniAsr.CHAT_MODELS:
        if key:
            return QwenOmniAsr(api_key=key, model=model, base_url=base,
                               timeout=timeout,
                               chunk_seconds=int(c.get("chunk_seconds", 300)))
        print("[channels] ASR 缺 api_key，退化为仅描述文案", flush=True)
        return NullAsr()

    # 3) SiliconFlow 转写端点
    if provider in ("auto", "siliconflow", "transcription"):
        if model and model not in SiliconFlowAsr.KNOWN_MODELS:
            print(f"[channels] 模型 {model!r} 不在转写白名单里，"
                  f"仍尝试请求（服务端会返回 503/404，便于发现改名）", flush=True)
        if key:
            return SiliconFlowAsr(api_key=key, model=model, base_url=base,
                                  timeout=timeout)
        print("[channels] ASR 缺 api_key，退化为仅描述文案", flush=True)
        return NullAsr()

    print(f"[channels] 未知 ASR provider={provider!r}，退化为仅描述文案", flush=True)
    return NullAsr()


# ---------------- 订阅 / 状态 / 文本 ----------------

SUBS_FILE = "channels_subs.json"
STATE_FILE = "channels_state.json"


def load_subs(data_dir: str = "") -> list:
    """订阅列表：[{"username": "...", "name": "...", "enabled": true, "codes": ["600xxx"]}]"""
    path = os.path.join(data_dir or _data_dir(), SUBS_FILE)
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            d = d.get("subscriptions") or []
        return [s for s in d if isinstance(s, dict) and s.get("username")]
    except Exception as exc:
        print(f"[channels] 订阅文件读取失败: {exc}", flush=True)
        return []


def save_subs(subs: list, data_dir: str = "") -> str:
    base = data_dir or _data_dir()
    os.makedirs(base, exist_ok=True)
    path = os.path.join(base, SUBS_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"updated_at": _now().isoformat(timespec="seconds"),
                   "subscriptions": subs}, f, ensure_ascii=False, indent=2)
    return path


def load_state(data_dir: str = "") -> dict:
    """{username: {"seen": [object_id...], "last_poll": iso}}"""
    path = os.path.join(data_dir or _data_dir(), STATE_FILE)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_state(state: dict, data_dir: str = "") -> str:
    base = data_dir or _data_dir()
    os.makedirs(base, exist_ok=True)
    path = os.path.join(base, STATE_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    return path


def _first(d: dict, *keys, default=""):
    """按候选键名顺序取第一个非空值 —— 上游字段名不稳定（wx_channel 各版本改过）。"""
    if not isinstance(d, dict):
        return default
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return default


def _flat_text(v) -> str:
    """把 shortTitle/description 这类**类型不稳**的值摊平成纯字符串。

    实测 `objectDesc.shortTitle` 是 `[{'shortTitle': '...'}]`（**list[dict]**，
    2026-10-06 v5.7.10），直接 `str()` 会得到 Python repr
    `[{'shortTitle': '老腾讯赚钱新腾讯烧钱'}]` 并原样显示给用户。
    """
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, dict):
        for k in ("shortTitle", "description", "title", "text", "content", "desc"):
            r = _flat_text(v.get(k))
            if r:
                return r
        # 已知键都没中 → 按插入顺序往里递归一层（兜住
        # `[{"a": [{"shortTitle": "..."}]}]` 这种多包一层的形态）
        for x in v.values():
            r = _flat_text(x)
            if r:
                return r
        return ""
    if isinstance(v, (list, tuple)):
        for x in v:
            r = _flat_text(x)
            if r:
                return r
        return ""
    return ""


def _desc_text(obj: dict) -> tuple:
    """正文/标题从 `objectDesc` 里取 —— 它是**对象不是字符串**。

    真实结构（2026-10-06 实测）：
      objectDesc = {"description": "...正文...", "shortTitle": [{...}], "media": [...]}
    原实现只在顶层找 `desc`/`description`，而正文在 `objectDesc.description`
    下面一层 → 取不到，标题和描述双双为空（条目会退化成「视频 xxxxxxxx」）。
    """
    od = obj.get("objectDesc")
    if isinstance(od, str):
        return od.strip(), ""
    if not isinstance(od, dict):
        return "", ""
    desc = _flat_text(od.get("description"))
    title = _flat_text(od.get("shortTitle"))
    return desc, title


def extract_text(profile: dict) -> tuple:
    """从 feed/profile 里抽 (标题, 完整描述, 发布时间, 视频url)。

    返回的 `desc` 是**正文级文本**（视频号常有长描述，比标题有用得多）。
    """
    od_desc, od_title = _desc_text(profile) if isinstance(profile, dict) else ("", "")
    # 标题优先用规范化后的 shortTitle —— 别直接 _first(...,"shortTitle")，
    # 那拿到的是 list[dict]，str() 出来是 repr（见 _flat_text 注释）
    title = od_title or str(_first(profile, "title", default="")).strip() \
        or od_desc.split("\n")[0][:80]
    desc = str(_first(profile, "desc", "description", default="") or od_desc).strip()
    # `createtime` 是**小写 t**（实测），别只写 createTime —— dict.get 区分大小写
    ts = _first(profile, "create_time", "createtime", "createTime", "publish_time",
                "time", default=0)
    url = str(_first(profile, "share_url", "shareUrl", "url", "link",
                     default="")).strip()
    return title, desc, ts, url


def normalize_ts(ts) -> str:
    """视频号的时间戳单位实测混乱（秒/毫秒都有）→ 统一成 ISO 字符串。

    判据：> 1e11 视为毫秒。写错会让 publish_time 落到 1970/55000 年，
    而 `sa_news.publish_time` 只是 TEXT/TIMESTAMPTZ，不报错。
    """
    try:
        n = int(float(ts))
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    if n > 1e11:
        n //= 1000
    try:
        return datetime.fromtimestamp(n).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return ""


# ---------------- 主流程 ----------------

def poll_once(client: WxChannelClient, subs: list, asr: AsrProvider | None = None,
              state: dict | None = None, data_dir: str = "",
              with_comments: bool = True, comment_pages: int = 1,
              max_new_per_sub: int = 10, downloader=None) -> dict:
    """轮询一轮所有订阅，返回本轮新增条目。

    `downloader` 是可选的 `callable(object_id) -> 本地 mp4 路径`，
    用于 ASR 前置下载；不给就只出 L1（描述文案）。

    **seen 只在条目成功构造后更新**，且 `failures` 单独回报 ——
    崩在中间不会把视频标记成已读。
    """
    asr = asr or NullAsr()
    state = state if state is not None else load_state(data_dir)
    out = {"ts": _now().isoformat(timespec="seconds"), "items": [],
           "errors": [], "failures": [], "polled": 0}
    for sub in subs:
        if not sub.get("enabled", True):
            continue
        user = str(sub.get("username") or "").strip()
        if not user:
            continue
        out["polled"] += 1
        st = state.setdefault(user, {"seen": [], "last_poll": None})
        seen = set(st.get("seen") or [])
        try:
            page = client.contact_feed_list(user)
        except WxChannelError as exc:
            out["errors"].append(f"{user}: 列表失败 {exc}")
            continue
        items = page.get("items") or []
        if not items:
            out["errors"].append(f"{user}: 列表为空（若接口 code=0 仍空，"
                                 f"通常是 wx_channel 没抓到该博主的流量）")
            continue
        new_cnt = 0
        for it in items:
            oid = str(_first(it, "object_id", "objectId", "id", "object_id_str",
                              default="")).strip()
            if not oid:
                out["failures"].append(f"{user}: 条目缺 object_id，跳过")
                continue
            if oid in seen:
                continue
            if new_cnt >= max_new_per_sub:
                out["errors"].append(f"{user}: 超过单轮上限 {max_new_per_sub}，"
                                     f"剩余留到下轮")
                break
            title, desc, ts, url = extract_text(it)
            # 真实字段是 objectNonceId（2026-10-06 实测），原候选名取不到
            nonce_id = str(_first(it, "nonce_id", "nonceId", "objectNonceId",
                                  default="")).strip()
            try:
                prof = client.feed_profile(oid, nonce_id) if nonce_id \
                    else client.feed_profile(oid)
            except WxChannelError as exc:
                out["failures"].append(f"{oid}: 详情失败 {exc}")
                continue
            if prof:
                p_title, p_desc, p_ts, p_url = extract_text(prof)
                title = p_title or title
                desc = p_desc or desc
                ts = ts or p_ts
                url = url or p_url
                nonce_id = nonce_id or str(
                    _first(prof, "nonce_id", "nonceId", "objectNonceId",
                           default="")).strip()

            rec = {
                "object_id": oid,
                "nonce_id": nonce_id,
                "username": user,
                "author": sub.get("name") or user,
                "codes": sub.get("codes") or [],
                "title": (title or desc)[:200] or f"视频 {oid[:8]}",
                "desc": desc,
                "publish_time": normalize_ts(ts),
                "url": url or f"wxchannels://{oid}",
                "source": SOURCE,
                "media": str(_first(prof, "media", "media_type", default="video")),
                "stats": {
                    "like": _first(prof, "like_count", "likeCount", "like", default=0),
                    "comment": _first(prof, "comment_count", "commentCount", default=0),
                    "fav": _first(prof, "fav_count", "favCount", default=0),
                },
                "asr_text": "",
                "asr_error": "",
            }
            # L2：下载 + 转写。失败不丢条目，只标错（保留 L1）
            if asr.available() and downloader is not None:
                try:
                    path = downloader(oid)
                    if path and os.path.exists(path):
                        rec["asr_text"] = asr.transcribe(path)
                    else:
                        rec["asr_error"] = "下载未产出文件"
                except WxChannelError as exc:
                    rec["asr_error"] = str(exc)
                except Exception as exc:      # 下载器可能抛任意异常
                    rec["asr_error"] = f"{type(exc).__name__}: {exc}"

            if with_comments:
                cs, cmeta = _fetch_comments(client, oid, comment_pages,
                                            nonce_id=nonce_id)
                rec["comments"] = cs
                rec["comment_meta"] = cmeta
                if not cmeta.get("ready"):
                    # 明确区分「客户端没就绪」和「真的 0 条评论」
                    rec["comment_error"] = (cmeta.get("error")
                                            or "评论客户端未就绪（微信需停在该视频页面）")

            rec["full_text"] = "\n".join(
                [rec["desc"], rec["asr_text"]]).strip()
            out["items"].append(rec)
            seen.add(oid)
            new_cnt += 1
        st["seen"] = sorted(seen)[-500:]     # 只留最近 500 条，避免状态无限膨胀
        st["last_poll"] = out["ts"]
    return out


def _fetch_comments(client: WxChannelClient, object_id: str, pages: int,
                    nonce_id: str = "", max_replies: int = 3) -> tuple:
    """评论列表（翻页 + 二级回复）。返回 (comments, meta)。

    ⚠️ **拿不到 ≠ 没有评论**：评论是走页面 DOM/Store 采集的
    （链路：HTTP API → WebSocket Hub → 注入脚本 → 页面内 `finderGetCommentList`），
    **微信页面必须停在那个视频上**才会返回数据。所以 `meta["ready"]` 为 False 时，
    调用方要报「客户端未就绪」而不是「该视频 0 条评论」——
    这两者混在一起会让监控看起来在工作、其实什么都没抓到。
    """
    out = []
    marker = ""
    total = 0
    ready = False
    err = ""
    for _ in range(max(1, pages)):
        try:
            page = client.comment_list(object_id, nonce_id=nonce_id,
                                       next_marker=marker)
        except WxChannelError as exc:
            err = str(exc)
            break
        items = page.get("items") or []
        if items:
            ready = True
        total = page.get("total") or total
        for c in items:
            replies = []
            for r2 in (c.get("levelTwoComment") or [])[:max_replies]:
                replies.append({
                    "nickname": str(_first(r2, "nickname", default="")),
                    "content": str(_first(r2, "content", default="")),
                    "time": normalize_ts(_first(r2, "createtime", default=0)),
                    "like": _first(r2, "likeCount", "like_count", default=0),
                })
            out.append({
                "comment_id": str(_first(c, "commentId", "comment_id", default="")),
                "nickname": str(_first(c, "nickname", "nick_name", default="")),
                "content": str(_first(c, "content", default="")),
                # 时间字段是 createtime（秒级）——之前按 create_time 取，永远拿不到
                "time": normalize_ts(_first(c, "createtime", "create_time",
                                            "createTime", default=0)),
                "like": _first(c, "likeCount", "like_count", default=0),
                "reply_count": _first(c, "expandCommentCount",
                                      "expand_comment_count", default=0),
                "reply_to_id": str(_first(c, "replyCommentId", default="")),
                "replies": replies,
            })
        marker = page.get("next_marker") or ""
        if not marker or not items:
            break
    return out, {"ready": ready, "total": total, "error": err,
                 "collected": len(out)}


# ---------------- 落盘 / 入库 ----------------

def append_items(result: dict, data_dir: str = "") -> str:
    """把本轮条目追加到 `data/channels_items_<日期>.jsonl`。"""
    try:
        base = data_dir or _data_dir()
        os.makedirs(base, exist_ok=True)
        path = os.path.join(base, f"channels_items_{_now():%Y%m%d}.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            for it in result.get("items") or []:
                f.write(json.dumps(it, ensure_ascii=False, default=str) + "\n")
        return path
    except Exception as exc:
        print(f"[channels] 条目落盘失败: {exc}", flush=True)
        return ""


def sentiment_of(text: str, codes: list | None = None) -> str:
    """情绪判定：**复用** `news_fetcher.classify_sentiment`，不自己写一套词表。"""
    try:
        from news_fetcher import classify_sentiment
    except Exception as exc:
        print(f"[channels] 复用 classify_sentiment 失败，跳过情绪: {exc}", flush=True)
        return ""
    codes = codes or []
    try:
        # 与 news_fetcher 落库前的口径一致：按股票名取该股的正/负词
        import news_fetcher as nf
        senti_words = getattr(nf, "load_sentiment_words", lambda: {})(  # noqa: E501
        ) if hasattr(nf, "load_sentiment_words") else {}
        sw = senti_words.get(codes[0]) if codes else None
        if isinstance(sw, dict):
            return classify_sentiment(text, sw.get("pos"), sw.get("neg"))
        return classify_sentiment(text, None, None)
    except Exception as exc:
        print(f"[channels] 情绪判定异常: {exc}", flush=True)
        return ""


def save_to_sa_news(conn, items: list, with_relations: bool = True,
                    commit: bool = True) -> int:
    """写现有内容监控表。**不新建表**（AGENTS.md：改 schema 超出「修 bug」范围）。

    - `sa_news`：url 唯一 → 天然去重；`code=''` 表示非股票源，
      真正的关联股票放 `sa_news_related`（多对多）
    - 完整正文（描述 + ASR）不塞进 sa_news（title 语义是标题），
      落在 `data/channels_items_*.jsonl`，用 url 关联

    ⚠️ `commit=False` 时**不提交**，事务由调用方控制。
    这一点是踩过坑才加的（2026-10-06）：原本函数内部无条件 `commit()`，
    于是端到端测试里那句 `conn.rollback()` 变成空操作，**3 行测试数据直接
    落进了生产 `sa_news`**。凡是「函数自己 commit」的实现，都没法被调用方
    放进事务里回滚 —— 需要可测试/可原子组合的写入就别自己 commit。
    """
    inserted = 0
    with conn.cursor() as cur:
        for it in items:
            senti = sentiment_of(it.get("full_text") or it.get("title", ""),
                                 it.get("codes"))
            cur.execute(
                "INSERT INTO sa_news (code, title, url, source, media, "
                "publish_time, sentiment) VALUES (%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (url) DO NOTHING",
                ("", (it.get("title") or "")[:500], it["url"], SOURCE,
                 it.get("media", "video"), it.get("publish_time") or None, senti))
            inserted += cur.rowcount
            if with_relations:
                for c in (it.get("codes") or [])[:20]:
                    cur.execute(
                        "INSERT INTO sa_news_related (url, code) VALUES (%s,%s) "
                        "ON CONFLICT (url, code) DO NOTHING", (it["url"], c))
    if commit:
        conn.commit()
    return inserted


# ---------------- 渲染 ----------------

def render(result: dict, top_comments: int = 3) -> str:
    items = result.get("items") or []
    L = [f"【视频号内容监控】（{len(items)} 条新内容，"
         f"{result.get('ts','')[:16]}）"]
    if not items:
        L.append("本轮无新增（若长期为空，先确认 wx_channel 是否已抓到该博主流量）")
    for it in items[:10]:
        L.append("")
        L.append(f"▶ {it.get('author','')}  {it.get('title','')[:60]}")
        L.append(f"   发布 {it.get('publish_time') or '未知'}  "
                 f"赞 {_num(it.get('stats',{}).get('like'))} "
                 f"评 {_num(it.get('stats',{}).get('comment'))}")
        if it.get("asr_text"):
            L.append(f"   转写: {it['asr_text'][:180]}")
        elif it.get("asr_error"):
            L.append(f"   ⚠ 转写失败: {it['asr_error'][:100]}")
        if it.get("desc"):
            L.append(f"   描述: {it['desc'][:180]}")
        cs = it.get("comments") or []
        if cs:
            L.append(f"   评论 {len(cs)} 条，最新{top_comments}条:")
            for c in cs[-top_comments:]:
                who = c.get("nickname") or "(匿名)"
                L.append(f"     · {who}: {(c.get('content') or '')[:80]}")
    for e in (result.get("errors") or [])[:5]:
        L.append(f"\n⚠ {e}")
    for f in (result.get("failures") or [])[:5]:
        L.append(f"⚠ 失败: {f}")
    return "\n".join(L)


def _num(v):
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return str(v) if v not in (None, "") else "0"


def check_once(api_base: str = DEFAULT_API, asr_conf: dict | None = None,
               subs: list | None = None, data_dir: str = "",
               downloader=None, with_comments: bool = True) -> dict:
    """一次完整采集：读订阅 → 轮询 → 落盘。**不写库**（写库由调用方决定）。"""
    client = WxChannelClient(base_url=api_base,
                             token=(asr_conf or {}).get("api_token", ""))
    subs = subs if subs is not None else load_subs(data_dir)
    asr = get_asr(asr_conf or {})
    state = load_state(data_dir)
    result = poll_once(client, subs, asr=asr, state=state, data_dir=data_dir,
                       with_comments=with_comments, downloader=downloader)
    if result.get("items"):
        append_items(result, data_dir)
        save_state(state, data_dir)      # 只有真产出才更新状态
    return result


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=DEFAULT_API)
    ap.add_argument("--no-comments", action="store_true")
    ap.add_argument("--render-only", action="store_true")
    a = ap.parse_args()
    try:
        r = check_once(api_base=a.api, with_comments=not a.no_comments)
        print(render(r))
        print(f"\n轮询 {r.get('polled')} 个博主，新条目 {len(r.get('items') or [])}，"
              f"错误 {len(r.get('errors') or [])}，失败 {len(r.get('failures') or [])}")
    except WxChannelError as exc:
        print(f"接口不可用: {exc}")
        print("提示：wx_channel 必须跑在装了 PC 微信的机器上（见模块 docstring）")
