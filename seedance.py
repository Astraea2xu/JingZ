from __future__ import annotations

import base64
import copy
import http.client
import json
import math
import os
import shutil
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from agent import AgentError, MAX_VIDEO_REFERENCE_IMAGES, ProjectStore


FINAL_TASK_STATUSES = {"succeeded", "failed", "expired", "cancelled"}
ACTIVE_PRODUCTION_STATUSES = {"queued", "running"}
ALLOWED_RESOLUTIONS = {"480p", "720p", "1080p"}
ALLOWED_RATIOS = {"16:9", "4:3", "1:1", "3:4", "9:16", "21:9", "adaptive"}
MAX_PROMPT_CHARS = 6_000
MAX_API_LOG_CHARS = 8_000
SENSITIVE_LOG_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "access_token",
    "refresh_token",
    "secret",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_error_message(error: BaseException) -> str:
    text = str(error).strip() or type(error).__name__
    return text[:1_000]


@dataclass
class SeedanceSettings:
    base_url: str
    api_key: str
    model: str
    poll_interval: float
    request_timeout: int
    download_max_bytes: int
    ffmpeg_path: str
    download_retries: int = 4
    request_retries: int = 4
    content_base_url: str = ""

    @classmethod
    def from_environment(cls) -> "SeedanceSettings":
        configured_ffmpeg = os.getenv("FFMPEG_PATH", "").strip()
        detected_ffmpeg = configured_ffmpeg or shutil.which("ffmpeg") or ""
        return cls(
            base_url=(
                os.getenv("VIDEO_API_BASE_URL", "").strip()
                or os.getenv("SEEDANCE_BASE_URL", "").strip()
                or "https://api.177911.com/v1/video/generations"
            ),
            api_key=(
                os.getenv("VIDEO_API_KEY", "").strip()
                or os.getenv("SEEDANCE_API_KEY", "").strip()
                or os.getenv("ARK_API_KEY", "").strip()
            ),
            model=(
                os.getenv("VIDEO_MODEL", "").strip()
                or os.getenv("SEEDANCE_MODEL", "").strip()
                or "doubao-seedance-2-0-260128"
            ),
            poll_interval=max(
                1.0,
                float(
                    os.getenv("VIDEO_POLL_INTERVAL", "").strip()
                    or os.getenv("SEEDANCE_POLL_INTERVAL", "10")
                ),
            ),
            request_timeout=max(
                10,
                int(
                    os.getenv("VIDEO_API_TIMEOUT", "").strip()
                    or os.getenv("SEEDANCE_TIMEOUT", "120")
                ),
            ),
            download_max_bytes=max(
                1,
                int(
                    os.getenv("VIDEO_DOWNLOAD_MAX_MB", "").strip()
                    or os.getenv("SEEDANCE_DOWNLOAD_MAX_MB", "600")
                ),
            )
            * 1024
            * 1024,
            ffmpeg_path=detected_ffmpeg,
            download_retries=max(
                1,
                int(os.getenv("VIDEO_DOWNLOAD_RETRIES", "4")),
            ),
            request_retries=max(
                1,
                int(os.getenv("VIDEO_API_RETRIES", "4")),
            ),
            content_base_url=os.getenv(
                "VIDEO_CONTENT_API_BASE_URL",
                "",
            ).strip(),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.model and self.base_url)

    @property
    def ffmpeg_enabled(self) -> bool:
        return bool(self.ffmpeg_path)

    def public_config(self) -> dict[str, Any]:
        return {
            "videoModelEnabled": self.enabled,
            "videoModel": self.model if self.enabled else "尚未配置",
            "videoBaseUrl": self.base_url,
            "videoProtocol": "Seedance Video API",
            "ffmpegEnabled": self.ffmpeg_enabled,
            "supportedResolutions": sorted(ALLOWED_RESOLUTIONS),
            "supportedRatios": sorted(ALLOWED_RATIOS),
        }


class SeedanceProvider:
    def __init__(self, settings: SeedanceSettings):
        self.settings = settings

    @property
    def collection_url(self) -> str:
        """Use the configured collection URL exactly; never infer API paths."""
        return self.settings.base_url

    @staticmethod
    def _safe_log_url(url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        if not parsed.query:
            return url
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        redacted = []
        for key, value in query:
            normalized = key.lower()
            if any(
                marker in normalized
                for marker in ("token", "key", "secret", "sign", "credential")
            ):
                value = "***"
            redacted.append((key, value))
        return urllib.parse.urlunparse(
            parsed._replace(query=urllib.parse.urlencode(redacted))
        )

    @classmethod
    def _safe_log_value(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: (
                    "***"
                    if str(key).lower() in SENSITIVE_LOG_KEYS
                    else cls._safe_log_value(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [cls._safe_log_value(item) for item in value]
        if isinstance(value, str) and value.startswith("data:"):
            header = value.partition(",")[0]
            return f"<{header}; {len(value)} chars>"
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return cls._safe_log_url(value)
        return value

    @classmethod
    def _log_api_result(
        cls,
        method: str,
        url: str,
        status: int | str,
        result: Any,
    ) -> None:
        safe_result = cls._safe_log_value(result)
        if isinstance(safe_result, str):
            text = safe_result
        else:
            try:
                text = json.dumps(safe_result, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                text = str(safe_result)
        if len(text) > MAX_API_LOG_CHARS:
            text = f"{text[:MAX_API_LOG_CHARS]}…（响应日志已截断）"
        timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        print(
            f"[视频 API] {timestamp} {method} {cls._safe_log_url(url)} "
            f"-> HTTP {status}\n{text or '(empty response)'}",
            flush=True,
        )

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        data: bytes | None = None,
        content_type: str = "application/json",
    ) -> dict[str, Any]:
        if not self.settings.enabled:
            raise AgentError("尚未配置 Seedance 视频 API。")
        retry_safe = method.upper() in {"GET", "DELETE"}
        max_attempts = (
            max(1, self.settings.request_retries)
            if retry_safe
            else 1
        )
        for attempt in range(1, max_attempts + 1):
            request = urllib.request.Request(
                url,
                data=data,
                headers={
                    "Authorization": f"Bearer {self.settings.api_key}",
                    "Content-Type": content_type,
                    "User-Agent": "Jingzhou-Agent/0.2",
                    "Connection": "close",
                },
                method=method,
            )
            try:
                with urllib.request.urlopen(
                    request,
                    timeout=self.settings.request_timeout,
                ) as response:
                    body = response.read()
                    status = getattr(response, "status", 200)
                    if body:
                        decoded = body.decode("utf-8", errors="replace")
                        try:
                            result = json.loads(decoded)
                        except json.JSONDecodeError as exc:
                            self._log_api_result(
                                method,
                                url,
                                status,
                                decoded,
                            )
                            raise AgentError(
                                "视频 API 返回了非 JSON 响应。"
                            ) from exc
                    else:
                        result = {}
                    self._log_api_result(
                        method,
                        url,
                        status,
                        {
                            "attempt": attempt,
                            "response": result,
                        }
                        if attempt > 1
                        else result,
                    )
                    return result
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                exc.close()
                try:
                    detail = json.loads(body)
                    error = detail.get("error") or detail
                    if isinstance(error, dict):
                        message = error.get("message") or json.dumps(
                            error,
                            ensure_ascii=False,
                        )
                    else:
                        message = str(error)
                except json.JSONDecodeError:
                    detail = body
                    message = body
                retryable = retry_safe and exc.code in {
                    408,
                    409,
                    425,
                    429,
                    500,
                    502,
                    503,
                    504,
                }
                self._log_api_result(
                    method,
                    url,
                    exc.code,
                    {
                        "attempt": attempt,
                        "maxAttempts": max_attempts,
                        "retrying": retryable and attempt < max_attempts,
                        "response": detail,
                    },
                )
                if retryable and attempt < max_attempts:
                    time.sleep(min(2 ** (attempt - 1), 8))
                    continue
                raise AgentError(
                    f"视频 API 返回 HTTP {exc.code}：{message[:700]}"
                ) from exc
            except (
                urllib.error.URLError,
                TimeoutError,
                ssl.SSLError,
                ConnectionError,
                http.client.HTTPException,
            ) as exc:
                reason = getattr(exc, "reason", exc)
                self._log_api_result(
                    method,
                    url,
                    "NETWORK_ERROR",
                    {
                        "attempt": attempt,
                        "maxAttempts": max_attempts,
                        "retrying": retry_safe and attempt < max_attempts,
                        "error": str(reason),
                    },
                )
                if retry_safe and attempt < max_attempts:
                    time.sleep(min(2 ** (attempt - 1), 8))
                    continue
                if isinstance(exc, TimeoutError):
                    raise AgentError(
                        f"视频 API 请求超时（已尝试 {attempt} 次）。"
                    ) from exc
                raise AgentError(
                    f"无法连接视频 API（已尝试 {attempt} 次）：{reason}"
                ) from exc
        raise AgentError("视频 API 请求失败。")

    @staticmethod
    def _unwrap(response: dict[str, Any]) -> dict[str, Any]:
        data = response.get("data")
        return data if isinstance(data, dict) else response

    @staticmethod
    def _normalize_status(value: Any) -> str:
        status = str(value or "queued").lower()
        return {
            "pending": "queued",
            "processing": "running",
            "in_progress": "running",
            "completed": "succeeded",
            "success": "succeeded",
            "failure": "failed",
            "fail": "failed",
            "canceled": "cancelled",
            "error": "failed",
        }.get(status, status)

    @staticmethod
    def _task_error(task: dict[str, Any]) -> Any:
        candidates = [
            task.get("error"),
            (
                task.get("data", {}).get("error")
                if isinstance(task.get("data"), dict)
                else None
            ),
            (
                task.get("result", {}).get("error")
                if isinstance(task.get("result"), dict)
                else None
            ),
        ]
        for candidate in candidates:
            if candidate:
                return candidate
        fail_reason = str(task.get("fail_reason") or "").strip()
        return {"message": fail_reason} if fail_reason else None

    @staticmethod
    def _first_string(
        value: dict[str, Any],
        paths: tuple[tuple[str | int, ...], ...],
    ) -> str:
        for path in paths:
            current: Any = value
            for key in path:
                if isinstance(key, int) and isinstance(current, list):
                    if key >= len(current):
                        current = None
                        break
                    current = current[key]
                elif isinstance(key, str) and isinstance(current, dict):
                    current = current.get(key)
                else:
                    current = None
                    break
            if isinstance(current, str) and current:
                return current
        return ""

    def create_task(
        self,
        *,
        prompt: str,
        duration: int,
        ratio: str,
        resolution: str,
        generate_audio: bool,
        watermark: bool,
        reference_images: list[str] | None = None,
        first_frame: str = "",
        last_frame: str = "",
        priority: int = 0,
    ) -> dict[str, Any]:
        if resolution not in ALLOWED_RESOLUTIONS:
            raise AgentError("视频分辨率无效。")
        if ratio not in ALLOWED_RATIOS:
            raise AgentError("视频画幅无效。")
        if not 4 <= duration <= 15:
            raise AgentError("单段视频时长必须在 4–15 秒。")
        normalized_references = [
            image for image in (reference_images or []) if image
        ]
        if len(normalized_references) > MAX_VIDEO_REFERENCE_IMAGES:
            raise AgentError(
                f"Seedance 每个任务最多提交 "
                f"{MAX_VIDEO_REFERENCE_IMAGES} 张 reference_image。"
            )
        image_content: list[dict[str, Any]] = [
            {
                "type": "image_url",
                "image_url": {"url": image},
                "role": "reference_image",
            }
            for image in normalized_references
        ]
        if first_frame:
            image_content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": first_frame},
                    "role": "first_frame",
                }
            )
        if last_frame:
            image_content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": last_frame},
                    "role": "last_frame",
                }
            )
        metadata: dict[str, Any] = {
            "ratio": ratio,
            "generate_audio": bool(generate_audio),
            "watermark": bool(watermark),
            "priority": int(priority),
        }
        if image_content:
            metadata["content"] = image_content
        payload = {
            "model": self.settings.model,
            "prompt": prompt[:MAX_PROMPT_CHARS],
            "duration": duration,
            "size": resolution,
            "metadata": metadata,
        }
        response = self._request_json(
            "POST",
            self.collection_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            content_type="application/json",
        )
        task = self._unwrap(response)
        task_id = str(task.get("id") or task.get("task_id") or "")
        if not task_id:
            raise AgentError("视频 API 已响应，但未返回任务 ID。")
        return {
            **task,
            "id": task_id,
            "status": self._normalize_status(task.get("status")),
        }

    def get_task(self, task_id: str) -> dict[str, Any]:
        safe_id = urllib.parse.quote(task_id, safe="")
        response = self._request_json(
            "GET",
            f"{self.collection_url}/{safe_id}",
        )
        task = self._unwrap(response)
        video_url = self._first_string(
            task,
            (
                ("video_url",),
                ("output_url",),
                ("url",),
                ("content", "video_url"),
                ("data", "video_url"),
                ("result", "video_url"),
                ("result", "url"),
                ("result", "video", "url"),
                ("output", "video_url"),
                ("output", "url"),
                ("results", 0, "video_url"),
                ("results", 0, "url"),
                ("videos", 0, "url"),
            ),
        )
        last_frame_url = self._first_string(
            task,
            (
                ("last_frame_url",),
                ("content", "last_frame_url"),
                ("data", "last_frame_url"),
                ("result", "last_frame_url"),
                ("output", "last_frame_url"),
                ("results", 0, "last_frame_url"),
            ),
        )
        task_id_value = str(task.get("id") or task.get("task_id") or task_id)
        nested_data = (
            task.get("data") if isinstance(task.get("data"), dict) else {}
        )
        raw_status = nested_data.get("status") or task.get("status")
        return {
            **task,
            "id": task_id_value,
            "status": self._normalize_status(raw_status),
            "error": self._task_error(task),
            "content": {
                **(task.get("content") if isinstance(task.get("content"), dict) else {}),
                "video_url": video_url,
                "last_frame_url": last_frame_url,
            },
        }

    def cancel_or_delete_task(self, task_id: str) -> dict[str, Any]:
        safe_id = urllib.parse.quote(task_id, safe="")
        return self._request_json(
            "DELETE",
            f"{self.collection_url}/{safe_id}",
        )

    def download(
        self,
        url: str,
        destination: Path,
        *,
        authenticated: bool = False,
    ) -> None:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https":
            raise AgentError("视频 API 返回了非 HTTPS 媒体地址，已拒绝下载。")
        base_headers = {
            "User-Agent": "Jingzhou-Agent/0.2",
            "Accept-Encoding": "identity",
        }
        if authenticated:
            base_headers["Authorization"] = f"Bearer {self.settings.api_key}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(f"{destination.suffix}.part")
        succeeded = False
        max_attempts = max(1, self.settings.download_retries)
        try:
            for attempt in range(1, max_attempts + 1):
                offset = temporary.stat().st_size if temporary.exists() else 0
                headers = dict(base_headers)
                if offset:
                    headers["Range"] = f"bytes={offset}-"
                request = urllib.request.Request(url, headers=headers)
                try:
                    with urllib.request.urlopen(
                        request,
                        timeout=self.settings.request_timeout,
                    ) as response:
                        status = getattr(response, "status", 200)
                        content_type = response.headers.get("Content-Type", "")
                        is_resume = bool(offset and status == 206)
                        if not is_resume:
                            offset = 0
                        mode = "ab" if is_resume else "wb"
                        total = offset
                        received = 0
                        declared_text = response.headers.get("Content-Length")
                        declared = int(declared_text) if declared_text else 0
                        expected_total = offset + declared if declared else 0
                        if (
                            expected_total
                            and expected_total > self.settings.download_max_bytes
                        ):
                            raise AgentError(
                                "视频文件超过本地下载大小限制。"
                            )
                        with temporary.open(mode) as output:
                            while True:
                                chunk = response.read(1024 * 1024)
                                if not chunk:
                                    break
                                received += len(chunk)
                                total += len(chunk)
                                if total > self.settings.download_max_bytes:
                                    raise AgentError(
                                        "视频文件超过本地下载大小限制。"
                                    )
                                output.write(chunk)
                        if declared and received < declared:
                            raise urllib.error.URLError(
                                "连接提前结束："
                                f"本次应接收 {declared} 字节，实际接收 {received} 字节"
                            )
                    os.replace(temporary, destination)
                    succeeded = True
                    self._log_api_result(
                        "GET",
                        url,
                        status,
                        {
                            "type": "video_download",
                            "attempt": attempt,
                            "resumed": is_resume,
                            "contentType": content_type,
                            "bytes": total,
                            "savedTo": str(destination),
                        },
                    )
                    return
                except urllib.error.HTTPError as exc:
                    body = exc.read().decode("utf-8", errors="replace")
                    exc.close()
                    try:
                        detail: Any = json.loads(body)
                    except json.JSONDecodeError:
                        detail = body
                    retryable = exc.code in {
                        408,
                        409,
                        425,
                        429,
                        500,
                        502,
                        503,
                        504,
                    }
                    self._log_api_result(
                        "GET",
                        url,
                        exc.code,
                        {
                            "attempt": attempt,
                            "maxAttempts": max_attempts,
                            "retrying": retryable and attempt < max_attempts,
                            "response": detail,
                        },
                    )
                    if retryable and attempt < max_attempts:
                        time.sleep(min(2 ** (attempt - 1), 8))
                        continue
                    raise AgentError(
                        f"视频下载返回 HTTP {exc.code}：{body[:700]}"
                    ) from exc
                except (
                    urllib.error.URLError,
                    TimeoutError,
                    ssl.SSLError,
                    ConnectionError,
                    http.client.HTTPException,
                ) as exc:
                    reason = getattr(exc, "reason", exc)
                    self._log_api_result(
                        "GET",
                        url,
                        "NETWORK_ERROR",
                        {
                            "attempt": attempt,
                            "maxAttempts": max_attempts,
                            "downloadedBytes": (
                                temporary.stat().st_size
                                if temporary.exists()
                                else 0
                            ),
                            "retrying": attempt < max_attempts,
                            "error": str(reason),
                        },
                    )
                    if attempt < max_attempts:
                        time.sleep(min(2 ** (attempt - 1), 8))
                        continue
                    raise AgentError(
                        f"无法下载视频文件（已尝试 {max_attempts} 次）：{reason}"
                    ) from exc
        finally:
            if not succeeded and temporary.exists():
                temporary.unlink()

    def download_task_content(self, task_id: str, destination: Path) -> None:
        if not self.settings.content_base_url:
            raise AgentError(
                "Seedance 任务成功但未返回视频地址；请配置完整的 "
                "VIDEO_CONTENT_API_BASE_URL。"
            )
        safe_id = urllib.parse.quote(task_id, safe="")
        self.download(
            f"{self.settings.content_base_url.rstrip('/')}/{safe_id}/content",
            destination,
            authenticated=True,
        )


# Backward-compatible import name for existing extensions.
OpenAIVideoProvider = SeedanceProvider


def split_duration(duration: int) -> list[int]:
    duration = max(1, int(duration))
    if duration <= 4:
        return [4]
    if duration <= 15:
        return [duration]
    part_count = math.ceil(duration / 15)
    base, remainder = divmod(duration, part_count)
    parts = [base + (1 if index < remainder else 0) for index in range(part_count)]
    return [max(4, min(15, value)) for value in parts]


def build_seedance_prompt(
    project: dict[str, Any],
    shot: dict[str, Any],
    *,
    part: int,
    part_count: int,
) -> str:
    character_map = {
        str(item.get("id")): item
        for item in project.get("characters", [])
        if isinstance(item, dict)
    }
    scene_map = {
        str(item.get("id")): item
        for item in project.get("scenes", [])
        if isinstance(item, dict)
    }
    character_descriptions = []
    for character_id in shot.get("characterIds", []):
        character = character_map.get(str(character_id))
        if not character:
            continue
        character_descriptions.append(
            f"{character.get('name', character_id)}："
            f"{character.get('visualIdentity', '')}，"
            f"表演气质为{character.get('personality', '')}"
        )
    dialogue = str(shot.get("dialogue", "")).strip()
    dialogue_instruction = (
        f'人物清晰自然地说：“{dialogue}”。' if dialogue else "本镜头不出现口播台词。"
    )
    asset_map = {
        str(asset.get("id")): asset
        for asset in project.get("assets", [])
        if isinstance(asset, dict)
    }
    character_reference_descriptions = []
    scene_reference_descriptions = []
    for asset_id in shot.get("referenceAssetIds", []):
        asset = asset_map.get(str(asset_id))
        if asset:
            owner_type = str(asset.get("ownerType") or "")
            owner_id = str(asset.get("ownerId") or "")
            owner = (
                character_map.get(owner_id)
                if owner_type == "character"
                else scene_map.get(owner_id)
                if owner_type == "scene"
                else {}
            ) or {}
            reference_prompt = str(
                asset.get("editPrompt")
                or asset.get("prompt")
                or owner.get("imagePrompt")
                or ""
            ).strip()
            if not reference_prompt:
                continue
            if owner_type == "character":
                character = character_map.get(owner_id) or {}
                character_name = str(
                    character.get("name")
                    or asset.get("ownerName")
                    or owner_id
                    or "未命名角色"
                )
                character_reference_descriptions.append(
                    f"角色“{character_name}”={reference_prompt}"
                    "（该文本为所选参考图自身的提示内容）"
                )
            else:
                scene_name = str(
                    owner.get("name")
                    or asset.get("ownerName")
                    or owner_id
                    or "未命名场景"
                )
                scene_reference_descriptions.append(
                    f"场景“{scene_name}”={reference_prompt}"
                    "（该文本为所选参考图自身的提示内容）"
                )
    character_reference_instruction = (
        "角色参考图是不可改变的身份锚点："
        + "；".join(character_reference_descriptions)
        + "。视频中的人物脸型、五官比例、发型、服装、配饰、年龄感"
        "必须与各自对应的角色参考图一致，不得换脸、换装或改变体型；"
        "多名角色之间不得交换脸、发型、服装、配饰或其他身份特征。"
        if character_reference_descriptions
        else ""
    )
    scene_reference_instruction = (
        "场景参考图是不可改变的环境锚点："
        + "；".join(scene_reference_descriptions)
        + "。视频中的空间布局、背景物体位置、时间天气、光线方向、"
        "色彩和材质必须与场景参考图一致。"
        if scene_reference_descriptions
        else ""
    )
    part_instruction = (
        f"这是该镜头的第 {part}/{part_count} 个连续片段。"
        if part_count > 1
        else ""
    )
    complete_prompt = str(shot.get("completeVideoPrompt") or "").strip()
    if complete_prompt:
        prompt = "\n".join(
            value
            for value in [
                complete_prompt,
                part_instruction,
                character_reference_instruction,
                scene_reference_instruction,
                "严格使用随请求提交的 reference_image、first_frame 与 last_frame；画面中不要出现字幕、标题、Logo、水印或乱码文字。",
            ]
            if value
        )
        return prompt[:MAX_PROMPT_CHARS]
    prompt = "\n".join(
        value
        for value in [
            f"原创影视短片。项目：{project.get('title', '')}。",
            str(
                (project.get("stagePrompts") or {}).get(
                    "storyboard",
                    "",
                )
            ),
            str(
                (project.get("stagePrompts") or {}).get(
                    "video",
                    "",
                )
            ),
            part_instruction,
            f"场景：{shot.get('scene', '')}。",
            f"主体与动作：{shot.get('action', '')}。",
            f"摄影：{shot.get('camera', '')}。",
            f"视觉要求：{shot.get('visualPrompt', '')}。",
            f"时间与运动：{shot.get('videoPrompt', '')}。",
            dialogue_instruction,
            f"声音设计：{shot.get('audio', '')}。",
            (
                "角色必须保持一致："
                + "；".join(character_descriptions)
                + "。"
                if character_descriptions
                else ""
            ),
            f"连续性：{shot.get('continuity', '')}。",
            character_reference_instruction,
            scene_reference_instruction,
            "画面中不要出现字幕、标题、Logo、水印或乱码文字。动作自然，主体结构稳定，结尾保留可剪辑的稳定画面。",
        ]
        if value
    )
    return prompt[:MAX_PROMPT_CHARS]


def build_jobs(project: dict[str, Any]) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    assets = {
        str(asset.get("id")): asset
        for asset in project.get("assets", [])
        if isinstance(asset, dict)
    }
    characters = {
        str(character.get("id")): character
        for character in project.get("characters", [])
        if isinstance(character, dict)
    }
    scenes = {
        str(scene.get("id")): scene
        for scene in project.get("scenes", [])
        if isinstance(scene, dict)
    }
    for shot_index, shot in enumerate(project.get("storyboard", []), start=1):
        if not isinstance(shot, dict):
            continue
        source_shot = int(shot.get("shot") or shot_index)
        reference_ids = list(
            dict.fromkeys(
                str(asset_id)
                for asset_id in shot.get("referenceAssetIds", [])
                if str(asset_id) in assets
            )
        )[:MAX_VIDEO_REFERENCE_IMAGES]
        for character_id in shot.get("characterIds", []):
            if len(reference_ids) >= MAX_VIDEO_REFERENCE_IMAGES:
                break
            character = characters.get(str(character_id)) or {}
            for asset_id in character.get("referenceImageIds", [])[:1]:
                normalized_id = str(asset_id)
                if normalized_id in assets and normalized_id not in reference_ids:
                    reference_ids.append(normalized_id)
        scene = scenes.get(str(shot.get("sceneId"))) or {}
        for asset_id in scene.get("referenceImageIds", [])[:1]:
            if len(reference_ids) >= MAX_VIDEO_REFERENCE_IMAGES:
                break
            normalized_id = str(asset_id)
            if normalized_id in assets and normalized_id not in reference_ids:
                reference_ids.append(normalized_id)
        start_asset_id = str(shot.get("startFrameAssetId") or "")
        end_asset_id = str(shot.get("endFrameAssetId") or "")
        reference_urls = [
            str(assets[asset_id].get("url") or "")
            for asset_id in reference_ids
            if assets[asset_id].get("url")
        ]
        reference_url_asset_ids = [
            asset_id
            for asset_id in reference_ids
            if assets[asset_id].get("url")
        ]
        prompt_shot = {**shot, "referenceAssetIds": reference_ids}
        if end_asset_id in assets and assets[end_asset_id].get("prompt"):
            prompt_shot["continuity"] = (
                f"{shot.get('continuity', '')}；结尾画面参考："
                f"{assets[end_asset_id]['prompt']}"
            )
        parts = split_duration(int(shot.get("duration") or 5))
        for part_index, duration in enumerate(parts, start=1):
            label = f"镜头 {source_shot}"
            if len(parts) > 1:
                label += f" · {part_index}/{len(parts)}"
            jobs.append(
                {
                    "id": str(uuid.uuid4()),
                    "sourceShot": source_shot,
                    "sourceShotId": str(
                        shot.get("id") or f"shot-{source_shot}"
                    ),
                    "part": part_index,
                    "partCount": len(parts),
                    "label": label,
                    "status": "pending",
                    "duration": duration,
                    "prompt": build_seedance_prompt(
                        project,
                        prompt_shot,
                        part=part_index,
                        part_count=len(parts),
                    ),
                    "referenceAssetIds": reference_ids,
                    "referenceImageUrls": reference_urls,
                    "referenceImageAssetIds": reference_url_asset_ids,
                    "startFrameAssetId": (
                        start_asset_id if start_asset_id in assets else ""
                    ),
                    "endFrameAssetId": (
                        end_asset_id if end_asset_id in assets else ""
                    ),
                    "startFrameUrl": (
                        str(assets[start_asset_id].get("url") or "")
                        if start_asset_id in assets
                        else ""
                    ),
                    "endFrameUrl": (
                        str(assets[end_asset_id].get("url") or "")
                        if end_asset_id in assets
                        else ""
                    ),
                    "inputReferenceSource": "",
                    "inputReferenceAssetId": "",
                    "submittedReferenceAssetIds": [],
                    "lastFrameReferenceAssetId": "",
                    "taskId": "",
                    "attempts": 0,
                    "videoUrl": "",
                    "lastFrameUrl": "",
                    "localVideoUrl": "",
                    "localLastFrameUrl": "",
                    "error": "",
                    "providerErrorCode": "",
                    "policyViolation": False,
                    "createdAt": utc_now(),
                    "startedAt": "",
                    "finishedAt": "",
                }
            )
    return jobs


class VideoPipelineManager:
    def __init__(
        self,
        store: ProjectStore,
        media_root: Path,
        settings: SeedanceSettings | None = None,
        provider: SeedanceProvider | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.store = store
        self.media_root = media_root
        self.media_root.mkdir(parents=True, exist_ok=True)
        self.settings = settings or SeedanceSettings.from_environment()
        self.provider = provider or SeedanceProvider(self.settings)
        self.sleep = sleep
        self._active: set[str] = set()
        self._lock = threading.RLock()

    def config(self) -> dict[str, Any]:
        return self.settings.public_config()

    def is_active(self, project_id: str) -> bool:
        with self._lock:
            return project_id in self._active

    @staticmethod
    def _assert_complete_prompts(
        project: dict[str, Any],
        shot_id: str = "",
    ) -> None:
        candidates = [
            shot
            for shot in project.get("storyboard", [])
            if isinstance(shot, dict)
            and (
                not shot_id
                or str(shot.get("id") or "") == shot_id
            )
        ]
        if shot_id and not candidates:
            raise AgentError("分镜不存在。")
        incomplete = [
            int(shot.get("shot") or index + 1)
            for index, shot in enumerate(candidates)
            if not str(shot.get("completeVideoPrompt") or "").strip()
            or bool(shot.get("completeVideoPromptStale"))
        ]
        if incomplete:
            raise AgentError(
                "请先生成并确认完整视频提示词：镜头 "
                + "、".join(str(value) for value in incomplete)
            )

    def start(
        self,
        project_id: str,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.settings.enabled:
            raise AgentError(
                "尚未配置视频 API。请设置 VIDEO_API_KEY 和 VIDEO_MODEL。"
            )
        project = self.store.get(project_id)
        if not project:
            raise AgentError("项目不存在。")
        if options.get("confirmedPromptWorkflow"):
            self._assert_complete_prompts(project)
        existing = project.get("videoProduction")
        if existing and existing.get("status") in ACTIVE_PRODUCTION_STATUSES:
            self._spawn(project_id)
            return existing

        resolution = str(options.get("resolution", "720p"))
        ratio = str(options.get("ratio") or project.get("brief", {}).get("aspectRatio", "9:16"))
        if resolution not in ALLOWED_RESOLUTIONS:
            raise AgentError("不支持该视频分辨率。")
        if ratio not in ALLOWED_RATIOS:
            raise AgentError("不支持该视频画幅。")
        jobs = build_jobs(project)
        if not jobs:
            raise AgentError("当前项目没有可生成的视频分镜。")

        production = {
            "id": str(uuid.uuid4()),
            "status": "queued",
            "retryScope": "all",
            "retryJobId": "",
            "createdAt": utc_now(),
            "updatedAt": utc_now(),
            "completedJobs": 0,
            "failedJobs": 0,
            "totalJobs": len(jobs),
            "settings": {
                "model": self.settings.model,
                "resolution": resolution,
                "ratio": ratio,
                "generateAudio": bool(options.get("generateAudio", True)),
                "watermark": bool(options.get("watermark", False)),
                "continuity": bool(options.get("continuity", True)),
                "priority": max(0, min(9, int(options.get("priority", 0)))),
            },
            "jobs": jobs,
            "manifestUrl": "",
            "finalVideoUrl": "",
            "assembly": {
                "status": "pending",
                "message": "",
            },
            "error": "",
        }

        def assign(current: dict[str, Any]) -> None:
            current["videoProduction"] = production

        self.store.mutate(project_id, assign)
        self._spawn(project_id)
        return production

    def start_shot(
        self,
        project_id: str,
        shot_id: str,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.settings.enabled:
            raise AgentError(
                "尚未配置视频 API。请设置 VIDEO_API_KEY 和 VIDEO_MODEL。"
            )
        project = self.store.get(project_id)
        if not project:
            raise AgentError("项目不存在。")
        self._assert_complete_prompts(project, shot_id)
        existing = project.get("videoProduction") or {}
        if (
            existing.get("status") in ACTIVE_PRODUCTION_STATUSES
            or self.is_active(project_id)
        ):
            raise AgentError("已有视频任务正在生成，请完成后再生成这个分镜。")

        fresh_jobs = build_jobs(project)
        target_jobs = [
            job
            for job in fresh_jobs
            if str(job.get("sourceShotId") or "") == shot_id
        ]
        if not target_jobs:
            raise AgentError("分镜不存在或无法生成视频。")

        previous_settings = existing.get("settings") or {}
        resolution = str(
            options.get("resolution")
            or previous_settings.get("resolution")
            or "720p"
        )
        ratio = str(
            options.get("ratio")
            or previous_settings.get("ratio")
            or project.get("brief", {}).get("aspectRatio", "9:16")
        )
        if resolution not in ALLOWED_RESOLUTIONS:
            raise AgentError("不支持该视频分辨率。")
        if ratio not in ALLOWED_RATIOS:
            raise AgentError("不支持该视频画幅。")

        previous_by_source = {
            (
                str(job.get("sourceShotId") or ""),
                int(job.get("part") or 1),
            ): copy.deepcopy(job)
            for job in existing.get("jobs", [])
            if isinstance(job, dict)
            and str(job.get("sourceShotId") or "") != shot_id
        }
        merged_jobs: list[dict[str, Any]] = []
        target_job_ids = {
            str(job.get("id") or "") for job in target_jobs
        }
        for fresh in fresh_jobs:
            key = (
                str(fresh.get("sourceShotId") or ""),
                int(fresh.get("part") or 1),
            )
            if key[0] == shot_id:
                merged_jobs.append(fresh)
            elif key in previous_by_source:
                merged_jobs.append(previous_by_source[key])

        settings = {
            "model": self.settings.model,
            "resolution": resolution,
            "ratio": ratio,
            "generateAudio": bool(
                options.get(
                    "generateAudio",
                    previous_settings.get("generateAudio", True),
                )
            ),
            "watermark": bool(
                options.get(
                    "watermark",
                    previous_settings.get("watermark", False),
                )
            ),
            "continuity": bool(
                options.get(
                    "continuity",
                    previous_settings.get("continuity", True),
                )
            ),
            "priority": max(
                0,
                min(
                    9,
                    int(
                        options.get(
                            "priority",
                            previous_settings.get("priority", 0),
                        )
                    ),
                ),
            ),
        }
        completed = sum(
            job.get("status") == "succeeded" for job in merged_jobs
        )
        production = {
            "id": str(uuid.uuid4()),
            "retryOf": existing.get("id", ""),
            "status": "queued",
            "retryScope": "shot",
            "retryShotId": shot_id,
            "retryJobId": next(iter(target_job_ids), ""),
            "createdAt": utc_now(),
            "updatedAt": utc_now(),
            "completedJobs": completed,
            "failedJobs": sum(
                job.get("status") in {"failed", "expired", "cancelled"}
                for job in merged_jobs
            ),
            "totalJobs": len(merged_jobs),
            "settings": settings,
            "jobs": merged_jobs,
            "manifestUrl": "",
            "finalVideoUrl": "",
            "assembly": {
                "status": "pending",
                "message": "单个分镜生成完成后可重新拼接。",
            },
            "error": "",
            "stale": False,
            "storyboardRevision": int(project.get("storyboardRevision") or 0),
        }

        def assign(current: dict[str, Any]) -> None:
            current["videoProduction"] = production

        self.store.mutate(project_id, assign)
        self._spawn(project_id)
        return production

    def retry_failed(self, project_id: str) -> dict[str, Any]:
        if not self.settings.enabled:
            raise AgentError(
                "尚未配置视频 API。请设置 VIDEO_API_KEY 和 VIDEO_MODEL。"
            )
        project = self.store.get(project_id)
        if not project:
            raise AgentError("项目不存在。")
        existing = project.get("videoProduction") or {}
        if not existing:
            raise AgentError("当前项目还没有视频生产记录。")
        if existing.get("stale"):
            raise AgentError(
                "分镜或角色已经修改，请按最新分镜重新提交整轮视频生产。"
            )
        if existing.get("status") in ACTIVE_PRODUCTION_STATUSES:
            raise AgentError("视频生产仍在进行中，暂时不能重试。")

        retryable = [
            job
            for job in existing.get("jobs", [])
            if job.get("status") != "succeeded"
        ]
        if not retryable:
            raise AgentError("没有需要重新生成的失败镜头。")
        policy_blocked = [
            str(job.get("label") or "未命名镜头")
            for job in retryable
            if job.get("policyViolation")
        ]
        if policy_blocked:
            raise AgentError(
                "以下镜头触发了版权/内容政策审核，不能原样批量重试："
                + "、".join(policy_blocked)
                + "。请先重新生成并确认原创化的完整视频提示词。"
            )

        production = copy.deepcopy(existing)
        production["retryOf"] = existing.get("id", "")
        production["id"] = str(uuid.uuid4())
        production["status"] = "queued"
        production["retryScope"] = "failed"
        production["retryJobId"] = ""
        production["createdAt"] = utc_now()
        production["updatedAt"] = utc_now()
        production["completedJobs"] = sum(
            job.get("status") == "succeeded" for job in production.get("jobs", [])
        )
        production["failedJobs"] = 0
        production["manifestUrl"] = ""
        production["finalVideoUrl"] = ""
        production["assembly"] = {"status": "pending", "message": ""}
        production["error"] = ""
        for job in production.get("jobs", []):
            if job.get("status") == "succeeded":
                continue
            previous_error = str(job.get("error") or "")
            download_failure = any(
                marker in previous_error
                for marker in (
                    "无法下载视频文件",
                    "视频下载返回 HTTP",
                    "视频文件下载超时",
                    "视频文件超过本地下载大小限制",
                )
            )
            status_query_failure = any(
                marker in previous_error
                for marker in (
                    "无法连接视频 API",
                    "视频 API 请求超时",
                    "UNEXPECTED_EOF_WHILE_READING",
                    "连接提前结束",
                )
            )
            reuse_existing_task = bool(job.get("taskId")) and (
                download_failure or status_query_failure
            )
            job["status"] = "pending"
            if not reuse_existing_task:
                job["taskId"] = ""
            job["retryMode"] = (
                "download"
                if download_failure
                else "resume"
                if status_query_failure
                else "regenerate"
            )
            if not reuse_existing_task:
                job["videoUrl"] = ""
                job["lastFrameUrl"] = ""
            job["localVideoUrl"] = ""
            job["localLastFrameUrl"] = ""
            job["error"] = ""
            job["providerErrorCode"] = ""
            job["policyViolation"] = False
            job["startedAt"] = ""
            job["finishedAt"] = ""
            job["providerUpdatedAt"] = ""
            if not reuse_existing_task:
                job["inputReferenceSource"] = ""
                job["inputReferenceAssetId"] = ""
                job["submittedReferenceAssetIds"] = []
                job["lastFrameReferenceAssetId"] = ""

        def assign(current: dict[str, Any]) -> None:
            current["videoProduction"] = production

        self.store.mutate(project_id, assign)
        self._spawn(project_id)
        return production

    def assemble(self, project_id: str) -> dict[str, Any]:
        project = self.store.get(project_id)
        if not project:
            raise AgentError("项目不存在。")
        production = project.get("videoProduction") or {}
        if not production:
            raise AgentError("当前项目还没有视频生产记录。")
        if production.get("stale"):
            raise AgentError(
                "分镜或角色已经修改，请先按最新分镜重新生成视频。"
            )
        if (
            production.get("status") in ACTIVE_PRODUCTION_STATUSES
            or self.is_active(project_id)
        ):
            raise AgentError("视频仍在生成中，请完成后再拼接。")
        succeeded = [
            job
            for job in production.get("jobs", [])
            if job.get("status") == "succeeded"
        ]
        if not succeeded:
            raise AgentError("没有可拼接的成功视频片段。")
        missing_local = [
            str(job.get("label") or "未命名镜头")
            for job in succeeded
            if not str(job.get("localVideoUrl") or "")
        ]
        if missing_local:
            raise AgentError(
                "以下镜头尚未保存到本机，无法拼接："
                + "、".join(missing_local)
            )

        manifest_url = self._write_manifest(project_id, project, succeeded)
        final_url, assembly = self._assemble(project_id, succeeded)

        def save_result(current: dict[str, Any]) -> None:
            target = current.get("videoProduction") or {}
            if target.get("id") != production.get("id"):
                raise AgentError("视频生产记录已变化，请刷新后重试。")
            target["manifestUrl"] = manifest_url
            target["finalVideoUrl"] = final_url
            target["assembly"] = assembly
            target["updatedAt"] = utc_now()

        updated = self.store.mutate(project_id, save_result)
        if not updated:
            raise AgentError("项目不存在。")
        return updated["videoProduction"]

    def delete_video_asset(
        self,
        project_id: str,
        video_asset_id: str,
    ) -> dict[str, Any]:
        project = self.store.get(project_id)
        if not project:
            raise AgentError("项目不存在。")
        production = project.get("videoProduction") or {}
        if not production:
            raise AgentError("当前项目还没有视频资产。")
        if (
            production.get("status") in ACTIVE_PRODUCTION_STATUSES
            or self.is_active(project_id)
        ):
            raise AgentError("视频仍在生成中，暂时不能删除视频资产。")
        asset_id = str(video_asset_id or "").strip()
        if asset_id != "final" and not any(
            str(job.get("id") or "") == asset_id
            for job in production.get("jobs", [])
        ):
            raise AgentError("视频资产不存在。")

        urls_to_delete: list[str] = []
        if asset_id == "final":
            urls_to_delete.extend(
                [
                    str(production.get("finalVideoUrl") or ""),
                    str(production.get("manifestUrl") or ""),
                ]
            )
        else:
            target = next(
                job
                for job in production.get("jobs", [])
                if str(job.get("id") or "") == asset_id
            )
            urls_to_delete.extend(
                [
                    str(target.get("localVideoUrl") or ""),
                    str(target.get("localLastFrameUrl") or ""),
                    str(production.get("finalVideoUrl") or ""),
                    str(production.get("manifestUrl") or ""),
                ]
            )

        def remove_video(current: dict[str, Any]) -> None:
            target_production = current.get("videoProduction") or {}
            if target_production.get("id") != production.get("id"):
                raise AgentError("视频生产记录已变化，请刷新后重试。")
            if asset_id == "final":
                target_production["finalVideoUrl"] = ""
                target_production["manifestUrl"] = ""
                target_production["assembly"] = {
                    "status": "deleted",
                    "message": "完整合片已从资产库删除，可重新一键拼接。",
                }
            else:
                target_job = self._job(current, asset_id)
                for field in (
                    "videoUrl",
                    "lastFrameUrl",
                    "localVideoUrl",
                    "localLastFrameUrl",
                ):
                    target_job[field] = ""
                target_job["status"] = "failed"
                target_job["error"] = (
                    "视频输出已从资产库删除，可单独重新生成。"
                )
                target_job["finishedAt"] = utc_now()
                target_production["finalVideoUrl"] = ""
                target_production["manifestUrl"] = ""
                target_production["assembly"] = {
                    "status": "pending",
                    "message": "镜头资产发生变化，需要重新拼接。",
                }
                jobs = target_production.get("jobs", [])
                completed = sum(
                    job.get("status") == "succeeded" for job in jobs
                )
                target_production["completedJobs"] = completed
                target_production["failedJobs"] = len(jobs) - completed
                target_production["status"] = (
                    "partial" if completed else "failed"
                )
            target_production["updatedAt"] = utc_now()

        updated = self.store.mutate(project_id, remove_video)
        if not updated:
            raise AgentError("项目不存在。")
        project_root = (self.media_root / project_id).resolve()
        for url in urls_to_delete:
            if not url.startswith(f"/media/{project_id}/"):
                continue
            candidate = (
                self.media_root
                / project_id
                / url.removeprefix(f"/media/{project_id}/")
            ).resolve()
            if project_root in candidate.parents and candidate.is_file():
                candidate.unlink()
        return updated["videoProduction"]

    def retry_job(
        self,
        project_id: str,
        job_id: str,
    ) -> dict[str, Any]:
        if not self.settings.enabled:
            raise AgentError(
                "尚未配置视频 API。请设置 VIDEO_API_KEY 和 VIDEO_MODEL。"
            )
        project = self.store.get(project_id)
        if not project:
            raise AgentError("项目不存在。")
        existing = project.get("videoProduction") or {}
        if not existing:
            raise AgentError("当前项目还没有视频生产记录。")
        if existing.get("stale"):
            raise AgentError(
                "分镜或角色已经修改，请按最新分镜重新提交整轮视频生产。"
            )
        if existing.get("status") in ACTIVE_PRODUCTION_STATUSES:
            raise AgentError("视频生产仍在进行中，暂时不能单独重生成。")
        source = next(
            (
                job
                for job in existing.get("jobs", [])
                if str(job.get("id")) == job_id
            ),
            None,
        )
        if not source:
            raise AgentError("视频分镜任务不存在。")
        if source.get("policyViolation"):
            self._assert_complete_prompts(
                project,
                str(source.get("sourceShotId") or ""),
            )

        production = copy.deepcopy(existing)
        production["retryOf"] = existing.get("id", "")
        production["id"] = str(uuid.uuid4())
        production["status"] = "queued"
        production["retryScope"] = "single"
        production["retryJobId"] = job_id
        production["createdAt"] = utc_now()
        production["updatedAt"] = utc_now()
        production["manifestUrl"] = ""
        production["finalVideoUrl"] = ""
        production["assembly"] = {"status": "pending", "message": ""}
        production["error"] = ""
        fresh_jobs = build_jobs(project)
        fresh = next(
            (
                job
                for job in fresh_jobs
                if int(job.get("sourceShot", 0))
                == int(source.get("sourceShot", 0))
                and int(job.get("part", 1)) == int(source.get("part", 1))
            ),
            None,
        )
        target = next(
            job
            for job in production.get("jobs", [])
            if str(job.get("id")) == job_id
        )
        if fresh:
            for field in (
                "duration",
                "prompt",
                "referenceAssetIds",
                "referenceImageUrls",
                "referenceImageAssetIds",
                "startFrameAssetId",
                "endFrameAssetId",
                "startFrameUrl",
                "endFrameUrl",
                "sourceShotId",
            ):
                target[field] = copy.deepcopy(fresh.get(field))
        target["status"] = "pending"
        target["taskId"] = ""
        target["videoUrl"] = ""
        target["lastFrameUrl"] = ""
        target["localVideoUrl"] = ""
        target["localLastFrameUrl"] = ""
        target["error"] = ""
        target["providerErrorCode"] = ""
        target["policyViolation"] = False
        target["retryMode"] = "regenerate"
        target["startedAt"] = ""
        target["finishedAt"] = ""
        target["providerUpdatedAt"] = ""
        target["inputReferenceSource"] = ""
        target["inputReferenceAssetId"] = ""
        target["submittedReferenceAssetIds"] = []
        target["lastFrameReferenceAssetId"] = ""
        production["completedJobs"] = sum(
            job.get("status") == "succeeded"
            for job in production.get("jobs", [])
        )
        production["failedJobs"] = sum(
            job.get("status") in {"failed", "expired", "cancelled"}
            for job in production.get("jobs", [])
        )

        def assign(current: dict[str, Any]) -> None:
            current["videoProduction"] = production

        self.store.mutate(project_id, assign)
        self._spawn(project_id)
        return production

    def resume_incomplete(self) -> None:
        for project in self.store.iter_projects():
            production = project.get("videoProduction") or {}
            if production.get("status") in ACTIVE_PRODUCTION_STATUSES:
                self._spawn(project.get("id", ""))

    def _spawn(self, project_id: str) -> None:
        if not project_id:
            return
        with self._lock:
            if project_id in self._active:
                return
            self._active.add(project_id)
        thread = threading.Thread(
            target=self._run_guarded,
            args=(project_id,),
            name=f"video-{project_id[:8]}",
            daemon=True,
        )
        thread.start()

    def _run_guarded(self, project_id: str) -> None:
        try:
            self._run(project_id)
        except BaseException as exc:
            message = safe_error_message(exc)

            def fail(project: dict[str, Any]) -> None:
                production = project.get("videoProduction") or {}
                production["status"] = "failed"
                production["error"] = message
                production["updatedAt"] = utc_now()

            self.store.mutate(project_id, fail)
        finally:
            with self._lock:
                self._active.discard(project_id)

    def _run(self, project_id: str) -> None:
        def mark_running(project: dict[str, Any]) -> None:
            production = project["videoProduction"]
            production["status"] = "running"
            production["updatedAt"] = utc_now()

        self.store.mutate(project_id, mark_running)
        project = self.store.get(project_id)
        if not project:
            return
        production_id = project["videoProduction"]["id"]
        job_ids = [job["id"] for job in project["videoProduction"]["jobs"]]
        previous_frame = ""

        for job_id in job_ids:
            project = self.store.get(project_id)
            if not project:
                return
            production = project.get("videoProduction") or {}
            if production.get("id") != production_id:
                return
            job = next(
                (item for item in production.get("jobs", []) if item.get("id") == job_id),
                None,
            )
            if not job:
                continue
            if job.get("status") == "succeeded":
                previous_frame = self._frame_input(project_id, job)
                continue
            if job.get("status") in {"failed", "expired", "cancelled"}:
                continue

            try:
                task_id = str(job.get("taskId") or "")
                if not task_id:
                    continuity_enabled = production.get("settings", {}).get(
                        "continuity",
                        True,
                    )
                    references = self._job_reference_inputs(
                        project_id,
                        job,
                    )
                    first_frame, first_frame_asset_id = (
                        self._job_frame_input(
                            project_id,
                            str(job.get("startFrameUrl") or ""),
                            str(job.get("startFrameAssetId") or ""),
                        )
                    )
                    last_frame, last_frame_asset_id = self._job_frame_input(
                        project_id,
                        str(job.get("endFrameUrl") or ""),
                        str(job.get("endFrameAssetId") or ""),
                    )
                    input_reference_source = (
                        "start_frame"
                        if first_frame
                        else "last_frame"
                        if last_frame
                        else "reference_images"
                    )
                    input_reference_asset_id = first_frame_asset_id
                    if (
                        continuity_enabled
                        and int(job.get("part", 1)) > 1
                    ):
                        if previous_frame:
                            first_frame = previous_frame
                            input_reference_source = "previous_part_tail"
                            input_reference_asset_id = ""
                    submitted_references = references[
                        :MAX_VIDEO_REFERENCE_IMAGES
                    ]
                    if (
                        submitted_references
                        and not input_reference_asset_id
                    ):
                        input_reference_asset_id = (
                            submitted_references[0][1]
                        )
                    if (
                        not submitted_references
                        and not first_frame
                        and not last_frame
                    ):
                        input_reference_source = "none"
                    result = self.provider.create_task(
                        prompt=job["prompt"],
                        duration=int(job["duration"]),
                        ratio=production["settings"]["ratio"],
                        resolution=production["settings"]["resolution"],
                        generate_audio=production["settings"]["generateAudio"],
                        watermark=production["settings"]["watermark"],
                        reference_images=[
                            value for value, _asset_id in submitted_references
                        ],
                        first_frame=first_frame,
                        last_frame=last_frame,
                        priority=production["settings"].get("priority", 0),
                    )
                    task_id = result["id"]

                    def submitted(current: dict[str, Any]) -> None:
                        target = self._job(current, job_id)
                        target["taskId"] = task_id
                        target["status"] = "queued"
                        target["attempts"] = int(target.get("attempts", 0)) + 1
                        target["inputReferenceSource"] = (
                            input_reference_source
                        )
                        target["inputReferenceAssetId"] = (
                            input_reference_asset_id
                        )
                        target["submittedReferenceAssetIds"] = [
                            asset_id
                            for _value, asset_id in submitted_references
                            if asset_id
                        ]
                        target["lastFrameReferenceAssetId"] = (
                            last_frame_asset_id if last_frame else ""
                        )
                        target["startedAt"] = target.get("startedAt") or utc_now()
                        current["videoProduction"]["updatedAt"] = utc_now()

                    self.store.mutate(project_id, submitted)

                result = self._poll_task(project_id, job_id, task_id)
                if result.get("status") == "succeeded":
                    self._save_outputs(project_id, job_id, result)
                    latest = self.store.get(project_id) or {}
                    latest_job = self._job(latest, job_id)
                    previous_frame = self._frame_input(project_id, latest_job)
            except BaseException as exc:
                message = safe_error_message(exc)

                def job_failed(current: dict[str, Any]) -> None:
                    target = self._job(current, job_id)
                    target["status"] = "failed"
                    target["error"] = message
                    target["finishedAt"] = utc_now()
                    current["videoProduction"]["updatedAt"] = utc_now()

                self.store.mutate(project_id, job_failed)

        self._finish_production(project_id, production_id)

    def _poll_task(
        self,
        project_id: str,
        job_id: str,
        task_id: str,
    ) -> dict[str, Any]:
        while True:
            result = self.provider.get_task(task_id)
            status = str(result.get("status", "running"))
            if status not in FINAL_TASK_STATUSES | {"queued", "running"}:
                status = "running"
            provider_error = result.get("error")
            provider_error_code = self._provider_error_code(
                provider_error
            )
            policy_violation = self._is_policy_violation(
                provider_error_code,
                provider_error,
            )

            def update_status(current: dict[str, Any]) -> None:
                target = self._job(current, job_id)
                target["status"] = status
                nested_data = (
                    result.get("data")
                    if isinstance(result.get("data"), dict)
                    else {}
                )
                target["providerUpdatedAt"] = (
                    result.get("updated_at")
                    or nested_data.get("updated_at")
                    or ""
                )
                if provider_error:
                    target["error"] = self._provider_error(
                        provider_error
                    )
                    target["providerErrorCode"] = provider_error_code
                    target["policyViolation"] = policy_violation
                if policy_violation:
                    source_shot_id = str(
                        target.get("sourceShotId") or ""
                    )
                    for shot in current.get("storyboard", []):
                        if (
                            isinstance(shot, dict)
                            and str(shot.get("id") or "")
                            == source_shot_id
                        ):
                            shot["completeVideoPromptStale"] = True
                            shot["videoPolicyViolation"] = {
                                "code": provider_error_code,
                                "message": target["error"],
                                "detectedAt": utc_now(),
                            }
                            break
                current["videoProduction"]["updatedAt"] = utc_now()

            self.store.mutate(project_id, update_status)
            if status in FINAL_TASK_STATUSES:
                if status != "succeeded":
                    def finalize_failure(current: dict[str, Any]) -> None:
                        target = self._job(current, job_id)
                        target["finishedAt"] = utc_now()

                    self.store.mutate(project_id, finalize_failure)
                return result
            self.sleep(self.settings.poll_interval)

    def _save_outputs(
        self,
        project_id: str,
        job_id: str,
        result: dict[str, Any],
    ) -> None:
        content = result.get("content") or {}
        video_url = str(content.get("video_url") or "")
        last_frame_url = str(content.get("last_frame_url") or "")

        def record_remote_sources(current: dict[str, Any]) -> None:
            target = self._job(current, job_id)
            target["status"] = "downloading"
            target["videoUrl"] = video_url
            target["lastFrameUrl"] = last_frame_url
            target["error"] = ""
            current["videoProduction"]["updatedAt"] = utc_now()

        self.store.mutate(project_id, record_remote_sources)

        project = self.store.get(project_id) or {}
        job = self._job(project, job_id)
        job_position = (
            project.get("videoProduction", {}).get("jobs", []).index(job) + 1
        )
        directory = self.media_root / project_id
        video_name = f"clip-{job_position:03d}.mp4"
        frame_name = f"clip-{job_position:03d}-last.png"
        if video_url:
            self.provider.download(video_url, directory / video_name)
        else:
            task_id = str(result.get("id") or job.get("taskId") or "")
            if not task_id:
                raise AgentError("视频任务成功，但没有任务 ID 或视频地址。")
            self.provider.download_task_content(task_id, directory / video_name)
        if last_frame_url:
            try:
                self.provider.download(last_frame_url, directory / frame_name)
            except AgentError:
                frame_name = ""

        def complete(current: dict[str, Any]) -> None:
            target = self._job(current, job_id)
            target["status"] = "succeeded"
            target["videoUrl"] = video_url
            target["lastFrameUrl"] = last_frame_url
            target["localVideoUrl"] = f"/media/{project_id}/{video_name}"
            target["localLastFrameUrl"] = (
                f"/media/{project_id}/{frame_name}" if frame_name else ""
            )
            target["usage"] = result.get("usage") or {}
            target["finishedAt"] = utc_now()
            current["videoProduction"]["updatedAt"] = utc_now()

        self.store.mutate(project_id, complete)

    def _finish_production(self, project_id: str, production_id: str) -> None:
        project = self.store.get(project_id)
        if not project:
            return
        production = project.get("videoProduction") or {}
        if production.get("id") != production_id:
            return
        jobs = production.get("jobs", [])
        succeeded = [job for job in jobs if job.get("status") == "succeeded"]
        failed = [job for job in jobs if job.get("status") != "succeeded"]
        manifest_url = self._write_manifest(project_id, project, succeeded)
        final_url, assembly = self._assemble(project_id, succeeded)

        def finalize(current: dict[str, Any]) -> None:
            target = current["videoProduction"]
            target["completedJobs"] = len(succeeded)
            target["failedJobs"] = len(failed)
            target["manifestUrl"] = manifest_url
            target["finalVideoUrl"] = final_url
            target["assembly"] = assembly
            target["status"] = (
                "succeeded"
                if succeeded and not failed
                else "partial"
                if succeeded
                else "failed"
            )
            target["updatedAt"] = utc_now()
            if not succeeded:
                target["error"] = "所有视频任务均未成功。"

        self.store.mutate(project_id, finalize)

    def _write_manifest(
        self,
        project_id: str,
        project: dict[str, Any],
        jobs: list[dict[str, Any]],
    ) -> str:
        directory = self.media_root / project_id
        directory.mkdir(parents=True, exist_ok=True)
        manifest = {
            "projectId": project_id,
            "title": project.get("title"),
            "createdAt": utc_now(),
            "clips": [
                {
                    "label": job.get("label"),
                    "duration": job.get("duration"),
                    "video": job.get("localVideoUrl"),
                    "sourceShot": job.get("sourceShot"),
                }
                for job in jobs
            ],
        }
        (directory / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return f"/media/{project_id}/manifest.json"

    def _assemble(
        self,
        project_id: str,
        jobs: list[dict[str, Any]],
    ) -> tuple[str, dict[str, str]]:
        if not jobs:
            return "", {"status": "skipped", "message": "没有可合成的视频片段。"}
        if len(jobs) == 1:
            return str(jobs[0].get("localVideoUrl") or ""), {
                "status": "succeeded",
                "message": "单镜头项目无需额外合片。",
            }
        if not self.settings.ffmpeg_enabled:
            return "", {
                "status": "needs_ffmpeg",
                "message": "镜头已全部保存；安装 FFmpeg 或配置 FFMPEG_PATH 后可自动合成为完整视频。",
            }

        directory = self.media_root / project_id
        input_file = directory / "concat.txt"
        output_file = directory / "final.mp4"
        lines = []
        for job in jobs:
            local_url = str(job.get("localVideoUrl") or "")
            filename = Path(local_url).name
            escaped = (directory / filename).resolve().as_posix().replace("'", "'\\''")
            lines.append(f"file '{escaped}'")
        input_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        copy_command = [
            self.settings.ffmpeg_path,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(input_file),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output_file),
        ]
        result = subprocess.run(
            copy_command,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        if result.returncode != 0:
            transcode_command = [
                self.settings.ffmpeg_path,
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(input_file),
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                str(output_file),
            ]
            result = subprocess.run(
                transcode_command,
                capture_output=True,
                text=True,
                timeout=1800,
                check=False,
            )
        if result.returncode == 0 and output_file.is_file():
            return f"/media/{project_id}/final.mp4", {
                "status": "succeeded",
                "message": "所有镜头已合成为完整 MP4。",
            }
        return "", {
            "status": "failed",
            "message": f"FFmpeg 合片失败：{result.stderr[-500:]}",
        }

    def _frame_input(self, project_id: str, job: dict[str, Any]) -> str:
        local_url = str(job.get("localLastFrameUrl") or "")
        if local_url:
            path = self.media_root / project_id / Path(local_url).name
            if path.is_file() and path.stat().st_size <= 30 * 1024 * 1024:
                encoded = base64.b64encode(path.read_bytes()).decode("ascii")
                return f"data:image/png;base64,{encoded}"
        return str(job.get("lastFrameUrl") or "")

    def _job_reference_inputs(
        self,
        project_id: str,
        job: dict[str, Any],
    ) -> list[tuple[str, str]]:
        asset_ids = [
            str(value)
            for value in job.get("referenceImageAssetIds", [])
        ]
        references: list[tuple[str, str]] = []
        seen: set[str] = set()
        for index, url in enumerate(job.get("referenceImageUrls", [])):
            value, asset_id = self._job_frame_input(
                project_id,
                str(url or ""),
                asset_ids[index] if index < len(asset_ids) else "",
            )
            if value and value not in seen:
                seen.add(value)
                references.append((value, asset_id))
        return references

    def _job_frame_input(
        self,
        project_id: str,
        url: str,
        asset_id: str = "",
    ) -> tuple[str, str]:
        if url.startswith(f"/media/{project_id}/"):
            path = self.media_root / project_id / url.removeprefix(
                f"/media/{project_id}/"
            )
            resolved = path.resolve()
            project_root = (self.media_root / project_id).resolve()
            if (
                project_root in resolved.parents
                and resolved.is_file()
                and resolved.stat().st_size <= 30 * 1024 * 1024
            ):
                mime_type = (
                    "image/jpeg"
                    if resolved.suffix.lower() in {".jpg", ".jpeg"}
                    else "image/webp"
                    if resolved.suffix.lower() == ".webp"
                    else "image/png"
                )
                encoded = base64.b64encode(
                    resolved.read_bytes()
                ).decode("ascii")
                return f"data:{mime_type};base64,{encoded}", asset_id
        elif url.startswith(("https://", "data:")):
            return url, asset_id
        return "", ""

    @staticmethod
    def _job(project: dict[str, Any], job_id: str) -> dict[str, Any]:
        for job in project.get("videoProduction", {}).get("jobs", []):
            if job.get("id") == job_id:
                return job
        raise AgentError("视频任务记录不存在。")

    @staticmethod
    def _provider_error(error: Any) -> str:
        if isinstance(error, dict):
            code = str(error.get("code") or "").strip()
            message = str(
                error.get("message") or code or error
            ).strip()
            if VideoPipelineManager._is_policy_violation(code, error):
                return (
                    "视频版权/内容政策审核未通过"
                    + (f"（{code}）" if code else "")
                    + f"：{message}。请移除具体影视作品、受保护角色、"
                    "品牌形象、Logo、在世艺术家风格等指向性元素，"
                    "重新生成并确认原创化提示词后再提交。"
                )
            return f"{code}：{message}" if code and code != message else message
        return str(error)

    @staticmethod
    def _provider_error_code(error: Any) -> str:
        if isinstance(error, dict):
            return str(error.get("code") or "").strip()
        return ""

    @staticmethod
    def _is_policy_violation(code: str, error: Any) -> bool:
        text = (
            f"{code} "
            + (
                json.dumps(error, ensure_ascii=False)
                if isinstance(error, (dict, list))
                else str(error or "")
            )
        ).lower()
        return any(
            marker in text
            for marker in (
                "policyviolation",
                "sensitivecontent",
                "copyright",
                "版权",
                "内容政策",
            )
        )
