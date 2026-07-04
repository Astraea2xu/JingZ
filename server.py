from __future__ import annotations

import argparse
import errno
import json
import mimetypes
import os
import re
import socket
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from agent import (
    AgentError,
    CreativeAgent,
    ProjectStore,
    public_project_view,
    public_video_production_view,
)
from seedance import SeedanceSettings, VideoPipelineManager


ROOT = Path(__file__).resolve().parent
STATIC_ROOT = ROOT / "static"
DATA_ROOT = ROOT / "data" / "projects"
MEDIA_ROOT = ROOT / "data" / "media"


def is_client_disconnect_error(error: BaseException | None) -> bool:
    if isinstance(
        error,
        (BrokenPipeError, ConnectionResetError, ConnectionAbortedError),
    ):
        return True
    if isinstance(error, OSError):
        return (
            getattr(error, "winerror", None) in {10053, 10054, 10058}
            or getattr(error, "errno", None)
            in {errno.EPIPE, errno.ECONNRESET, errno.ECONNABORTED}
        )
    return False


def load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            # The project-local .env is the source of truth. This prevents
            # stale variables inherited from the launching terminal from
            # silently overriding values the user just changed.
            os.environ[key] = value


load_env_file(ROOT / ".env")
AGENT = CreativeAgent(ProjectStore(DATA_ROOT), media_root=MEDIA_ROOT)
SEEDANCE_SETTINGS = SeedanceSettings.from_environment()
VIDEO_PIPELINE = VideoPipelineManager(
    AGENT.store,
    MEDIA_ROOT,
    SEEDANCE_SETTINGS,
)
VIDEO_PIPELINE.resume_incomplete()


class JingzhouHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self) -> None:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_EXCLUSIVEADDRUSE,
                1,
            )
        super().server_bind()

    def handle_error(self, request: object, client_address: object) -> None:
        if is_client_disconnect_error(sys.exc_info()[1]):
            return
        super().handle_error(request, client_address)


class AppHandler(BaseHTTPRequestHandler):
    server_version = "JingzhouAgent/0.2"

    def log_message(self, format: str, *args: object) -> None:
        sys.stdout.write(f"[镜舟] {self.address_string()} - {format % args}\n")

    def _send_json(self, payload: object, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self, max_bytes: int = 1_000_000) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise AgentError("请求长度无效。") from exc
        if length <= 0 or length > max_bytes:
            raise AgentError("请求内容为空或过大。")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentError("请求不是有效 JSON。") from exc

    def _serve_static(self, path: str) -> None:
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        destination = (STATIC_ROOT / relative).resolve()
        if destination != STATIC_ROOT and STATIC_ROOT not in destination.parents:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not destination.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = destination.read_bytes()
        content_type = mimetypes.guess_type(destination.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {
            "application/javascript",
            "application/json",
        }:
            content_type += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _serve_media(self, path: str) -> None:
        relative = path.removeprefix("/media/").lstrip("/")
        destination = (MEDIA_ROOT / relative).resolve()
        if MEDIA_ROOT not in destination.parents or not destination.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        file_size = destination.stat().st_size
        content_type = mimetypes.guess_type(destination.name)[0] or "application/octet-stream"
        start = 0
        end = file_size - 1
        status = HTTPStatus.OK
        range_header = self.headers.get("Range", "")
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not match:
                self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                return
            if match.group(1):
                start = int(match.group(1))
            if match.group(2):
                end = int(match.group(2))
            if not 0 <= start <= end < file_size:
                self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                return
            status = HTTPStatus.PARTIAL_CONTENT
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "private, max-age=3600")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.end_headers()
        with destination.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (
                    BrokenPipeError,
                    ConnectionResetError,
                    ConnectionAbortedError,
                ):
                    return
                except OSError as exc:
                    if is_client_disconnect_error(exc):
                        return
                    raise
                remaining -= len(chunk)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/config":
            self._send_json({**AGENT.config(), **VIDEO_PIPELINE.config()})
            return
        if path == "/api/projects":
            self._send_json({"projects": AGENT.store.list()})
            return
        if path.startswith("/api/projects/"):
            project_id = path.removeprefix("/api/projects/")
            project = AGENT.prepare_project(project_id)
            if not project:
                self._send_json({"error": "项目不存在。"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json(public_project_view(project))
            return
        if path.startswith("/media/"):
            self._serve_media(path)
            return
        self._serve_static(path)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            upload_match = re.fullmatch(
                r"/api/projects/([a-f0-9-]{36})/assets/upload",
                path,
            )
            payload = self._read_json(
                45 * 1024 * 1024 if upload_match else 1_000_000
            )
            if path == "/api/generate":
                self._send_json(AGENT.generate(payload), HTTPStatus.CREATED)
                return
            if path == "/api/images":
                self._send_json(AGENT.generate_image(payload), HTTPStatus.CREATED)
                return
            chat_resolution_match = re.fullmatch(
                (
                    r"/api/projects/([a-f0-9-]{36})/"
                    r"chat/(apply|reject)"
                ),
                path,
            )
            if chat_resolution_match:
                project_id, decision = chat_resolution_match.groups()
                result = AGENT.resolve_chat_proposal(
                    project_id,
                    payload,
                    accept=decision == "apply",
                )
                self._send_json(
                    {
                        **result,
                        "project": public_project_view(result["project"]),
                    }
                )
                return
            chat_match = re.fullmatch(
                r"/api/projects/([a-f0-9-]{36})/chat",
                path,
            )
            if chat_match:
                result = AGENT.chat_edit_project(
                    chat_match.group(1),
                    payload,
                )
                self._send_json(
                    {
                        **result,
                        "project": public_project_view(result["project"]),
                    }
                )
                return
            character_add_match = re.fullmatch(
                r"/api/projects/([a-f0-9-]{36})/characters",
                path,
            )
            if character_add_match:
                project = AGENT.add_character(
                    character_add_match.group(1),
                    payload,
                )
                self._send_json(
                    public_project_view(project),
                    HTTPStatus.CREATED,
                )
                return
            scene_add_match = re.fullmatch(
                r"/api/projects/([a-f0-9-]{36})/scenes",
                path,
            )
            if scene_add_match:
                project = AGENT.add_scene(
                    scene_add_match.group(1),
                    payload,
                )
                self._send_json(
                    public_project_view(project),
                    HTTPStatus.CREATED,
                )
                return
            shot_add_match = re.fullmatch(
                r"/api/projects/([a-f0-9-]{36})/shots",
                path,
            )
            if shot_add_match:
                project = AGENT.add_shot(
                    shot_add_match.group(1),
                    payload,
                )
                self._send_json(
                    public_project_view(project),
                    HTTPStatus.CREATED,
                )
                return
            describe_character_match = re.fullmatch(
                (
                    r"/api/projects/([a-f0-9-]{36})/"
                    r"characters/([a-zA-Z0-9-]+)/describe"
                ),
                path,
            )
            if describe_character_match:
                project = AGENT.describe_character_from_asset(
                    describe_character_match.group(1),
                    describe_character_match.group(2),
                    payload,
                )
                self._send_json(public_project_view(project))
                return
            if upload_match:
                asset = AGENT.upload_project_asset(
                    upload_match.group(1),
                    payload,
                )
                self._send_json(asset, HTTPStatus.CREATED)
                return
            edit_match = re.fullmatch(
                r"/api/projects/([a-f0-9-]{36})/assets/edit",
                path,
            )
            if edit_match:
                asset = AGENT.edit_project_asset(
                    edit_match.group(1),
                    payload,
                )
                self._send_json(asset, HTTPStatus.CREATED)
                return
            asset_match = re.fullmatch(
                r"/api/projects/([a-f0-9-]{36})/assets/generate",
                path,
            )
            if asset_match:
                asset = AGENT.generate_project_asset(
                    asset_match.group(1),
                    payload,
                )
                self._send_json(asset, HTTPStatus.CREATED)
                return
            shot_prompt_match = re.fullmatch(
                (
                    r"/api/projects/([a-f0-9-]{36})/"
                    r"shots/([a-zA-Z0-9-]+)/video-prompt"
                ),
                path,
            )
            if shot_prompt_match:
                project = AGENT.generate_shot_video_prompt(
                    shot_prompt_match.group(1),
                    shot_prompt_match.group(2),
                )
                self._send_json(public_project_view(project))
                return
            shot_video_match = re.fullmatch(
                (
                    r"/api/projects/([a-f0-9-]{36})/"
                    r"shots/([a-zA-Z0-9-]+)/video"
                ),
                path,
            )
            if shot_video_match:
                production = VIDEO_PIPELINE.start_shot(
                    shot_video_match.group(1),
                    shot_video_match.group(2),
                    payload,
                )
                self._send_json(
                    public_video_production_view(production),
                    HTTPStatus.ACCEPTED,
                )
                return
            job_retry_match = re.fullmatch(
                (
                    r"/api/projects/([a-f0-9-]{36})/"
                    r"video-production/jobs/([a-f0-9-]{36})/retry"
                ),
                path,
            )
            if job_retry_match:
                production = VIDEO_PIPELINE.retry_job(
                    job_retry_match.group(1),
                    job_retry_match.group(2),
                )
                self._send_json(
                    public_video_production_view(production),
                    HTTPStatus.ACCEPTED,
                )
                return
            retry_match = re.fullmatch(
                r"/api/projects/([a-f0-9-]{36})/video-production/retry",
                path,
            )
            if retry_match:
                production = VIDEO_PIPELINE.retry_failed(retry_match.group(1))
                self._send_json(
                    public_video_production_view(production),
                    HTTPStatus.ACCEPTED,
                )
                return
            assemble_match = re.fullmatch(
                r"/api/projects/([a-f0-9-]{36})/video-production/assemble",
                path,
            )
            if assemble_match:
                production = VIDEO_PIPELINE.assemble(
                    assemble_match.group(1)
                )
                self._send_json(
                    public_video_production_view(production),
                    HTTPStatus.OK,
                )
                return
            video_match = re.fullmatch(
                r"/api/projects/([a-f0-9-]{36})/video-production",
                path,
            )
            if video_match:
                production = VIDEO_PIPELINE.start(video_match.group(1), payload)
                self._send_json(
                    public_video_production_view(production),
                    HTTPStatus.ACCEPTED,
                )
                return
            self._send_json({"error": "接口不存在。"}, HTTPStatus.NOT_FOUND)
        except AgentError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._send_json(
                {"error": f"服务内部错误：{type(exc).__name__}"},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def do_PATCH(self) -> None:
        path = urlparse(self.path).path
        try:
            match = re.fullmatch(
                r"/api/projects/([a-f0-9-]{36})/field",
                path,
            )
            if not match:
                self._send_json(
                    {"error": "接口不存在。"},
                    HTTPStatus.NOT_FOUND,
                )
                return
            project = AGENT.update_field(
                match.group(1),
                self._read_json(),
            )
            self._send_json(public_project_view(project))
        except AgentError as exc:
            self._send_json(
                {"error": str(exc)},
                HTTPStatus.BAD_REQUEST,
            )
        except Exception as exc:
            self._send_json(
                {"error": f"服务内部错误：{type(exc).__name__}"},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        try:
            asset_match = re.fullmatch(
                r"/api/projects/([a-f0-9-]{36})/assets/([a-zA-Z0-9-]+)",
                path,
            )
            video_asset_match = re.fullmatch(
                (
                    r"/api/projects/([a-f0-9-]{36})/"
                    r"video-assets/([a-zA-Z0-9-]+)"
                ),
                path,
            )
            shot_match = re.fullmatch(
                (
                    r"/api/projects/([a-f0-9-]{36})/"
                    r"shots/([a-zA-Z0-9-]+)"
                ),
                path,
            )
            character_match = re.fullmatch(
                (
                    r"/api/projects/([a-f0-9-]{36})/"
                    r"characters/([a-zA-Z0-9-]+)"
                ),
                path,
            )
            scene_match = re.fullmatch(
                (
                    r"/api/projects/([a-f0-9-]{36})/"
                    r"scenes/([a-zA-Z0-9-]+)"
                ),
                path,
            )
            if asset_match:
                project_id, asset_id = asset_match.groups()
                project = AGENT.store.get(project_id)
                production = (project or {}).get("videoProduction") or {}
                if (
                    VIDEO_PIPELINE.is_active(project_id)
                    or production.get("status") in {"queued", "running"}
                ):
                    raise AgentError(
                        "视频生产进行中，暂时不能删除参考图片。"
                    )
                AGENT.delete_project_asset(project_id, asset_id)
                self._send_json({"deleted": True})
                return
            if video_asset_match:
                production = VIDEO_PIPELINE.delete_video_asset(
                    video_asset_match.group(1),
                    video_asset_match.group(2),
                )
                self._send_json(public_video_production_view(production))
                return
            if shot_match:
                project = AGENT.delete_shot(
                    shot_match.group(1),
                    shot_match.group(2),
                )
                self._send_json(public_project_view(project))
                return
            if character_match:
                project = AGENT.delete_character(
                    character_match.group(1),
                    character_match.group(2),
                )
                self._send_json(public_project_view(project))
                return
            if scene_match:
                project = AGENT.delete_scene(
                    scene_match.group(1),
                    scene_match.group(2),
                )
                self._send_json(public_project_view(project))
                return
            match = re.fullmatch(
                r"/api/projects/([a-f0-9-]{36})",
                path,
            )
            if not match:
                self._send_json(
                    {"error": "接口不存在。"},
                    HTTPStatus.NOT_FOUND,
                )
                return
            project_id = match.group(1)
            project = AGENT.store.get(project_id)
            if not project:
                raise AgentError("项目不存在。")
            production = project.get("videoProduction") or {}
            if (
                VIDEO_PIPELINE.is_active(project_id)
                or production.get("status") in {"queued", "running"}
            ):
                raise AgentError("视频生产进行中，暂时不能删除项目。")
            AGENT.delete_project(project_id)
            self._send_json({"deleted": True})
        except AgentError as exc:
            self._send_json(
                {"error": str(exc)},
                HTTPStatus.BAD_REQUEST,
            )
        except Exception as exc:
            self._send_json(
                {"error": f"服务内部错误：{type(exc).__name__}"},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行镜舟视频故事创作 Agent")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        server = JingzhouHTTPServer(
            (args.host, args.port),
            AppHandler,
        )
    except OSError as exc:
        if getattr(exc, "winerror", None) == 10048:
            raise SystemExit(
                f"端口 {args.port} 已被占用；请先关闭旧的镜舟服务器。"
            ) from exc
        raise
    print(f"镜舟 Agent 已启动：http://{args.host}:{args.port}", flush=True)
    print(
        f"视频 API 日志已启用：{VIDEO_PIPELINE.settings.base_url}",
        flush=True,
    )
    print("按 Ctrl+C 停止。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止镜舟 Agent…")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
