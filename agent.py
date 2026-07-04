from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MODE_LABELS = {
    "short_drama": "短剧 Agent",
    "smart_video": "智能成片",
    "digital_human": "数字人口播",
    "visual_design": "视觉设计",
    "viral_remix": "灵感重构",
}

ALLOWED_ASPECT_RATIOS = {"9:16", "16:9", "1:1", "4:3"}
ALLOWED_DURATIONS = {15, 30, 60, 90, 180}
MAX_IDEA_LENGTH = 8_000
MAX_IMAGE_BYTES = 30 * 1024 * 1024
MAX_VIDEO_REFERENCE_IMAGES = 9
EDITABLE_STAGE_NAMES = {
    "overview",
    "script",
    "characters",
    "storyboard",
    "video",
    "publish",
}
ACTIVE_VIDEO_PRODUCTION_STATUSES = {"queued", "running"}
VIDEO_DELIVERY_FIELDS = {
    "videoUrl",
    "lastFrameUrl",
    "localVideoUrl",
    "localLastFrameUrl",
}


class AgentError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(text: str) -> str:
    value = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", text, flags=re.UNICODE)
    value = re.sub(r"-+", "-", value).strip("-")
    return value[:40] or "creative-project"


def clean_json_text(value: str) -> str:
    value = value.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\s*```$", "", value)
    start = value.find("{")
    end = value.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise AgentError("模型没有返回有效 JSON。")
    return value[start : end + 1]


def public_video_production_view(production: dict[str, Any]) -> dict[str, Any]:
    """Hide playable media until the complete production run reaches a terminal state."""
    result = copy.deepcopy(production)
    if result.get("status") not in ACTIVE_VIDEO_PRODUCTION_STATUSES:
        return result
    result["finalVideoUrl"] = ""
    result["manifestUrl"] = ""
    single_retry_job_id = (
        str(result.get("retryJobId") or "")
        if result.get("retryScope") == "single"
        else ""
    )
    for job in result.get("jobs", []):
        if not isinstance(job, dict):
            continue
        if (
            single_retry_job_id
            and str(job.get("id") or "") != single_retry_job_id
        ):
            continue
        for field in VIDEO_DELIVERY_FIELDS:
            job[field] = ""
    return result


def public_project_view(project: dict[str, Any]) -> dict[str, Any]:
    """Return the browser-facing project without leaking in-progress video URLs."""
    result = copy.deepcopy(project)
    production = result.get("videoProduction")
    if isinstance(production, dict):
        result["videoProduction"] = public_video_production_view(production)
    return result


@dataclass
class ModelSettings:
    base_url: str
    api_key: str
    model: str
    image_model: str
    timeout: int
    image_base_url: str = ""
    image_api_key: str = ""
    image_edit_base_url: str = ""
    image_edit_api_key: str = ""
    image_edit_model: str = ""
    vision_base_url: str = ""
    vision_api_key: str = ""
    vision_model: str = ""

    @classmethod
    def from_environment(cls) -> "ModelSettings":
        return cls(
            base_url=os.getenv("JINGZHOU_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            api_key=os.getenv("JINGZHOU_API_KEY", "").strip(),
            model=os.getenv("JINGZHOU_MODEL", "gpt-4.1-mini").strip(),
            image_model=(
                os.getenv("IMAGE_MODEL", "").strip()
                or os.getenv("JINGZHOU_IMAGE_MODEL", "").strip()
            ),
            timeout=int(os.getenv("JINGZHOU_TIMEOUT", "120")),
            image_base_url=os.getenv("IMAGE_API_BASE_URL", "").strip(),
            image_api_key=os.getenv("IMAGE_API_KEY", "").strip(),
            image_edit_base_url=os.getenv(
                "IMAGE_EDIT_API_BASE_URL",
                "",
            ).strip(),
            image_edit_api_key=os.getenv("IMAGE_EDIT_API_KEY", "").strip(),
            image_edit_model=(
                os.getenv("IMAGE_EDIT_MODEL", "").strip()
                or os.getenv("IMAGE_MODEL", "").strip()
                or os.getenv("JINGZHOU_IMAGE_MODEL", "").strip()
            ),
            vision_base_url=os.getenv(
                "VISION_API_BASE_URL",
                "",
            ).strip().rstrip("/"),
            vision_api_key=os.getenv("VISION_API_KEY", "").strip(),
            vision_model=os.getenv("VISION_MODEL", "").strip(),
        )

    @property
    def text_enabled(self) -> bool:
        return bool(self.api_key and self.model)

    @property
    def image_enabled(self) -> bool:
        return bool(
            (self.image_api_key or self.api_key)
            and self.image_model
            and (self.image_base_url or self.base_url)
        )

    @property
    def resolved_image_url(self) -> str:
        return (
            self.image_base_url
            or f"{self.base_url.rstrip('/')}/images/generations"
        )

    @property
    def resolved_image_api_key(self) -> str:
        return self.image_api_key or self.api_key

    @property
    def image_edit_enabled(self) -> bool:
        return bool(
            self.image_edit_base_url
            and self.image_edit_model
            and (
                self.image_edit_api_key
                or self.image_api_key
                or self.api_key
            )
        )

    @property
    def resolved_image_edit_api_key(self) -> str:
        return (
            self.image_edit_api_key
            or self.image_api_key
            or self.api_key
        )

    @property
    def vision_enabled(self) -> bool:
        return bool(
            self.vision_base_url
            and self.vision_model
            and (
                self.vision_api_key
                or self.image_api_key
                or self.api_key
            )
        )

    @property
    def resolved_vision_api_key(self) -> str:
        return (
            self.vision_api_key
            or self.image_api_key
            or self.api_key
        )

    @property
    def resolved_vision_url(self) -> str:
        return f"{self.vision_base_url}"


class OpenAICompatibleProvider:
    def __init__(self, settings: ModelSettings):
        self.settings = settings

    @staticmethod
    def _log_api_result(
        label: str,
        url: str,
        status: int | str,
        detail: str = "",
    ) -> None:
        timestamp = datetime.now().astimezone().strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        suffix = f" {detail[:500]}" if detail else ""
        print(
            f"[{label}] {timestamp} POST {url} -> HTTP {status}{suffix}",
            flush=True,
        )

    def _request(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        exact_url: str = "",
        api_key: str = "",
    ) -> dict[str, Any]:
        url = exact_url or f"{self.settings.base_url}{path}"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key or self.settings.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "Jingzhou-Agent/0.1",
            },
            method="POST",
        )
        label = "图片 API" if "/images" in url else "模型 API"
        try:
            with urllib.request.urlopen(request, timeout=self.settings.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
                self._log_api_result(
                    label,
                    url,
                    getattr(response, "status", 200),
                )
                return result
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            self._log_api_result(label, url, exc.code, body)
            raise AgentError(f"模型服务返回 HTTP {exc.code}: {body[:500]}") from exc
        except urllib.error.URLError as exc:
            self._log_api_result(label, url, "NETWORK_ERROR", str(exc.reason))
            raise AgentError(f"无法连接模型服务：{exc.reason}") from exc
        except TimeoutError as exc:
            self._log_api_result(label, url, "TIMEOUT")
            raise AgentError("模型服务响应超时。") from exc

    @staticmethod
    def _multipart(
        fields: dict[str, str],
        files: list[tuple[str, str, str, bytes]],
    ) -> tuple[bytes, str]:
        boundary = f"----JingzhouImage{uuid.uuid4().hex}"
        chunks: list[bytes] = []
        for name, value in fields.items():
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode("ascii"),
                    (
                        f'Content-Disposition: form-data; name="{name}"'
                        "\r\n\r\n"
                    ).encode("ascii"),
                    value.encode("utf-8"),
                    b"\r\n",
                ]
            )
        for name, filename, mime_type, content in files:
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode("ascii"),
                    (
                        f'Content-Disposition: form-data; name="{name}"; '
                        f'filename="{filename}"\r\n'
                    ).encode("ascii"),
                    f"Content-Type: {mime_type}\r\n\r\n".encode("ascii"),
                    content,
                    b"\r\n",
                ]
            )
        chunks.append(f"--{boundary}--\r\n".encode("ascii"))
        return b"".join(chunks), f"multipart/form-data; boundary={boundary}"

    def _request_multipart(
        self,
        url: str,
        fields: dict[str, str],
        files: list[tuple[str, str, str, bytes]],
        *,
        api_key: str,
    ) -> dict[str, Any]:
        body, content_type = self._multipart(fields, files)
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": content_type,
                "User-Agent": "Jingzhou-Agent/0.3",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.settings.timeout,
            ) as response:
                result = json.loads(response.read().decode("utf-8"))
                self._log_api_result(
                    "图片编辑 API",
                    url,
                    getattr(response, "status", 200),
                )
                return result
        except urllib.error.HTTPError as exc:
            response_body = exc.read().decode("utf-8", errors="replace")
            self._log_api_result(
                "图片编辑 API",
                url,
                exc.code,
                response_body,
            )
            raise AgentError(
                f"图片编辑 API 返回 HTTP {exc.code}: {response_body[:500]}"
            ) from exc
        except urllib.error.URLError as exc:
            self._log_api_result(
                "图片编辑 API",
                url,
                "NETWORK_ERROR",
                str(exc.reason),
            )
            raise AgentError(f"无法连接图片编辑 API：{exc.reason}") from exc
        except TimeoutError as exc:
            self._log_api_result("图片编辑 API", url, "TIMEOUT")
            raise AgentError("图片编辑 API 响应超时。") from exc

    def generate_project(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        if not self.settings.text_enabled:
            raise AgentError("尚未配置文本模型。")
        response = self._request(
            "/chat/completions",
            {
                "model": self.settings.model,
                "temperature": 0.7,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            },
        )
        try:
            content = response["choices"][0]["message"]["content"]
            return json.loads(clean_json_text(content))
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise AgentError("无法解析模型返回的创作方案。") from exc

    def generate_video_prompt(
        self,
        project_context: dict[str, Any],
    ) -> str:
        if not self.settings.text_enabled:
            raise AgentError("尚未配置文本模型。")
        system_prompt = """你是影视视频生成提示词导演。请把项目上下文、当前分镜草稿、出镜角色设定、场景设定，以及每张参考图片对应的原始文本提示词，整合成一段可直接提交给视频生成模型的完整中文提示词。
要求：
1. 明确主体身份、外观、服装、动作、表情、场景空间、光线、天气、机位、景别、镜头运动、时间变化、台词/声音和结尾状态。
2. 每张参考图必须按其 ownerType、ownerName 和 usage 对应到正确角色、场景、首帧或尾帧，禁止混淆多名角色。
3. 结合前后分镜上下文，写明本镜头起始状态、动作过程与结束状态，保持故事连续。
4. 不虚构参考图中没有提供的关键身份特征；避免字幕、Logo、水印、乱码、变脸、换装、肢体畸变和主体漂移。
5. 主动清除版权风险：不得出现具体影视/动漫/游戏作品名、受保护角色名、品牌角色、Logo、可识别作品镜头复刻或“某位在世艺术家风格”。如上下文含此类指向，改写为不保留专有识别特征的原创人物、原创场景和通用视听语言。
6. 只输出 JSON：{"prompt":"完整视频提示词"}，不要 Markdown 或解释。"""
        response = self._request(
            "/chat/completions",
            {
                "model": self.settings.model,
                "temperature": 0.35,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": json.dumps(
                            project_context,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                ],
            },
        )
        result = self._structured_content(response, "完整视频提示词")
        prompt = str(result.get("prompt") or "").strip()
        if not prompt:
            raise AgentError("文本模型没有返回完整视频提示词。")
        return prompt[:8_000]

    @staticmethod
    def _structured_content(response: dict[str, Any], label: str) -> dict[str, Any]:
        try:
            content = response["choices"][0]["message"]["content"]
            return json.loads(clean_json_text(content))
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise AgentError(f"无法解析模型返回的{label}。") from exc

    def edit_project(
        self,
        project_context: dict[str, Any],
        history: list[dict[str, str]],
        instruction: str,
    ) -> dict[str, Any]:
        if not self.settings.text_enabled:
            raise AgentError("尚未配置文本模型。")
        system_prompt = """你是影视项目编辑助手。你会收到当前项目的完整文本上下文。根据用户要求修改项目，但只通过结构化操作表达变更。
可用 action：
1. update_script：fields 可包含 logline、synopsis、narration、beats。
2. add_character：character 包含 name、role、visualIdentity、personality、voice、imagePrompt。
3. update_character：提供 id，fields 只包含需要修改的角色字段。
4. delete_character：提供 id。
5. add_scene：scene 包含 name、imagePrompt。
6. update_scene：提供 id，fields 只包含 name、imagePrompt。
7. delete_scene：提供 id；仍被分镜使用时，应同时给出对应 update_shot 操作。
8. add_shot：shot 包含 scene、duration、action、camera、visualPrompt、videoPrompt、dialogue、audio、continuity、characterIds；可选 afterShotId。
9. update_shot：提供 id，fields 只包含需要修改的分镜字段。
10. delete_shot：提供 id。
characterIds 对已有角色填写角色 ID；同一批操作中新增加的角色也可填写角色名称。
修改分镜时必须结合完整剧本、所有角色、所有场景及相邻分镜，保持人物身份、场景空间、时间线、动作和台词上下文连续。
reply 必须先概述建议修改的位置、修改方向及理由，明确这是等待用户确认的建议，
不能声称已经完成修改。
如果用户只是询问，不需要修改，则 operations 为空。不得编造现有角色、场景或分镜 ID。
只输出 JSON：{"reply":"给用户的简体中文回复","operations":[]}。"""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    "当前项目：\n"
                    + json.dumps(
                        project_context,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                ),
            },
        ]
        messages.extend(history[-10:])
        messages.append({"role": "user", "content": instruction})
        response = self._request(
            "/chat/completions",
            {
                "model": self.settings.model,
                "temperature": 0.3,
                "messages": messages,
            },
        )
        return self._structured_content(response, "项目修改方案")

    def describe_character(
        self,
        image_data_uri: str,
        character_name: str,
    ) -> dict[str, Any]:
        if not self.settings.vision_enabled:
            raise AgentError(
                "尚未配置视觉理解模型，请设置 VISION_API_BASE_URL、"
                "VISION_API_KEY 和 VISION_MODEL。"
            )
        host = (
            urllib.parse.urlparse(self.settings.vision_base_url)
            .hostname
            or ""
        ).lower()
        if host == "api.deepseek.com":
            raise AgentError(
                "DeepSeek Chat Completions 当前只接受文本，不能用作角色图片"
                "识别模型。请将 VISION_API_BASE_URL 和 VISION_MODEL 配置为"
                "支持 image_url 图片输入的多模态模型。"
            )
        try:
            response = self._request(
                "",
                {
                    "model": self.settings.vision_model,
                    "temperature": 0.2,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "你是影视角色设计师。分析参考图片中可见的角色，"
                                "生成稳定、可复用且不推断敏感属性的角色设定。"
                                "只输出 JSON。"
                            ),
                        },
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        f"角色名称：{character_name or '未命名角色'}。"
                                        "请返回 visualIdentity、personality、role、voice、"
                                        "imagePrompt 五个简体中文字段。visualIdentity 只描述"
                                        "可见外观、服装、发型、配饰和体态；性格与声音使用"
                                        "适合影视创作的建议性设定。"
                                    ),
                                },
                                {
                                    "type": "image_url",
                                    "image_url": {"url": image_data_uri},
                                },
                            ],
                        },
                    ],
                },
                exact_url=self.settings.resolved_vision_url,
                api_key=self.settings.resolved_vision_api_key,
            )
        except AgentError as exc:
            detail = str(exc)
            if (
                "unknown variant `image_url`" in detail
                or "expected `text`" in detail
                or "does not support image" in detail.lower()
            ):
                raise AgentError(
                    f"视觉模型 {self.settings.vision_model} 不支持图片输入。"
                    "请在 VISION_MODEL 中填写支持 image_url 的多模态模型，"
                    "不要使用 DeepSeek 纯文本模型。"
                ) from exc
            raise
        return self._structured_content(response, "角色描述")

    def generate_image(self, prompt: str, size: str = "1024x1024") -> dict[str, Any]:
        if not self.settings.image_enabled:
            raise AgentError("尚未配置图像模型。")
        image_path = urllib.parse.urlparse(
            self.settings.resolved_image_url
        ).path.rstrip("/")
        if image_path.endswith("/chat/completions"):
            raise AgentError(
                "IMAGE_API_BASE_URL 配置错误：图片生成必须填写完整的 "
                "/v1/images/generations 地址，不能填写文本模型的 "
                "/chat/completions 地址。"
            )
        response = self._request(
            "",
            {
                "model": self.settings.image_model,
                "prompt": prompt,
                "size": size,
                "n": 1,
            },
            exact_url=self.settings.resolved_image_url,
            api_key=self.settings.resolved_image_api_key,
        )
        try:
            item = response["data"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise AgentError("图像模型没有返回可用结果。") from exc
        if item.get("url"):
            return {"type": "url", "value": item["url"]}
        if item.get("b64_json"):
            return {"type": "base64", "value": item["b64_json"]}
        raise AgentError("图像模型结果不含 URL 或 base64 数据。")

    def edit_image(
        self,
        prompt: str,
        image: bytes,
        mime_type: str,
        *,
        size: str = "1024x1024",
    ) -> dict[str, Any]:
        return self.edit_images(
            prompt,
            [(image, mime_type)],
            size=size,
        )

    def edit_images(
        self,
        prompt: str,
        images: list[tuple[bytes, str]],
        *,
        size: str = "1024x1024",
    ) -> dict[str, Any]:
        if not self.settings.image_edit_enabled:
            raise AgentError("尚未配置图片编辑模型。")
        if not images:
            raise AgentError("图片编辑至少需要一张参考图。")
        if len(images) > 16:
            raise AgentError("图片编辑最多支持 16 张参考图。")
        edit_path = urllib.parse.urlparse(
            self.settings.image_edit_base_url
        ).path.rstrip("/")
        if edit_path.endswith("/images/generations"):
            raise AgentError(
                "IMAGE_EDIT_API_BASE_URL 配置错误：图片编辑使用 multipart "
                "请求，必须填写完整的 /v1/images/edits 地址，不能填写 "
                "/images/generations。"
            )
        files = []
        for index, (image, mime_type) in enumerate(images, start=1):
            extension = {
                "image/jpeg": ".jpg",
                "image/webp": ".webp",
            }.get(mime_type, ".png")
            filename = (
                f"source{extension}"
                if len(images) == 1
                else f"source-{index:02d}{extension}"
            )
            files.append(
                (
                    "image" if len(images) == 1 else "image[]",
                    filename,
                    mime_type,
                    image,
                )
            )
        response = self._request_multipart(
            self.settings.image_edit_base_url,
            {
                "model": self.settings.image_edit_model,
                "prompt": prompt,
                "size": size,
                "n": "1",
                "input_fidelity": "high",
            },
            files,
            api_key=self.settings.resolved_image_edit_api_key,
        )
        try:
            item = response["data"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise AgentError("图片编辑模型没有返回可用结果。") from exc
        if item.get("url"):
            return {"type": "url", "value": item["url"]}
        if item.get("b64_json"):
            return {"type": "base64", "value": item["b64_json"]}
        raise AgentError("图片编辑结果不含 URL 或 base64 数据。")


SYSTEM_PROMPT = """你是“镜舟”，一位资深短视频总导演与制作统筹 Agent。
你的任务是把模糊创意转换为可执行、可编辑、可交给图像/视频生成模型的制作方案。

原则：
1. 先建立清晰钩子、受众收益与情绪曲线，再写镜头。
2. 角色外观与场景空间描述必须稳定、具体、可重复，用于跨镜头一致性。
3. 每个镜头都要包含画面、动作、机位、音频和连续性提示。
4. 不照搬在世艺术家风格、具体作品镜头或受版权保护角色；用户提供参考时，提取抽象结构并创作原创表达。
5. 不生成冒充真人、欺诈、违法或危险内容。
6. 单个分镜时长尽量控制在 4–15 秒，以便直接交给 Seedance 2.0；长内容拆成更多镜头。
7. 只输出一个 JSON 对象，不要 Markdown，不要解释。

JSON 必须严格符合用户给出的字段结构。所有面向创作者的文字使用简体中文。"""


OUTPUT_SCHEMA = {
    "title": "项目标题",
    "brief": {
        "hook": "前三秒钩子",
        "audience": "目标受众",
        "goal": "传播目标",
        "coreConflict": "核心冲突或问题",
        "tone": "整体语气",
        "durationSeconds": 60,
        "aspectRatio": "9:16",
    },
    "characters": [
        {
            "id": "char-1",
            "name": "角色名",
            "role": "角色功能",
            "visualIdentity": "稳定外观锚点",
            "personality": "性格",
            "voice": "声音设定",
        }
    ],
    "scenes": [
        {
            "id": "scene-1",
            "name": "场景名",
            "imagePrompt": "稳定的场景空间、光线、天气、材质与陈设提示词",
        }
    ],
    "script": {
        "logline": "一句话故事",
        "synopsis": "故事梗概",
        "beats": [
            {
                "beat": "节拍名",
                "duration": 8,
                "purpose": "叙事作用",
                "content": "剧情内容",
            }
        ],
        "narration": "完整旁白或口播稿",
    },
    "storyboard": [
        {
            "shot": 1,
            "duration": 4,
            "scene": "场景",
            "action": "动作",
            "camera": "景别与运镜",
            "visualPrompt": "关键帧图像提示词",
            "videoPrompt": "视频生成提示词",
            "dialogue": "台词或旁白",
            "audio": "音效与音乐",
            "characterIds": ["char-1"],
            "continuity": "连续性注意项",
        }
    ],
    "deliverables": {
        "titleOptions": ["标题 A", "标题 B", "标题 C"],
        "caption": "发布文案",
        "hashtags": ["#标签"],
        "coverPrompt": "封面生成提示词",
        "musicPrompt": "配乐提示词",
        "negativePrompt": "通用负面提示词",
        "checklist": ["制作检查项"],
    },
}


class ProjectStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def save(self, project: dict[str, Any]) -> None:
        with self._lock:
            destination = self.root / f"{project['id']}.json"
            fd, temp_name = tempfile.mkstemp(prefix="jingzhou-", suffix=".json", dir=self.root)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(project, handle, ensure_ascii=False, indent=2)
                os.replace(temp_name, destination)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)

    def list(self) -> list[dict[str, Any]]:
        projects = []
        for path in self.root.glob("*.json"):
            try:
                project = json.loads(path.read_text(encoding="utf-8"))
                projects.append(
                    {
                        "id": project["id"],
                        "title": project["title"],
                        "mode": project["mode"],
                        "modeLabel": project["modeLabel"],
                        "createdAt": project["createdAt"],
                    }
                )
            except (OSError, json.JSONDecodeError, KeyError):
                continue
        return sorted(projects, key=lambda item: item["createdAt"], reverse=True)

    def get(self, project_id: str) -> dict[str, Any] | None:
        if not re.fullmatch(r"[a-f0-9-]{36}", project_id):
            return None
        with self._lock:
            path = self.root / f"{project_id}.json"
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None

    def mutate(
        self, project_id: str, mutator: Any
    ) -> dict[str, Any] | None:
        with self._lock:
            project = self.get(project_id)
            if project is None:
                return None
            mutator(project)
            project["updatedAt"] = utc_now()
            self.save(project)
            return project

    def iter_projects(self) -> list[dict[str, Any]]:
        projects = []
        with self._lock:
            for path in self.root.glob("*.json"):
                try:
                    projects.append(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    continue
        return projects

    def delete(self, project_id: str) -> bool:
        if not re.fullmatch(r"[a-f0-9-]{36}", project_id):
            return False
        with self._lock:
            path = self.root / f"{project_id}.json"
            if not path.is_file():
                return False
            path.unlink()
            return True


class CreativeAgent:
    def __init__(
        self,
        store: ProjectStore,
        settings: ModelSettings | None = None,
        media_root: Path | None = None,
    ):
        self.store = store
        self.settings = settings or ModelSettings.from_environment()
        self.provider = OpenAICompatibleProvider(self.settings)
        self.media_root = media_root
        if self.media_root:
            self.media_root.mkdir(parents=True, exist_ok=True)

    def config(self) -> dict[str, Any]:
        return {
            "textModelEnabled": self.settings.text_enabled,
            "imageModelEnabled": self.settings.image_enabled,
            "imageEditModelEnabled": self.settings.image_edit_enabled,
            "visionModelEnabled": self.settings.vision_enabled,
            "model": self.settings.model if self.settings.text_enabled else "内置演示引擎",
            "imageModel": (
                self.settings.image_model
                if self.settings.image_enabled
                else "尚未配置"
            ),
            "imageEditModel": (
                self.settings.image_edit_model
                if self.settings.image_edit_enabled
                else "尚未配置"
            ),
            "visionModel": (
                self.settings.vision_model
                if self.settings.vision_enabled
                else "尚未配置"
            ),
            "modes": [
                {"id": mode_id, "label": label}
                for mode_id, label in MODE_LABELS.items()
            ],
        }

    def validate_request(self, request: dict[str, Any]) -> dict[str, Any]:
        mode = request.get("mode", "short_drama")
        if mode not in MODE_LABELS:
            raise AgentError("未知创作模式。")
        idea = str(request.get("idea", "")).strip()
        if not idea:
            raise AgentError("请先输入创意或素材说明。")
        if len(idea) > MAX_IDEA_LENGTH:
            raise AgentError(f"创意内容不能超过 {MAX_IDEA_LENGTH} 字。")
        duration = int(request.get("duration", 60))
        if duration not in ALLOWED_DURATIONS:
            raise AgentError("不支持该时长。")
        aspect_ratio = request.get("aspectRatio", "9:16")
        if aspect_ratio not in ALLOWED_ASPECT_RATIOS:
            raise AgentError("不支持该画幅。")
        return {
            "mode": mode,
            "idea": idea,
            "audience": str(request.get("audience", "")).strip() or "泛短视频用户",
            "duration": duration,
            "aspectRatio": aspect_ratio,
            "style": str(request.get("style", "")).strip() or "电影感写实",
            "tone": str(request.get("tone", "")).strip() or "有张力、节奏明快",
            "requirements": str(request.get("requirements", "")).strip(),
        }

    def generate(self, raw_request: dict[str, Any]) -> dict[str, Any]:
        request = self.validate_request(raw_request)
        if self.settings.text_enabled:
            project = self._generate_with_model(request)
            engine = "model"
        else:
            project = self._generate_demo(request)
            engine = "demo"

        project.update(
            {
                "id": str(uuid.uuid4()),
                "mode": request["mode"],
                "modeLabel": MODE_LABELS[request["mode"]],
                "createdAt": utc_now(),
                "updatedAt": utc_now(),
                "engine": engine,
                "sourceIdea": request["idea"],
            }
        )
        self._normalize_project(project, request)
        self.store.save(project)
        return project

    def _generate_with_model(self, request: dict[str, Any]) -> dict[str, Any]:
        prompt = (
            f"创作模式：{MODE_LABELS[request['mode']]}\n"
            f"原始创意：{request['idea']}\n"
            f"目标受众：{request['audience']}\n"
            f"目标时长：{request['duration']} 秒\n"
            f"画幅：{request['aspectRatio']}\n"
            f"视觉风格：{request['style']}\n"
            f"语气：{request['tone']}\n"
            f"附加要求：{request['requirements'] or '无'}\n\n"
            f"请严格返回这个字段结构：\n"
            f"{json.dumps(OUTPUT_SCHEMA, ensure_ascii=False, indent=2)}"
        )
        return self.provider.generate_project(SYSTEM_PROMPT, prompt)

    def _generate_demo(self, request: dict[str, Any]) -> dict[str, Any]:
        idea = request["idea"]
        title_seed = re.split(r"[。！？\n]", idea)[0][:18]
        title = f"{title_seed}｜{MODE_LABELS[request['mode']]}"
        duration = request["duration"]
        shot_count = max(4, min(12, round(duration / 8)))
        shot_duration = max(2, round(duration / shot_count))

        if request["mode"] == "digital_human":
            characters = [
                {
                    "id": "char-1",
                    "name": "主讲人",
                    "role": "可信赖的知识型讲述者",
                    "visualIdentity": "28 岁东亚面孔，短黑发，深灰西装外套，暖白内搭，左胸银色别针",
                    "personality": "清晰、真诚、不过度表演",
                    "voice": "中性偏温暖，语速每分钟 230 字，重点处短停顿",
                }
            ]
        else:
            characters = [
                {
                    "id": "char-1",
                    "name": "林舟",
                    "role": "推动变化的主角",
                    "visualIdentity": "25 岁东亚青年，齐肩黑发，墨绿色短夹克，米白帆布包，右手银色手表",
                    "personality": "克制、敏锐，遇到关键选择时果断",
                    "voice": "年轻自然，情绪由平静逐渐坚定",
                },
                {
                    "id": "char-2",
                    "name": "顾野",
                    "role": "制造阻力与反转的对手",
                    "visualIdentity": "30 岁东亚男性，利落短发，黑色高领衫，深色长风衣，细框眼镜",
                    "personality": "冷静、精确，隐藏真实动机",
                    "voice": "低沉克制，句尾轻收",
                },
            ]

        beat_names = ["钩子", "建立情境", "冲突升级", "关键反转", "行动兑现", "余韵与召唤"]
        beats = []
        for index, beat_name in enumerate(beat_names):
            beats.append(
                {
                    "beat": beat_name,
                    "duration": round(duration / len(beat_names)),
                    "purpose": [
                        "立刻制造信息缺口",
                        "让观众理解人物与目标",
                        "提高失败代价",
                        "重写观众对事件的理解",
                        "给出可视化解决动作",
                        "留下记忆点并引导互动",
                    ][index],
                    "content": f"围绕“{title_seed}”推进{beat_name}，用一个可视动作而非解释完成信息传递。",
                }
            )

        storyboard = []
        camera_plan = [
            "极近景快速推入",
            "中景手持跟拍",
            "过肩镜头缓慢横移",
            "广角固定镜头",
            "特写轻微环绕",
            "中近景稳定器前移",
            "俯拍快速下压",
            "广角拉远收束",
            "主观镜头轻微晃动",
            "定格感近景",
            "低机位平稳后退",
            "侧面中景缓慢推近",
        ]
        for index in range(shot_count):
            shot_number = index + 1
            phase = beat_names[min(len(beat_names) - 1, index * len(beat_names) // shot_count)]
            scene = "有层次的现代室内空间" if index % 2 == 0 else "傍晚城市街道，远处霓虹初亮"
            action = (
                f"{characters[0]['name']}发现与“{title_seed}”有关的关键细节，"
                f"通过明确的手部动作推动{phase}。"
            )
            visual_anchor = characters[0]["visualIdentity"]
            storyboard.append(
                {
                    "shot": shot_number,
                    "duration": shot_duration if index < shot_count - 1 else max(
                        2, duration - shot_duration * (shot_count - 1)
                    ),
                    "scene": scene,
                    "action": action,
                    "camera": camera_plan[index],
                    "visualPrompt": (
                        f"{request['style']}，{scene}，{visual_anchor}，{action}，"
                        f"{camera_plan[index]}，电影级布光，真实材质，{request['aspectRatio']} 构图"
                    ),
                    "videoPrompt": (
                        f"{shot_duration} 秒，{camera_plan[index]}。人物先短暂停顿再完成动作，"
                        "衣着、脸型、发型保持一致；自然运动模糊，镜头焦点稳定，结尾留 0.5 秒剪辑点。"
                    ),
                    "dialogue": f"{phase}：{idea[:42]}" if index in {0, shot_count - 1} else "",
                    "audio": "低频节拍渐进，关键动作处加入干净的瞬态音效",
                    "characterIds": ["char-1"],
                    "continuity": "保持墨绿色夹克、米白帆布包与右手银色手表；光线方向由画面左侧进入。",
                }
            )

        return {
            "title": title,
            "brief": {
                "hook": f"如果“{title_seed}”的真相，和你看到的完全相反呢？",
                "audience": request["audience"],
                "goal": "在前三秒建立悬念，并让观众完整看完后愿意评论自己的选择。",
                "coreConflict": f"主角必须在有限时间内验证“{title_seed}”，否则失去改变结果的机会。",
                "tone": request["tone"],
                "durationSeconds": duration,
                "aspectRatio": request["aspectRatio"],
            },
            "characters": characters,
            "script": {
                "logline": f"一个普通人因“{title_seed}”被迫做出一次无法撤回的选择。",
                "synopsis": (
                    f"故事从异常细节切入，围绕“{idea[:80]}”建立目标与阻力。"
                    "中段用一次视觉化反转改变因果关系，结尾通过主角的主动选择完成情绪兑现。"
                ),
                "beats": beats,
                "narration": (
                    f"我原本以为，{title_seed}只是偶然。直到那个细节第二次出现。"
                    "真正的问题不是我有没有看见，而是看见之后，我还愿不愿意做出选择。"
                    "如果是你，会停下来，还是继续往前？"
                ),
            },
            "storyboard": storyboard,
            "deliverables": {
                "titleOptions": [
                    f"我差点错过“{title_seed}”背后的真相",
                    f"看懂这个细节，你就懂了{title_seed}",
                    f"最后 3 秒，整个故事反过来了",
                ],
                "caption": f"有些答案不藏在远方，而藏在被我们忽略的那个瞬间。关于{title_seed}，你会怎么选？",
                "hashtags": ["#AI短片", "#故事感", "#短视频创作", "#镜舟Agent"],
                "coverPrompt": (
                    f"{request['style']}短视频封面，主角侧脸近景，前景一个关键物件，"
                    "背景形成强烈明暗分区，预留上方中文标题区，高对比，高辨识度"
                ),
                "musicPrompt": "90 BPM 现代电影氛围配乐，前半克制脉冲，中段加入弦乐张力，结尾留出呼吸感。",
                "negativePrompt": "角色变脸，多余手指，服装变化，文字乱码，水印，低清晰度，过曝，镜头抖动，物体穿模。",
                "checklist": [
                    "逐镜头核对角色外观锚点",
                    "确认镜头时长总和与目标时长一致",
                    "旁白高峰与反转镜头对齐",
                    "封面不直接泄露结局",
                    "发布前确认素材版权与人物授权",
                ],
            },
        }

    def _normalize_project(
        self, project: dict[str, Any], request: dict[str, Any]
    ) -> None:
        if not isinstance(project, dict):
            raise AgentError("创作方案格式错误。")
        project.setdefault("title", f"{slugify(request['idea'])}｜{MODE_LABELS[request['mode']]}")
        project.setdefault("brief", {})
        project["brief"].setdefault("audience", request["audience"])
        project["brief"].setdefault("durationSeconds", request["duration"])
        project["brief"].setdefault("aspectRatio", request["aspectRatio"])
        project.setdefault("characters", [])
        project.setdefault("script", {"beats": [], "narration": ""})
        project.setdefault("storyboard", [])
        project.setdefault("deliverables", {})
        project.setdefault("assets", [])
        project.setdefault("chatHistory", [])
        project.setdefault("pendingChatProposal", None)
        project.setdefault(
            "stagePrompts",
            {
                "overview": "完善创意钩子、受众、冲突、语气与传播目标。",
                "script": "扩写故事梗概、叙事节拍、台词和旁白，保持因果清晰。",
                "characters": "固定角色外观、服装、性格、声音和跨镜头识别锚点。",
                "storyboard": "将剧本拆成可执行分镜，明确场景、动作、机位、声音与连续性。",
                "video": "保持角色和场景一致，动作自然，镜头稳定，结尾保留剪辑点。",
                "publish": "生成标题、发布文案、封面、音乐和交付检查项。",
            },
        )

        characters = project["characters"]
        if not isinstance(characters, list):
            project["characters"] = []
            characters = project["characters"]
        for index, character in enumerate(characters, start=1):
            if not isinstance(character, dict):
                characters[index - 1] = {}
                character = characters[index - 1]
            character.setdefault("id", f"char-{index}")
            character.setdefault("name", f"角色 {index}")
            character.setdefault("role", "")
            character.setdefault("visualIdentity", "")
            character.setdefault("personality", "")
            character.setdefault("voice", "")
            character.setdefault(
                "imagePrompt",
                (
                    f"原创影视角色设定图，{character.get('name', '')}，"
                    f"{character.get('visualIdentity', '')}，正面全身，"
                    "中性站姿，纯净背景，真实材质，稳定面部与服装细节，"
                    "不含文字、Logo、水印"
                ),
            )
            character.setdefault("referenceImageIds", [])

        existing_scenes = project.get("scenes")
        if not isinstance(existing_scenes, list):
            existing_scenes = []
        normalized_scenes = []
        for index, scene in enumerate(existing_scenes, start=1):
            if not isinstance(scene, dict):
                continue
            scene.setdefault("id", f"scene-{index}")
            scene.setdefault("name", f"场景 {index}")
            scene.setdefault(
                "imagePrompt",
                (
                    f"原创影视场景概念图，{scene.get('name', '')}，"
                    "广角建立镜头，无人物，明确空间结构、光线方向、"
                    "时间天气、陈设与材质细节，不含文字、Logo、水印"
                ),
            )
            scene.setdefault("referenceImageIds", [])
            normalized_scenes.append(scene)
        existing_scenes = normalized_scenes
        scene_by_name = {
            str(scene.get("name", "")): scene
            for scene in existing_scenes
            if isinstance(scene, dict) and scene.get("name")
        }
        scene_by_id = {
            str(scene.get("id", "")): scene
            for scene in existing_scenes
            if isinstance(scene, dict) and scene.get("id")
        }

        shots = project["storyboard"]
        if not isinstance(shots, list):
            project["storyboard"] = []
            shots = project["storyboard"]
        for index, shot in enumerate(shots):
            if not isinstance(shot, dict):
                shots[index] = {"shot": index + 1}
                shot = shots[index]
            shot["shot"] = index + 1
            shot.setdefault("duration", 4)
            shot.setdefault("scene", "")
            shot.setdefault("action", "")
            shot.setdefault("camera", "")
            shot.setdefault("visualPrompt", "")
            shot.setdefault("videoPrompt", "")
            shot.setdefault("completeVideoPrompt", "")
            shot.setdefault("completeVideoPromptStale", False)
            shot.setdefault("completeVideoPromptGeneratedAt", "")
            shot.setdefault("dialogue", "")
            shot.setdefault("audio", "")
            shot.setdefault("characterIds", [])
            shot.setdefault("continuity", "")
            shot.setdefault("id", f"shot-{shot.get('shot', index + 1)}")
            scene_name = str(shot.get("scene") or f"场景 {index + 1}")
            scene = scene_by_name.get(scene_name)
            if not scene:
                scene = {
                    "id": f"scene-{len(scene_by_name) + 1}",
                    "name": scene_name,
                    "imagePrompt": (
                        f"原创影视场景概念图，{scene_name}，"
                        "广角建立镜头，无人物，明确空间结构、光线方向、"
                        "时间天气与材质细节，不含文字、Logo、水印"
                    ),
                    "referenceImageIds": [],
                }
                scene_by_name[scene_name] = scene
                scene_by_id[str(scene["id"])] = scene
            scene.setdefault("imagePrompt", "")
            scene.setdefault("referenceImageIds", [])
            current_scene = scene_by_id.get(
                str(shot.get("sceneId") or "")
            )
            if (
                not current_scene
                or str(current_scene.get("name") or "") != scene_name
            ):
                shot["sceneId"] = scene["id"]
            raw_reference_ids = shot.get("referenceAssetIds", [])
            if not isinstance(raw_reference_ids, list):
                raw_reference_ids = []
            shot["referenceAssetIds"] = list(
                dict.fromkeys(
                    str(value)
                    for value in raw_reference_ids
                    if str(value)
                )
            )[:MAX_VIDEO_REFERENCE_IMAGES]
            shot.setdefault("startFrameAssetId", "")
            shot.setdefault("endFrameAssetId", "")
        project["scenes"] = list(scene_by_name.values())
        character_by_id = {
            str(character.get("id")): character
            for character in project.get("characters", [])
            if isinstance(character, dict) and character.get("id")
        }
        appearances: dict[str, list[dict[str, Any]]] = {
            character_id: [] for character_id in character_by_id
        }
        for shot in shots:
            shot["characterIds"] = list(
                dict.fromkeys(
                    str(value)
                    for value in shot.get("characterIds", [])
                    if str(value) in character_by_id
                )
            )
            for character_id in shot["characterIds"]:
                appearances[character_id].append(
                    {
                        "shotId": str(shot.get("id") or ""),
                        "shot": int(shot.get("shot") or 0),
                        "scene": str(shot.get("scene") or ""),
                    }
                )
        for character_id, character in character_by_id.items():
            character["appearances"] = appearances[character_id]
            character["appearanceCount"] = len(
                appearances[character_id]
            )

    def generate_image(self, raw_request: dict[str, Any]) -> dict[str, Any]:
        prompt = str(raw_request.get("prompt", "")).strip()
        if not prompt or len(prompt) > 4_000:
            raise AgentError("图像提示词不能为空且不能超过 4000 字。")
        size = str(raw_request.get("size", "1024x1024"))
        if size not in {"1024x1024", "1536x1024", "1024x1536"}:
            raise AgentError("不支持该图像尺寸。")
        return self.provider.generate_image(prompt, size)

    def prepare_project(self, project_id: str) -> dict[str, Any] | None:
        project = self.store.get(project_id)
        if not project:
            return None
        before = json.dumps(project, ensure_ascii=False, sort_keys=True)
        brief = project.get("brief") or {}
        request = {
            "mode": project.get("mode", "short_drama"),
            "idea": project.get("sourceIdea", project.get("title", "")),
            "audience": brief.get("audience", "泛短视频用户"),
            "duration": int(brief.get("durationSeconds") or 60),
            "aspectRatio": brief.get("aspectRatio", "9:16"),
        }
        self._normalize_project(project, request)
        after = json.dumps(project, ensure_ascii=False, sort_keys=True)
        if before != after:
            self.store.save(project)
        return project

    @staticmethod
    def _find_item(
        items: Any,
        item_id: str,
        label: str,
    ) -> dict[str, Any]:
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and str(item.get("id")) == item_id:
                    return item
        raise AgentError(f"{label}不存在。")

    @staticmethod
    def _mark_complete_video_prompts_stale(
        project: dict[str, Any],
        shot_id: str = "",
    ) -> None:
        for shot in project.get("storyboard", []):
            if not isinstance(shot, dict):
                continue
            if shot_id and str(shot.get("id") or "") != shot_id:
                continue
            if str(shot.get("completeVideoPrompt") or "").strip():
                shot["completeVideoPromptStale"] = True

    def _shot_video_prompt_context(
        self,
        project: dict[str, Any],
        shot: dict[str, Any],
    ) -> dict[str, Any]:
        characters = {
            str(item.get("id")): item
            for item in project.get("characters", [])
            if isinstance(item, dict)
        }
        scenes = {
            str(item.get("id")): item
            for item in project.get("scenes", [])
            if isinstance(item, dict)
        }
        assets = {
            str(item.get("id")): item
            for item in project.get("assets", [])
            if isinstance(item, dict)
        }
        selected_asset_ids = list(
            dict.fromkeys(
                str(value)
                for value in shot.get("referenceAssetIds", [])
                if str(value) in assets
            )
        )[:MAX_VIDEO_REFERENCE_IMAGES]
        for character_id in shot.get("characterIds", []):
            character = characters.get(str(character_id)) or {}
            for asset_id in character.get("referenceImageIds", [])[:1]:
                normalized = str(asset_id)
                if (
                    normalized in assets
                    and normalized not in selected_asset_ids
                    and len(selected_asset_ids) < MAX_VIDEO_REFERENCE_IMAGES
                ):
                    selected_asset_ids.append(normalized)
        scene = scenes.get(str(shot.get("sceneId") or "")) or {}
        for asset_id in scene.get("referenceImageIds", [])[:1]:
            normalized = str(asset_id)
            if (
                normalized in assets
                and normalized not in selected_asset_ids
                and len(selected_asset_ids) < MAX_VIDEO_REFERENCE_IMAGES
            ):
                selected_asset_ids.append(normalized)
        start_id = str(shot.get("startFrameAssetId") or "")
        end_id = str(shot.get("endFrameAssetId") or "")
        ordered_asset_ids = list(selected_asset_ids)
        for asset_id in (start_id, end_id):
            if asset_id in assets and asset_id not in ordered_asset_ids:
                ordered_asset_ids.append(asset_id)

        image_context = []
        for asset_id in ordered_asset_ids:
            asset = assets[asset_id]
            owner_type = str(asset.get("ownerType") or "")
            owner_id = str(asset.get("ownerId") or "")
            owner = (
                characters.get(owner_id)
                if owner_type == "character"
                else scenes.get(owner_id)
                if owner_type == "scene"
                else {}
            ) or {}
            usages = []
            if asset_id in selected_asset_ids:
                usages.append("reference_image")
            if asset_id == start_id:
                usages.append("first_frame")
            if asset_id == end_id:
                usages.append("last_frame")
            image_context.append(
                {
                    "assetId": asset_id,
                    "ownerType": owner_type,
                    "ownerId": owner_id,
                    "ownerName": owner.get("name") or asset.get("ownerName") or "",
                    "usage": usages,
                    "prompt": (
                        asset.get("editPrompt")
                        or asset.get("prompt")
                        or owner.get("imagePrompt")
                        or ""
                    ),
                }
            )

        storyboard = [
            {
                key: value
                for key, value in item.items()
                if key
                not in {
                    "referenceAssetIds",
                    "startFrameAssetId",
                    "endFrameAssetId",
                    "completeVideoPrompt",
                }
            }
            for item in project.get("storyboard", [])
            if isinstance(item, dict)
        ]
        return {
            "project": {
                "title": project.get("title"),
                "brief": project.get("brief"),
                "script": project.get("script"),
                "stagePrompts": {
                    "storyboard": (project.get("stagePrompts") or {}).get(
                        "storyboard",
                        "",
                    ),
                    "video": (project.get("stagePrompts") or {}).get(
                        "video",
                        "",
                    ),
                },
            },
            "fullStoryboard": storyboard,
            "currentShot": {
                key: value
                for key, value in shot.items()
                if key not in {"completeVideoPrompt"}
            },
            "selectedCharacters": [
                characters[str(character_id)]
                for character_id in shot.get("characterIds", [])
                if str(character_id) in characters
            ],
            "selectedScene": scene,
            "referenceImages": image_context,
        }

    def generate_shot_video_prompt(
        self,
        project_id: str,
        shot_id: str,
    ) -> dict[str, Any]:
        project = self.prepare_project(project_id)
        if not project:
            raise AgentError("项目不存在。")
        self._assert_project_editable(project)
        shot = self._find_item(project.get("storyboard"), shot_id, "分镜")
        prompt = self.provider.generate_video_prompt(
            self._shot_video_prompt_context(project, shot)
        )

        def save(current: dict[str, Any]) -> None:
            self._assert_project_editable(current)
            target = self._find_item(
                current.get("storyboard"),
                shot_id,
                "分镜",
            )
            target["completeVideoPrompt"] = prompt
            target["completeVideoPromptStale"] = False
            target["completeVideoPromptGeneratedAt"] = utc_now()
            self._mark_video_production_stale(
                current,
                "完整视频提示词已重新生成",
            )

        updated = self.store.mutate(project_id, save)
        if not updated:
            raise AgentError("项目不存在。")
        return updated

    def update_field(
        self,
        project_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        scope = str(payload.get("scope", "")).strip()
        item_id = str(payload.get("id", "")).strip()
        field = str(payload.get("field", "")).strip()
        value = payload.get("value")
        allowed: dict[str, set[str]] = {
            "stage": EDITABLE_STAGE_NAMES,
            "brief": {
                "hook",
                "audience",
                "goal",
                "coreConflict",
                "tone",
            },
            "script": {"logline", "synopsis", "narration"},
            "character": {
                "name",
                "role",
                "visualIdentity",
                "personality",
                "voice",
                "imagePrompt",
            },
            "scene": {"name", "imagePrompt"},
            "shot": {
                "scene",
                "action",
                "camera",
                "visualPrompt",
                "videoPrompt",
                "completeVideoPrompt",
                "dialogue",
                "audio",
                "continuity",
                "duration",
                "characterIds",
                "referenceAssetIds",
                "startFrameAssetId",
                "endFrameAssetId",
            },
            "deliverable": {
                "caption",
                "coverPrompt",
                "musicPrompt",
                "negativePrompt",
            },
        }
        if scope not in allowed or field not in allowed[scope]:
            raise AgentError("该字段不允许修改。")
        if scope == "stage":
            if item_id not in EDITABLE_STAGE_NAMES:
                raise AgentError("未知创作环节。")
            field = item_id
        if field in {"characterIds", "referenceAssetIds"}:
            if not isinstance(value, list):
                raise AgentError("素材绑定必须是数组。")
            value = list(
                dict.fromkeys(
                    str(item)[:100]
                    for item in value
                    if str(item)
                )
            )
            max_items = (
                MAX_VIDEO_REFERENCE_IMAGES
                if field == "referenceAssetIds"
                else 50
            )
            if len(value) > max_items:
                if field == "referenceAssetIds":
                    raise AgentError(
                        f"每个分镜最多选择 {MAX_VIDEO_REFERENCE_IMAGES} 张"
                        "视频参考图。"
                    )
                raise AgentError("出镜角色绑定不能超过 50 项。")
        elif field == "duration":
            value = max(1, min(180, int(value)))
        else:
            value = str(value or "").strip()
            if len(value) > 8_000:
                raise AgentError("字段内容不能超过 8000 字。")

        def mutate(project: dict[str, Any]) -> None:
            if scope in {"shot", "character", "scene"}:
                self._assert_project_editable(project)
            if scope == "stage":
                project.setdefault("stagePrompts", {})[field] = value
                if field in {"storyboard", "video"}:
                    self._mark_complete_video_prompts_stale(project)
                return
            if scope in {"brief", "script"}:
                project.setdefault(scope, {})[field] = value
                self._mark_complete_video_prompts_stale(project)
                return
            if scope == "deliverable":
                project.setdefault("deliverables", {})[field] = value
                return
            collection_name = {
                "character": "characters",
                "scene": "scenes",
                "shot": "storyboard",
            }[scope]
            target = self._find_item(
                project.get(collection_name),
                item_id,
                {
                    "character": "角色",
                    "scene": "场景",
                    "shot": "分镜",
                }[scope],
            )
            if field == "referenceAssetIds":
                valid_ids = {
                    str(asset.get("id"))
                    for asset in project.get("assets", [])
                    if isinstance(asset, dict)
                }
                if any(asset_id not in valid_ids for asset_id in value):
                    raise AgentError("包含不存在的参考素材。")
            if field == "characterIds":
                valid_ids = {
                    str(character.get("id"))
                    for character in project.get("characters", [])
                    if isinstance(character, dict)
                }
                if any(character_id not in valid_ids for character_id in value):
                    raise AgentError("包含不存在的出镜角色。")
            target[field] = value
            if scope == "shot":
                if field == "completeVideoPrompt":
                    target["completeVideoPromptStale"] = False
                    target["completeVideoPromptGeneratedAt"] = utc_now()
                else:
                    self._mark_complete_video_prompts_stale(
                        project,
                        item_id,
                    )
            elif scope in {"character", "scene"}:
                self._mark_complete_video_prompts_stale(project)
            if scope in {"shot", "character", "scene"}:
                self._normalize_project(
                    project,
                    self._project_request(project),
                )
                reason_label = {
                    "shot": "分镜内容或素材绑定已修改",
                    "character": "角色设定已修改",
                    "scene": "场景设定已修改",
                }[scope]
                self._mark_video_production_stale(
                    project,
                    reason_label,
                )

        updated = self.store.mutate(project_id, mutate)
        if not updated:
            raise AgentError("项目不存在。")
        return updated

    @staticmethod
    def _assert_project_editable(project: dict[str, Any]) -> None:
        production = project.get("videoProduction") or {}
        if production.get("status") in ACTIVE_VIDEO_PRODUCTION_STATUSES:
            raise AgentError("视频正在生成中，请完成后再修改项目结构。")

    @staticmethod
    def _mark_video_production_stale(
        project: dict[str, Any],
        reason: str,
    ) -> None:
        production = project.get("videoProduction")
        if not isinstance(production, dict):
            return
        if production.get("status") in ACTIVE_VIDEO_PRODUCTION_STATUSES:
            raise AgentError("视频正在生成中，请完成后再修改项目结构。")
        revision = int(project.get("storyboardRevision") or 0) + 1
        project["storyboardRevision"] = revision
        production["stale"] = True
        production["staleReason"] = reason[:500]
        production["staleAt"] = utc_now()
        production["storyboardRevision"] = revision
        production["currentStoryboardShotCount"] = len(
            project.get("storyboard", [])
        )

    @staticmethod
    def _project_request(project: dict[str, Any]) -> dict[str, Any]:
        brief = project.get("brief") or {}
        return {
            "mode": project.get("mode", "short_drama"),
            "idea": project.get("sourceIdea", project.get("title", "")),
            "audience": brief.get("audience", "泛短视频用户"),
            "duration": int(brief.get("durationSeconds") or 60),
            "aspectRatio": brief.get("aspectRatio", "9:16"),
        }

    @staticmethod
    def _new_character(payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload.get("name") or "新角色").strip()[:100]
        visual_identity = str(
            payload.get("visualIdentity") or ""
        ).strip()[:4_000]
        image_prompt = str(payload.get("imagePrompt") or "").strip()
        if not image_prompt:
            image_prompt = (
                f"原创影视角色设定图，{name}，{visual_identity}，"
                "正面全身，中性站姿，纯净背景，稳定面部与服装细节，"
                "不含文字、Logo、水印"
            )
        return {
            "id": f"char-{uuid.uuid4().hex[:10]}",
            "name": name,
            "role": str(payload.get("role") or "").strip()[:2_000],
            "visualIdentity": visual_identity,
            "personality": str(
                payload.get("personality") or ""
            ).strip()[:2_000],
            "voice": str(payload.get("voice") or "").strip()[:2_000],
            "imagePrompt": image_prompt[:4_000],
            "referenceImageIds": [],
        }

    @staticmethod
    def _new_scene(payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload.get("name") or "新场景").strip()[:200]
        image_prompt = str(payload.get("imagePrompt") or "").strip()
        if not image_prompt:
            image_prompt = (
                f"原创影视场景概念图，{name}，广角建立镜头，无人物，"
                "明确空间结构、光线方向、时间天气、陈设与材质细节，"
                "不含文字、Logo、水印"
            )
        return {
            "id": f"scene-{uuid.uuid4().hex[:10]}",
            "name": name,
            "imagePrompt": image_prompt[:4_000],
            "referenceImageIds": [],
        }

    @staticmethod
    def _new_shot(
        payload: dict[str, Any],
        valid_character_ids: set[str],
    ) -> dict[str, Any]:
        character_ids = payload.get("characterIds", [])
        if not isinstance(character_ids, list):
            character_ids = []
        try:
            duration = int(payload.get("duration") or 5)
        except (TypeError, ValueError):
            duration = 5
        return {
            "id": f"shot-{uuid.uuid4().hex[:10]}",
            "shot": 0,
            "duration": max(1, min(180, duration)),
            "scene": str(payload.get("scene") or "新场景").strip()[:1_000],
            "action": str(payload.get("action") or "").strip()[:8_000],
            "camera": str(payload.get("camera") or "中景固定镜头").strip()[:4_000],
            "visualPrompt": str(
                payload.get("visualPrompt") or ""
            ).strip()[:8_000],
            "videoPrompt": str(
                payload.get("videoPrompt") or ""
            ).strip()[:8_000],
            "completeVideoPrompt": "",
            "completeVideoPromptStale": False,
            "completeVideoPromptGeneratedAt": "",
            "dialogue": str(payload.get("dialogue") or "").strip()[:8_000],
            "audio": str(payload.get("audio") or "").strip()[:4_000],
            "continuity": str(
                payload.get("continuity") or ""
            ).strip()[:4_000],
            "characterIds": [
                str(value)
                for value in character_ids
                if str(value) in valid_character_ids
            ],
            "referenceAssetIds": [],
            "startFrameAssetId": "",
            "endFrameAssetId": "",
        }

    def add_character(
        self,
        project_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        character = self._new_character(payload)

        def add(project: dict[str, Any]) -> None:
            self._assert_project_editable(project)
            project.setdefault("characters", []).append(character)
            self._normalize_project(
                project,
                self._project_request(project),
            )
            self._mark_video_production_stale(
                project,
                "新增角色后，视频任务需要按最新角色设定重新生成",
            )

        updated = self.store.mutate(project_id, add)
        if not updated:
            raise AgentError("项目不存在。")
        return updated

    def add_scene(
        self,
        project_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        scene = self._new_scene(payload)

        def add(project: dict[str, Any]) -> None:
            self._assert_project_editable(project)
            existing_names = {
                str(item.get("name") or "").strip()
                for item in project.get("scenes", [])
                if isinstance(item, dict)
            }
            if scene["name"] in existing_names:
                raise AgentError("同名场景已经存在。")
            project.setdefault("scenes", []).append(scene)
            self._normalize_project(
                project,
                self._project_request(project),
            )
            self._mark_video_production_stale(
                project,
                "新增场景后，视频任务需要按最新场景设定重新生成",
            )

        updated = self.store.mutate(project_id, add)
        if not updated:
            raise AgentError("项目不存在。")
        return updated

    def delete_character(
        self,
        project_id: str,
        character_id: str,
    ) -> dict[str, Any]:
        def remove(project: dict[str, Any]) -> None:
            self._assert_project_editable(project)
            self._find_item(
                project.get("characters"),
                character_id,
                "角色",
            )
            removed_asset_ids = {
                str(asset.get("id") or "")
                for asset in project.get("assets", [])
                if isinstance(asset, dict)
                and asset.get("ownerType") == "character"
                and str(asset.get("ownerId") or "") == character_id
            }
            project["characters"] = [
                item
                for item in project.get("characters", [])
                if str(item.get("id") or "") != character_id
            ]
            project["assets"] = [
                asset
                for asset in project.get("assets", [])
                if str(asset.get("id") or "") not in removed_asset_ids
            ]
            for shot in project.get("storyboard", []):
                shot["characterIds"] = [
                    value
                    for value in shot.get("characterIds", [])
                    if str(value) != character_id
                ]
                shot["referenceAssetIds"] = [
                    value
                    for value in shot.get("referenceAssetIds", [])
                    if str(value) not in removed_asset_ids
                ]
                if str(shot.get("startFrameAssetId") or "") in removed_asset_ids:
                    shot["startFrameAssetId"] = ""
                if str(shot.get("endFrameAssetId") or "") in removed_asset_ids:
                    shot["endFrameAssetId"] = ""
            self._normalize_project(project, self._project_request(project))
            self._mark_video_production_stale(
                project,
                "角色及其参考图已删除",
            )

        updated = self.store.mutate(project_id, remove)
        if not updated:
            raise AgentError("项目不存在。")
        return updated

    def delete_scene(
        self,
        project_id: str,
        scene_id: str,
    ) -> dict[str, Any]:
        def remove(project: dict[str, Any]) -> None:
            self._assert_project_editable(project)
            scene = self._find_item(project.get("scenes"), scene_id, "场景")
            if any(
                str(shot.get("sceneId") or "") == scene_id
                or str(shot.get("scene") or "") == str(scene.get("name") or "")
                for shot in project.get("storyboard", [])
                if isinstance(shot, dict)
            ):
                raise AgentError("该场景仍被分镜使用，请先修改或删除相关分镜。")
            removed_asset_ids = {
                str(asset.get("id") or "")
                for asset in project.get("assets", [])
                if isinstance(asset, dict)
                and asset.get("ownerType") == "scene"
                and str(asset.get("ownerId") or "") == scene_id
            }
            project["scenes"] = [
                item
                for item in project.get("scenes", [])
                if str(item.get("id") or "") != scene_id
            ]
            project["assets"] = [
                asset
                for asset in project.get("assets", [])
                if str(asset.get("id") or "") not in removed_asset_ids
            ]
            for shot in project.get("storyboard", []):
                shot["referenceAssetIds"] = [
                    value
                    for value in shot.get("referenceAssetIds", [])
                    if str(value) not in removed_asset_ids
                ]
            self._normalize_project(project, self._project_request(project))
            self._mark_video_production_stale(
                project,
                "场景及其参考图已删除",
            )

        updated = self.store.mutate(project_id, remove)
        if not updated:
            raise AgentError("项目不存在。")
        return updated

    def add_shot(
        self,
        project_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        project = self.prepare_project(project_id)
        if not project:
            raise AgentError("项目不存在。")
        valid_character_ids = {
            str(item.get("id"))
            for item in project.get("characters", [])
            if isinstance(item, dict)
        }
        shot = self._new_shot(payload, valid_character_ids)
        after_shot_id = str(payload.get("afterShotId") or "")

        def add(current: dict[str, Any]) -> None:
            self._assert_project_editable(current)
            storyboard = current.setdefault("storyboard", [])
            insert_at = len(storyboard)
            if after_shot_id:
                for index, item in enumerate(storyboard):
                    if str(item.get("id") or "") == after_shot_id:
                        insert_at = index + 1
                        break
            storyboard.insert(insert_at, shot)
            for index, item in enumerate(storyboard, start=1):
                item["shot"] = index
            self._normalize_project(
                current,
                self._project_request(current),
            )
            self._mark_video_production_stale(
                current,
                "新增分镜后，视频任务需要按最新分镜重新生成",
            )

        updated = self.store.mutate(project_id, add)
        if not updated:
            raise AgentError("项目不存在。")
        return updated

    def delete_shot(self, project_id: str, shot_id: str) -> dict[str, Any]:
        def remove(project: dict[str, Any]) -> None:
            self._assert_project_editable(project)
            storyboard = project.get("storyboard", [])
            if not any(
                str(item.get("id") or "") == shot_id
                for item in storyboard
                if isinstance(item, dict)
            ):
                raise AgentError("分镜不存在。")
            project["storyboard"] = [
                item
                for item in storyboard
                if str(item.get("id") or "") != shot_id
            ]
            for index, item in enumerate(project["storyboard"], start=1):
                item["shot"] = index
            self._normalize_project(
                project,
                self._project_request(project),
            )
            self._mark_video_production_stale(
                project,
                "删除分镜后，视频任务需要按最新分镜重新生成",
            )

        updated = self.store.mutate(project_id, remove)
        if not updated:
            raise AgentError("项目不存在。")
        return updated

    def _apply_chat_operations(
        self,
        project: dict[str, Any],
        operations: list[dict[str, Any]],
    ) -> int:
        applied = 0
        video_affecting = any(
            isinstance(operation, dict)
            and str(operation.get("action") or "")
            in {
                "update_character",
                "delete_character",
                "add_scene",
                "update_scene",
                "delete_scene",
                "add_shot",
                "update_shot",
                "delete_shot",
            }
            for operation in operations
        )
        character_fields = {
            "name",
            "role",
            "visualIdentity",
            "personality",
            "voice",
            "imagePrompt",
        }
        scene_fields = {"name", "imagePrompt"}
        shot_fields = {
            "scene",
            "duration",
            "action",
            "camera",
            "visualPrompt",
            "videoPrompt",
            "dialogue",
            "audio",
            "continuity",
            "characterIds",
        }
        script_fields = {"logline", "synopsis", "narration", "beats"}
        for operation in operations[:20]:
            if not isinstance(operation, dict):
                continue
            action = str(operation.get("action") or "")
            if action == "update_script":
                fields = operation.get("fields") or {}
                if not isinstance(fields, dict):
                    continue
                script = project.setdefault("script", {})
                for field, value in fields.items():
                    if field not in script_fields:
                        continue
                    if field == "beats":
                        if isinstance(value, list):
                            script[field] = value[:30]
                    else:
                        script[field] = str(value or "").strip()[:8_000]
                applied += 1
            elif action == "add_character":
                value = operation.get("character") or {}
                if isinstance(value, dict):
                    project.setdefault("characters", []).append(
                        self._new_character(value)
                    )
                    applied += 1
            elif action == "update_character":
                fields = operation.get("fields") or {}
                if not isinstance(fields, dict):
                    continue
                character = self._find_item(
                    project.get("characters"),
                    str(operation.get("id") or ""),
                    "角色",
                )
                for field, value in fields.items():
                    if field in character_fields:
                        character[field] = str(value or "").strip()[:8_000]
                applied += 1
            elif action == "delete_character":
                character_id = str(operation.get("id") or "")
                if not any(
                    str(item.get("id") or "") == character_id
                    for item in project.get("characters", [])
                    if isinstance(item, dict)
                ):
                    raise AgentError("模型指定删除的角色不存在。")
                project["characters"] = [
                    item
                    for item in project.get("characters", [])
                    if str(item.get("id") or "") != character_id
                ]
                for shot in project.get("storyboard", []):
                    shot["characterIds"] = [
                        value
                        for value in shot.get("characterIds", [])
                        if str(value) != character_id
                    ]
                applied += 1
            elif action == "add_scene":
                value = operation.get("scene") or {}
                if isinstance(value, dict):
                    project.setdefault("scenes", []).append(
                        self._new_scene(value)
                    )
                    applied += 1
            elif action == "update_scene":
                fields = operation.get("fields") or {}
                if not isinstance(fields, dict):
                    continue
                scene = self._find_item(
                    project.get("scenes"),
                    str(operation.get("id") or ""),
                    "场景",
                )
                previous_name = str(scene.get("name") or "")
                for field, value in fields.items():
                    if field in scene_fields:
                        scene[field] = str(value or "").strip()[:4_000]
                if scene.get("name") != previous_name:
                    for shot in project.get("storyboard", []):
                        if str(shot.get("sceneId") or "") == str(scene.get("id")):
                            shot["scene"] = scene["name"]
                applied += 1
            elif action == "delete_scene":
                scene_id = str(operation.get("id") or "")
                scene = self._find_item(
                    project.get("scenes"),
                    scene_id,
                    "场景",
                )
                if any(
                    str(shot.get("sceneId") or "") == scene_id
                    or str(shot.get("scene") or "")
                    == str(scene.get("name") or "")
                    for shot in project.get("storyboard", [])
                    if isinstance(shot, dict)
                ):
                    raise AgentError(
                        "模型建议删除的场景仍被分镜使用，请先让模型修改相关分镜。"
                    )
                project["scenes"] = [
                    item
                    for item in project.get("scenes", [])
                    if str(item.get("id") or "") != scene_id
                ]
                applied += 1
            elif action == "add_shot":
                value = operation.get("shot") or {}
                if not isinstance(value, dict):
                    continue
                value = copy.deepcopy(value)
                name_to_id = {
                    str(item.get("name") or ""): str(item.get("id") or "")
                    for item in project.get("characters", [])
                    if isinstance(item, dict)
                }
                if isinstance(value.get("characterIds"), list):
                    value["characterIds"] = [
                        name_to_id.get(str(item), str(item))
                        for item in value["characterIds"]
                    ]
                valid_ids = {
                    str(item.get("id"))
                    for item in project.get("characters", [])
                    if isinstance(item, dict)
                }
                shot = self._new_shot(value, valid_ids)
                storyboard = project.setdefault("storyboard", [])
                after_id = str(operation.get("afterShotId") or "")
                insert_at = len(storyboard)
                for index, item in enumerate(storyboard):
                    if str(item.get("id") or "") == after_id:
                        insert_at = index + 1
                        break
                storyboard.insert(insert_at, shot)
                applied += 1
            elif action == "update_shot":
                fields = operation.get("fields") or {}
                if not isinstance(fields, dict):
                    continue
                shot = self._find_item(
                    project.get("storyboard"),
                    str(operation.get("id") or ""),
                    "分镜",
                )
                valid_ids = {
                    str(item.get("id"))
                    for item in project.get("characters", [])
                    if isinstance(item, dict)
                }
                for field, value in fields.items():
                    if field not in shot_fields:
                        continue
                    if field == "duration":
                        try:
                            duration = int(value)
                        except (TypeError, ValueError):
                            continue
                        shot[field] = max(1, min(180, duration))
                    elif field == "characterIds" and isinstance(value, list):
                        shot[field] = [
                            str(item)
                            for item in value
                            if str(item) in valid_ids
                        ]
                    else:
                        shot[field] = str(value or "").strip()[:8_000]
                applied += 1
            elif action == "delete_shot":
                shot_id = str(operation.get("id") or "")
                if not any(
                    str(item.get("id") or "") == shot_id
                    for item in project.get("storyboard", [])
                    if isinstance(item, dict)
                ):
                    raise AgentError("模型指定删除的分镜不存在。")
                project["storyboard"] = [
                    item
                    for item in project.get("storyboard", [])
                    if str(item.get("id") or "") != shot_id
                ]
                applied += 1
        for index, shot in enumerate(project.get("storyboard", []), start=1):
            shot["shot"] = index
        self._normalize_project(project, self._project_request(project))
        if applied:
            self._mark_complete_video_prompts_stale(project)
        if video_affecting and applied:
            self._mark_video_production_stale(
                project,
                "对话编辑修改了角色或分镜",
            )
        return applied

    @staticmethod
    def _chat_content_fingerprint(project: dict[str, Any]) -> str:
        content = {
            "title": project.get("title"),
            "brief": project.get("brief"),
            "script": project.get("script"),
            "characters": project.get("characters"),
            "scenes": project.get("scenes"),
            "storyboard": project.get("storyboard"),
            "stagePrompts": project.get("stagePrompts"),
            "deliverables": project.get("deliverables"),
        }
        serialized = json.dumps(
            content,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _chat_proposal_preview(
        self,
        project: dict[str, Any],
        operations: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        previews: list[dict[str, str]] = []
        script_labels = {
            "logline": "一句话故事",
            "synopsis": "故事梗概",
            "narration": "旁白 / 台词",
            "beats": "叙事节拍",
        }
        character_labels = {
            "name": "名称",
            "role": "角色功能",
            "visualIdentity": "视觉锚点",
            "personality": "性格",
            "voice": "声音",
            "imagePrompt": "角色图片提示词",
        }
        scene_labels = {
            "name": "名称",
            "imagePrompt": "场景图片提示词",
        }
        shot_labels = {
            "scene": "场景",
            "duration": "时长",
            "action": "动作",
            "camera": "机位与运镜",
            "visualPrompt": "关键帧提示词",
            "videoPrompt": "视频提示词",
            "dialogue": "台词 / 旁白",
            "audio": "声音",
            "continuity": "连续性",
            "characterIds": "出镜角色",
        }

        def text(value: Any) -> str:
            if isinstance(value, (dict, list)):
                result = json.dumps(value, ensure_ascii=False)
            else:
                result = str(value or "")
            return result[:2_000]

        for operation in operations[:20]:
            if not isinstance(operation, dict):
                continue
            action = str(operation.get("action") or "")
            if action == "update_script":
                fields = operation.get("fields") or {}
                if not isinstance(fields, dict):
                    continue
                for field, after in fields.items():
                    if field not in script_labels:
                        continue
                    previews.append(
                        {
                            "action": action,
                            "title": f"修改剧本 · {script_labels[field]}",
                            "before": text(
                                (project.get("script") or {}).get(field)
                            ),
                            "after": text(after),
                        }
                    )
            elif action == "add_character":
                character = operation.get("character") or {}
                previews.append(
                    {
                        "action": action,
                        "title": (
                            "新增角色 · "
                            + str(character.get("name") or "未命名角色")
                        ),
                        "before": "当前项目中不存在该角色",
                        "after": text(character),
                    }
                )
            elif action in {"update_character", "delete_character"}:
                character = self._find_item(
                    project.get("characters"),
                    str(operation.get("id") or ""),
                    "角色",
                )
                if action == "delete_character":
                    previews.append(
                        {
                            "action": action,
                            "title": f"删除角色 · {character.get('name')}",
                            "before": text(character),
                            "after": "删除角色并解除其分镜出场绑定",
                        }
                    )
                    continue
                fields = operation.get("fields") or {}
                if not isinstance(fields, dict):
                    continue
                for field, after in fields.items():
                    if field not in character_labels:
                        continue
                    previews.append(
                        {
                            "action": action,
                            "title": (
                                f"修改角色「{character.get('name')}」"
                                f" · {character_labels[field]}"
                            ),
                            "before": text(character.get(field)),
                            "after": text(after),
                        }
                    )
            elif action == "add_scene":
                scene = operation.get("scene") or {}
                previews.append(
                    {
                        "action": action,
                        "title": (
                            "新增场景 · "
                            + str(scene.get("name") or "未命名场景")
                        ),
                        "before": "当前项目中不存在该场景",
                        "after": text(scene),
                    }
                )
            elif action in {"update_scene", "delete_scene"}:
                scene = self._find_item(
                    project.get("scenes"),
                    str(operation.get("id") or ""),
                    "场景",
                )
                if action == "delete_scene":
                    previews.append(
                        {
                            "action": action,
                            "title": f"删除场景 · {scene.get('name')}",
                            "before": text(scene),
                            "after": "删除场景及其项目资产绑定",
                        }
                    )
                    continue
                fields = operation.get("fields") or {}
                if not isinstance(fields, dict):
                    continue
                for field, after in fields.items():
                    if field not in scene_labels:
                        continue
                    previews.append(
                        {
                            "action": action,
                            "title": (
                                f"修改场景「{scene.get('name')}」"
                                f" · {scene_labels[field]}"
                            ),
                            "before": text(scene.get(field)),
                            "after": text(after),
                        }
                    )
            elif action == "add_shot":
                shot = operation.get("shot") or {}
                previews.append(
                    {
                        "action": action,
                        "title": f"新增分镜 · {shot.get('scene') or '新场景'}",
                        "before": "当前分镜列表中不存在该镜头",
                        "after": text(shot),
                    }
                )
            elif action in {"update_shot", "delete_shot"}:
                shot = self._find_item(
                    project.get("storyboard"),
                    str(operation.get("id") or ""),
                    "分镜",
                )
                if action == "delete_shot":
                    previews.append(
                        {
                            "action": action,
                            "title": f"删除分镜 {shot.get('shot')}",
                            "before": text(shot),
                            "after": "删除后自动重排分镜编号",
                        }
                    )
                    continue
                fields = operation.get("fields") or {}
                if not isinstance(fields, dict):
                    continue
                for field, after in fields.items():
                    if field not in shot_labels:
                        continue
                    previews.append(
                        {
                            "action": action,
                            "title": (
                                f"修改分镜 {shot.get('shot')}"
                                f" · {shot_labels[field]}"
                            ),
                            "before": text(shot.get(field)),
                            "after": text(after),
                        }
                    )
        return previews[:50]

    def chat_edit_project(
        self,
        project_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        message = str(payload.get("message") or "").strip()
        if not message or len(message) > 8_000:
            raise AgentError("对话内容不能为空且不能超过 8000 字。")
        project = self.prepare_project(project_id)
        if not project:
            raise AgentError("项目不存在。")
        self._assert_project_editable(project)
        history = [
            {
                "role": str(item.get("role") or ""),
                "content": str(item.get("content") or ""),
            }
            for item in project.get("chatHistory", [])
            if isinstance(item, dict)
            and item.get("role") in {"user", "assistant"}
        ]
        context = {
            "title": project.get("title"),
            "brief": project.get("brief"),
            "script": project.get("script"),
            "characters": project.get("characters"),
            "scenes": project.get("scenes"),
            "storyboard": project.get("storyboard"),
            "stagePrompts": project.get("stagePrompts"),
            "deliverables": project.get("deliverables"),
            "pendingProposal": project.get("pendingChatProposal"),
        }
        proposal = self.provider.edit_project(context, history, message)
        reply = str(
            proposal.get("reply") or "我已整理出修改建议，请确认。"
        ).strip()[:4_000]
        operations = proposal.get("operations", [])
        if not isinstance(operations, list):
            raise AgentError("模型返回的项目修改操作格式无效。")
        operations = [
            copy.deepcopy(item)
            for item in operations[:20]
            if isinstance(item, dict)
        ]
        validated_project = copy.deepcopy(project)
        proposed_count = self._apply_chat_operations(
            validated_project,
            operations,
        )
        preview = self._chat_proposal_preview(project, operations)
        pending_proposal = (
            {
                "id": str(uuid.uuid4()),
                "reply": reply,
                "operations": operations,
                "preview": preview,
                "operationCount": proposed_count,
                "baseFingerprint": self._chat_content_fingerprint(project),
                "createdAt": utc_now(),
            }
            if proposed_count
            else project.get("pendingChatProposal")
        )

        def save_proposal(project_value: dict[str, Any]) -> None:
            self._assert_project_editable(project_value)
            chat_history = project_value.setdefault("chatHistory", [])
            chat_history.extend(
                [
                    {
                        "role": "user",
                        "content": message,
                        "createdAt": utc_now(),
                    },
                    {
                        "role": "assistant",
                        "content": reply,
                        "createdAt": utc_now(),
                        "proposedOperations": proposed_count,
                    },
                ]
            )
            project_value["chatHistory"] = chat_history[-40:]
            project_value["pendingChatProposal"] = pending_proposal

        updated = self.store.mutate(project_id, save_proposal)
        if not updated:
            raise AgentError("项目不存在。")
        return {
            "reply": reply,
            "proposedOperations": proposed_count,
            "project": updated,
        }

    def resolve_chat_proposal(
        self,
        project_id: str,
        payload: dict[str, Any],
        *,
        accept: bool,
    ) -> dict[str, Any]:
        proposal_id = str(payload.get("proposalId") or "")
        applied_count = 0

        def resolve(project: dict[str, Any]) -> None:
            nonlocal applied_count
            self._assert_project_editable(project)
            proposal = project.get("pendingChatProposal")
            if not isinstance(proposal, dict):
                raise AgentError("当前没有等待确认的修改建议。")
            if proposal_id and str(proposal.get("id") or "") != proposal_id:
                raise AgentError("修改建议已经更新，请刷新后重新确认。")
            if accept:
                if (
                    str(proposal.get("baseFingerprint") or "")
                    != self._chat_content_fingerprint(project)
                ):
                    raise AgentError(
                        "项目内容在建议生成后发生了变化，请重新与模型对话。"
                    )
                operations = proposal.get("operations", [])
                if not isinstance(operations, list):
                    raise AgentError("待确认修改建议格式无效。")
                applied_count = self._apply_chat_operations(
                    project,
                    operations,
                )
                content = f"已按你的确认应用 {applied_count} 项修改。"
            else:
                content = "已放弃本轮修改建议，项目内容保持不变。"
            project["pendingChatProposal"] = None
            chat_history = project.setdefault("chatHistory", [])
            chat_history.append(
                {
                    "role": "assistant",
                    "content": content,
                    "createdAt": utc_now(),
                    "appliedOperations": applied_count,
                }
            )
            project["chatHistory"] = chat_history[-40:]

        updated = self.store.mutate(project_id, resolve)
        if not updated:
            raise AgentError("项目不存在。")
        return {
            "appliedOperations": applied_count,
            "accepted": accept,
            "project": updated,
        }

    def describe_character_from_asset(
        self,
        project_id: str,
        character_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        project = self.prepare_project(project_id)
        if not project:
            raise AgentError("项目不存在。")
        self._assert_project_editable(project)
        character = self._find_item(
            project.get("characters"),
            character_id,
            "角色",
        )
        asset_id = str(payload.get("assetId") or "").strip()
        asset = self._find_item(project.get("assets"), asset_id, "参考图片")
        if asset.get("type") != "image":
            raise AgentError("只能使用图片资产生成角色描述。")
        if (
            str(asset.get("ownerType") or "") != "character"
            or str(asset.get("ownerId") or "") != character_id
        ):
            raise AgentError("请选择当前角色自己的参考图片。")
        content, mime_type = self._load_local_asset_image(project_id, asset)
        image_data_uri = (
            f"data:{mime_type};base64,"
            + base64.b64encode(content).decode("ascii")
        )
        description = self.provider.describe_character(
            image_data_uri,
            str(character.get("name") or ""),
        )
        allowed_fields = {
            "visualIdentity",
            "personality",
            "role",
            "voice",
            "imagePrompt",
        }

        def update(project_value: dict[str, Any]) -> None:
            self._assert_project_editable(project_value)
            target = self._find_item(
                project_value.get("characters"),
                character_id,
                "角色",
            )
            for field in allowed_fields:
                value = description.get(field)
                if value:
                    target[field] = str(value).strip()[:8_000]
            target["descriptionSourceAssetId"] = asset_id
            self._normalize_project(
                project_value,
                self._project_request(project_value),
            )
            self._mark_complete_video_prompts_stale(project_value)
            self._mark_video_production_stale(
                project_value,
                "角色设定已根据参考图更新",
            )

        updated = self.store.mutate(project_id, update)
        if not updated:
            raise AgentError("项目不存在。")
        return updated

    @staticmethod
    def _image_extension(content: bytes) -> tuple[str, str]:
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png", "image/png"
        if content.startswith(b"\xff\xd8\xff"):
            return ".jpg", "image/jpeg"
        if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
            return ".webp", "image/webp"
        raise AgentError("仅支持 PNG、JPEG 或 WebP 图片。")

    def _decode_generated_image(self, result: dict[str, Any]) -> bytes:
        if result.get("type") == "base64":
            try:
                content = base64.b64decode(
                    str(result.get("value") or ""),
                    validate=True,
                )
            except (ValueError, binascii.Error) as exc:
                raise AgentError("图像模型返回的 Base64 无效。") from exc
        elif result.get("type") == "url":
            url = str(result.get("value") or "")
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme != "https":
                raise AgentError("图像模型返回了非 HTTPS 地址。")
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "Jingzhou-Agent/0.3"},
            )
            try:
                with urllib.request.urlopen(
                    request,
                    timeout=self.settings.timeout,
                ) as response:
                    content = response.read(MAX_IMAGE_BYTES + 1)
            except (urllib.error.URLError, TimeoutError) as exc:
                raise AgentError(f"无法下载图像模型结果：{exc}") from exc
        else:
            raise AgentError("图像模型没有返回可保存的图片。")
        if not content or len(content) > MAX_IMAGE_BYTES:
            raise AgentError("图片为空或超过 30 MB。")
        return content

    def _save_asset(
        self,
        project_id: str,
        owner_type: str,
        owner_id: str,
        prompt: str,
        content: bytes,
        source: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.media_root:
            raise AgentError("本地素材目录尚未配置。")
        extension, mime_type = self._image_extension(content)
        asset_id = f"asset-{uuid.uuid4().hex[:12]}"
        directory = (self.media_root / project_id / "assets").resolve()
        root = self.media_root.resolve()
        if root not in directory.parents:
            raise AgentError("素材保存路径无效。")
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f"{asset_id}{extension}"
        destination.write_bytes(content)
        asset = {
            "id": asset_id,
            "type": "image",
            "ownerType": owner_type,
            "ownerId": owner_id,
            "source": source,
            "prompt": prompt,
            "url": f"/media/{project_id}/assets/{destination.name}",
            "mimeType": mime_type,
            "createdAt": utc_now(),
            **(metadata or {}),
        }

        def mutate(project: dict[str, Any]) -> None:
            collection = (
                project.get("characters")
                if owner_type == "character"
                else project.get("scenes")
            )
            owner = self._find_item(
                collection,
                owner_id,
                "角色" if owner_type == "character" else "场景",
            )
            reference_ids = owner.setdefault("referenceImageIds", [])
            parent_asset_id = str(asset.get("parentAssetId") or "")
            if parent_asset_id:
                reference_ids[:] = [
                    value
                    for value in reference_ids
                    if str(value) != parent_asset_id
                ]
                reference_ids.insert(0, asset_id)
                for shot in project.get("storyboard", []):
                    if not isinstance(shot, dict):
                        continue
                    shot["referenceAssetIds"] = [
                        asset_id
                        if str(value) == parent_asset_id
                        else value
                        for value in shot.get("referenceAssetIds", [])
                    ]
                    for field in (
                        "startFrameAssetId",
                        "endFrameAssetId",
                    ):
                        if str(shot.get(field) or "") == parent_asset_id:
                            shot[field] = asset_id
            else:
                reference_ids.append(asset_id)
            project.setdefault("assets", []).append(asset)
            self._mark_complete_video_prompts_stale(project)

        try:
            updated = self.store.mutate(project_id, mutate)
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        if not updated:
            destination.unlink(missing_ok=True)
            raise AgentError("项目不存在。")
        return asset

    def _load_local_asset_image(
        self,
        project_id: str,
        asset: dict[str, Any],
    ) -> tuple[bytes, str]:
        media_root = self.media_root.resolve() if self.media_root else None
        relative_url = str(asset.get("url") or "")
        prefix = f"/media/{project_id}/"
        if not media_root or not relative_url.startswith(prefix):
            raise AgentError("参考图片不是可读取的本地素材。")
        source_path = (
            self.media_root
            / project_id
            / relative_url.removeprefix(prefix)
        ).resolve()
        project_media_root = (self.media_root / project_id).resolve()
        if (
            project_media_root not in source_path.parents
            or not source_path.is_file()
        ):
            raise AgentError("找不到选中的参考图片。")
        content = source_path.read_bytes()
        if not content or len(content) > MAX_IMAGE_BYTES:
            raise AgentError("参考图片为空或超过 30 MB。")
        _extension, mime_type = self._image_extension(content)
        return content, mime_type

    def _generation_reference_assets(
        self,
        project: dict[str, Any],
        owner_type: str,
        owner_id: str,
        explicit_ids: list[str],
        use_global_references: bool,
    ) -> list[dict[str, Any]]:
        assets = [
            asset
            for asset in project.get("assets", [])
            if isinstance(asset, dict) and asset.get("type") == "image"
        ]
        asset_by_id = {
            str(asset.get("id")): asset
            for asset in assets
            if asset.get("id")
        }
        selected_ids: list[str] = []

        def select(asset_id: Any) -> None:
            value = str(asset_id or "").strip()
            if value and value not in selected_ids:
                selected_ids.append(value)

        for asset_id in explicit_ids:
            if asset_id not in asset_by_id:
                raise AgentError("选择的参考图片不存在。")
            select(asset_id)

        if use_global_references:
            owner_collection = (
                project.get("characters")
                if owner_type == "character"
                else project.get("scenes")
            )
            owner = self._find_item(
                owner_collection,
                owner_id,
                "角色" if owner_type == "character" else "场景",
            )
            owner_references = owner.get("referenceImageIds", [])
            if owner_references:
                select(owner_references[0])
            for collection_name in ("characters", "scenes"):
                for item in project.get(collection_name, []):
                    if not isinstance(item, dict):
                        continue
                    references = item.get("referenceImageIds", [])
                    if references:
                        select(references[0])

        return [asset_by_id[asset_id] for asset_id in selected_ids[:16]]

    def generate_project_asset(
        self,
        project_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.settings.image_enabled:
            raise AgentError("尚未配置图像模型。")
        owner_type = str(payload.get("ownerType", ""))
        owner_id = str(payload.get("ownerId", ""))
        if owner_type not in {"character", "scene"}:
            raise AgentError("素材类型无效。")
        project = self.prepare_project(project_id)
        if not project:
            raise AgentError("项目不存在。")
        owner = self._find_item(
            (
                project.get("characters")
                if owner_type == "character"
                else project.get("scenes")
            ),
            owner_id,
            "角色" if owner_type == "character" else "场景",
        )
        prompt = str(
            payload.get("prompt") or owner.get("imagePrompt") or ""
        ).strip()
        stage_prompt = str(
            (project.get("stagePrompts") or {}).get("characters", "")
        ).strip()
        effective_prompt = "\n".join(
            value for value in (stage_prompt, prompt) if value
        )
        size = str(payload.get("size", "1024x1024"))
        raw_reference_ids = payload.get("referenceAssetIds", [])
        if not isinstance(raw_reference_ids, list):
            raise AgentError("参考图片列表格式无效。")
        reference_ids = [
            str(value).strip()
            for value in raw_reference_ids
            if str(value).strip()
        ]
        if len(reference_ids) > 16:
            raise AgentError("最多选择 16 张参考图片。")
        use_global_references = payload.get("useGlobalReferences", True) is not False
        reference_assets = self._generation_reference_assets(
            project,
            owner_type,
            owner_id,
            reference_ids,
            use_global_references,
        )
        if reference_assets:
            if not self.settings.image_edit_enabled:
                raise AgentError(
                    "全局一致性或手动参考图需要配置图片编辑模型。"
                )
            explicit_reference_ids = set(reference_ids)
            character_names = {
                str(item.get("id")): str(item.get("name") or "未命名角色")
                for item in project.get("characters", [])
                if isinstance(item, dict)
            }
            scene_names = {
                str(item.get("id")): str(item.get("name") or "未命名场景")
                for item in project.get("scenes", [])
                if isinstance(item, dict)
            }
            reference_manifest = []
            required_characters = []
            required_scenes = []
            for index, asset in enumerate(reference_assets, start=1):
                asset_id = str(asset.get("id") or "")
                is_explicit = asset_id in explicit_reference_ids
                asset_owner_type = str(asset.get("ownerType") or "")
                asset_owner_id = str(asset.get("ownerId") or "")
                if asset_owner_type == "character":
                    name = character_names.get(asset_owner_id, "未命名角色")
                    if is_explicit and name not in required_characters:
                        required_characters.append(name)
                    purpose = (
                        "手动指定，必须使用该人物身份并让该角色出现在最终画面中"
                        if is_explicit
                        else "全局身份参考，仅在目标画面涉及该角色时使用"
                    )
                    reference_manifest.append(
                        f"参考图 {index}：角色「{name}」的身份参考；{purpose}。"
                    )
                elif asset_owner_type == "scene":
                    name = scene_names.get(asset_owner_id, "未命名场景")
                    if is_explicit and name not in required_scenes:
                        required_scenes.append(name)
                    purpose = (
                        "手动指定，必须采用其空间、材质与光线设计"
                        if is_explicit
                        else "全局场景参考，仅在目标画面涉及该场景时使用"
                    )
                    reference_manifest.append(
                        f"参考图 {index}：场景「{name}」的视觉参考；{purpose}。"
                    )
                else:
                    reference_manifest.append(
                        f"参考图 {index}：项目视觉参考。"
                    )
            binding_rules = [
                "输入图片顺序与上述参考图序号严格一一对应。",
                "保持相关角色的脸型、五官、发型、体态、服装识别锚点一致，"
                "不同角色不能融合、换脸、互换服装或遗漏。",
                "同一角色即使有多张参考图也只表示同一个人，不得复制成多人。",
            ]
            if required_characters:
                binding_rules.append(
                    "最终画面必须同时包含这些手动指定角色，且每个角色只出现一次："
                    + "、".join(f"「{name}」" for name in required_characters)
                    + "。"
                )
            if required_scenes:
                binding_rules.append(
                    "最终画面的背景必须采用这些手动指定场景的设计："
                    + "、".join(f"「{name}」" for name in required_scenes)
                    + "。"
                )
            consistency_prompt = "\n".join(
                [
                    "把输入图片作为同一项目的角色身份和场景设计参考。",
                    *reference_manifest,
                    *binding_rules,
                ]
            )
            effective_prompt = f"{consistency_prompt}\n目标图片：{effective_prompt}"
            source_images = [
                self._load_local_asset_image(project_id, asset)
                for asset in reference_assets
            ]
            result = self.provider.edit_images(
                effective_prompt,
                source_images,
                size=size,
            )
            source = "reference-generated"
            metadata = {
                "referenceAssetIds": [
                    str(asset.get("id")) for asset in reference_assets
                ],
                "explicitReferenceAssetIds": reference_ids,
                "requiredCharacters": required_characters,
                "globalConsistency": use_global_references,
            }
        else:
            result = self.generate_image(
                {"prompt": effective_prompt, "size": size}
            )
            source = "generated"
            metadata = {
                "referenceAssetIds": [],
                "globalConsistency": use_global_references,
            }
        content = self._decode_generated_image(result)
        return self._save_asset(
            project_id,
            owner_type,
            owner_id,
            effective_prompt,
            content,
            source,
            metadata,
        )

    def upload_project_asset(
        self,
        project_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        owner_type = str(payload.get("ownerType", ""))
        owner_id = str(payload.get("ownerId", ""))
        if owner_type not in {"character", "scene"}:
            raise AgentError("素材类型无效。")
        data_url = str(payload.get("dataUrl", ""))
        header, separator, encoded = data_url.partition(",")
        if not separator or ";base64" not in header:
            raise AgentError("上传图片格式无效。")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise AgentError("上传图片 Base64 无效。") from exc
        if not content or len(content) > MAX_IMAGE_BYTES:
            raise AgentError("上传图片为空或超过 30 MB。")
        project = self.prepare_project(project_id)
        if not project:
            raise AgentError("项目不存在。")
        return self._save_asset(
            project_id,
            owner_type,
            owner_id,
            str(payload.get("prompt") or ""),
            content,
            "uploaded",
        )

    def edit_project_asset(
        self,
        project_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.settings.image_edit_enabled:
            raise AgentError("尚未配置图片编辑模型。")
        asset_id = str(payload.get("assetId") or "").strip()
        prompt = str(payload.get("prompt") or "").strip()
        if not asset_id:
            raise AgentError("请选择需要修改的图片。")
        if not prompt or len(prompt) > 8_000:
            raise AgentError("修改提示词不能为空且不能超过 8000 字。")
        size = str(payload.get("size") or "1024x1024")
        if size not in {"1024x1024", "1536x1024", "1024x1536"}:
            raise AgentError("不支持该图片尺寸。")
        project = self.prepare_project(project_id)
        if not project:
            raise AgentError("项目不存在。")
        asset = self._find_item(
            project.get("assets"),
            asset_id,
            "参考图片",
        )
        owner_type = str(asset.get("ownerType") or "")
        owner_id = str(asset.get("ownerId") or "")
        if owner_type not in {"character", "scene"}:
            raise AgentError("仅支持修改角色或场景图片。")
        content, mime_type = self._load_local_asset_image(project_id, asset)
        preservation = (
            "保持人物身份、脸型、五官比例、发型和未被点名的服装配饰不变。"
            if owner_type == "character"
            else "保持未被点名的空间布局、主体位置、透视关系和场景风格不变。"
        )
        effective_prompt = (
            "严格基于输入图片进行局部修改，只改变用户明确要求的内容。"
            f"{preservation}\n修改要求：{prompt}"
        )
        result = self.provider.edit_image(
            effective_prompt,
            content,
            mime_type,
            size=size,
        )
        edited_content = self._decode_generated_image(result)
        return self._save_asset(
            project_id,
            owner_type,
            owner_id,
            effective_prompt,
            edited_content,
            "edited",
            {
                "parentAssetId": asset_id,
                "editPrompt": prompt,
            },
        )

    def delete_project_asset(
        self,
        project_id: str,
        asset_id: str,
    ) -> None:
        project = self.prepare_project(project_id)
        if not project:
            raise AgentError("项目不存在。")
        asset = self._find_item(
            project.get("assets"),
            str(asset_id or "").strip(),
            "图片资产",
        )
        asset_url = str(asset.get("url") or "")
        destination: Path | None = None
        if self.media_root and asset_url.startswith(f"/media/{project_id}/"):
            candidate = (
                self.media_root
                / project_id
                / asset_url.removeprefix(f"/media/{project_id}/")
            ).resolve()
            project_root = (self.media_root / project_id).resolve()
            if project_root in candidate.parents:
                destination = candidate

        def remove_asset(current: dict[str, Any]) -> None:
            current["assets"] = [
                item
                for item in current.get("assets", [])
                if str(item.get("id") or "") != asset_id
            ]
            for collection_name in ("characters", "scenes"):
                for owner in current.get(collection_name, []):
                    if not isinstance(owner, dict):
                        continue
                    owner["referenceImageIds"] = [
                        value
                        for value in owner.get("referenceImageIds", [])
                        if str(value) != asset_id
                    ]
            for shot in current.get("storyboard", []):
                if not isinstance(shot, dict):
                    continue
                shot["referenceAssetIds"] = [
                    value
                    for value in shot.get("referenceAssetIds", [])
                    if str(value) != asset_id
                ]
                if str(shot.get("startFrameAssetId") or "") == asset_id:
                    shot["startFrameAssetId"] = ""
                if str(shot.get("endFrameAssetId") or "") == asset_id:
                    shot["endFrameAssetId"] = ""
            production = current.get("videoProduction") or {}
            for job in production.get("jobs", []):
                if not isinstance(job, dict):
                    continue
                job["referenceAssetIds"] = [
                    value
                    for value in job.get("referenceAssetIds", [])
                    if str(value) != asset_id
                ]
                job["referenceImageAssetIds"] = [
                    value
                    for value in job.get("referenceImageAssetIds", [])
                    if str(value) != asset_id
                ]
                job["referenceImageUrls"] = [
                    value
                    for value in job.get("referenceImageUrls", [])
                    if str(value) != asset_url
                ]
                if str(job.get("startFrameAssetId") or "") == asset_id:
                    job["startFrameAssetId"] = ""
                    job["startFrameUrl"] = ""
                if str(job.get("endFrameAssetId") or "") == asset_id:
                    job["endFrameAssetId"] = ""
                    job["endFrameUrl"] = ""
                if str(job.get("inputReferenceAssetId") or "") == asset_id:
                    job["inputReferenceAssetId"] = ""
                    job["inputReferenceAssetDeleted"] = True

        updated = self.store.mutate(project_id, remove_asset)
        if not updated:
            raise AgentError("项目不存在。")
        if destination and destination.is_file():
            destination.unlink()

    def delete_project(self, project_id: str) -> None:
        if not self.store.delete(project_id):
            raise AgentError("项目不存在。")
        if not self.media_root:
            return
        directory = (self.media_root / project_id).resolve()
        root = self.media_root.resolve()
        if root not in directory.parents:
            raise AgentError("项目素材路径无效。")
        if directory.is_dir():
            shutil.rmtree(directory)
