import io
import json
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from agent import AgentError, ProjectStore
from seedance import (
    SeedanceSettings,
    SeedanceProvider,
    VideoPipelineManager,
    build_jobs,
    build_seedance_prompt,
    split_duration,
)


class FakeSeedanceProvider:
    def __init__(self):
        self.created = []
        self.counter = 0

    def create_task(self, **kwargs):
        self.counter += 1
        self.created.append(kwargs)
        return {"id": f"task-{self.counter}"}

    def get_task(self, task_id):
        return {
            "id": task_id,
            "status": "succeeded",
            "content": {
                "video_url": f"https://example.test/{task_id}.mp4",
                "last_frame_url": f"https://example.test/{task_id}.png",
            },
            "usage": {"total_tokens": 100},
        }

    def download(self, url, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"fake-media")


class FailingDownloadProvider(FakeSeedanceProvider):
    def download(self, url, destination):
        raise AgentError(
            "无法下载视频文件：SSL: UNEXPECTED_EOF_WHILE_READING"
        )

class PolicyViolationProvider(FakeSeedanceProvider):
    def get_task(self, task_id):
        return {
            "id": task_id,
            "status": "failed",
            "error": {
                "code": (
                    "OutputVideoSensitiveContentDetected."
                    "PolicyViolation"
                ),
                "message": (
                    "The output video may be related to "
                    "copyright restrictions."
                ),
            },
        }


class FakeHTTPResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class FakeBinaryResponse:
    def __init__(self, events, *, status=200, content_length=0):
        self.events = list(events)
        self.status = status
        self.headers = {
            "Content-Type": "video/mp4",
            "Content-Length": str(content_length),
        }

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size=-1):
        if not self.events:
            return b""
        value = self.events.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


def sample_project(project_id="11111111-1111-1111-1111-111111111111"):
    return {
        "id": project_id,
        "title": "测试短片",
        "mode": "short_drama",
        "modeLabel": "短剧 Agent",
        "createdAt": "2026-07-02T00:00:00+00:00",
        "updatedAt": "2026-07-02T00:00:00+00:00",
        "brief": {
            "aspectRatio": "9:16",
            "durationSeconds": 8,
        },
        "characters": [
            {
                "id": "char-1",
                "name": "林舟",
                "visualIdentity": "黑色短发，墨绿色夹克",
                "personality": "克制而敏锐",
            }
        ],
        "storyboard": [
            {
                "shot": 1,
                "duration": 8,
                "scene": "雨夜街道",
                "action": "林舟打开一封来自未来的信",
                "camera": "中近景缓慢推近",
                "visualPrompt": "电影感雨夜，霓虹反光",
                "videoPrompt": "人物先停顿再拆开信封",
                "dialogue": "这封信不该存在。",
                "audio": "雨声与低频脉冲",
                "characterIds": ["char-1"],
                "continuity": "保持墨绿色夹克",
            }
        ],
    }


class SeedanceHelpersTests(unittest.TestCase):
    def test_split_duration_respects_seedance_limits(self):
        self.assertEqual(split_duration(3), [4])
        self.assertEqual(split_duration(15), [15])
        self.assertEqual(split_duration(16), [8, 8])
        self.assertEqual(sum(split_duration(31)), 31)
        self.assertTrue(all(4 <= item <= 15 for item in split_duration(31)))

    def test_prompt_contains_shot_and_character_anchors(self):
        project = sample_project()
        project["assets"] = [
            {
                "id": "character-reference",
                "ownerType": "character",
                "prompt": "短黑发、绿色夹克、银色耳钉",
                "url": "/media/project/assets/character.png",
            },
            {
                "id": "scene-reference",
                "ownerType": "scene",
                "prompt": "雨夜便利店、红色雨棚、左侧电话亭",
                "url": "/media/project/assets/scene.png",
            },
        ]
        project["storyboard"][0]["referenceAssetIds"] = [
            "character-reference",
            "scene-reference",
        ]
        prompt = build_seedance_prompt(
            project,
            project["storyboard"][0],
            part=1,
            part_count=1,
        )
        self.assertIn("雨夜街道", prompt)
        self.assertIn("墨绿色夹克", prompt)
        self.assertIn("这封信不该存在", prompt)
        self.assertIn("角色参考图是不可改变的身份锚点", prompt)
        self.assertIn("不得换脸、换装", prompt)
        self.assertIn("场景参考图是不可改变的环境锚点", prompt)
        self.assertIn("空间布局", prompt)

    def test_confirmed_complete_video_prompt_is_used(self):
        project = sample_project()
        project["storyboard"][0]["completeVideoPrompt"] = (
            "这是用户确认后的完整视频提示词，固定人物与雨夜空间。"
        )
        prompt = build_seedance_prompt(
            project,
            project["storyboard"][0],
            part=1,
            part_count=1,
        )
        self.assertTrue(
            prompt.startswith("这是用户确认后的完整视频提示词")
        )
        self.assertNotIn("主体与动作：", prompt)

    def test_build_jobs_splits_long_shots(self):
        project = sample_project()
        project["storyboard"][0]["duration"] = 18
        jobs = build_jobs(project)
        self.assertEqual([job["duration"] for job in jobs], [9, 9])
        self.assertEqual(jobs[1]["part"], 2)

    def test_build_jobs_collects_references_for_multiple_characters(self):
        project = sample_project()
        project["characters"].append(
            {
                "id": "char-2",
                "name": "顾言",
                "visualIdentity": "银灰长发，黑色风衣",
                "personality": "冷静",
                "referenceImageIds": ["asset-char-2"],
            }
        )
        project["characters"][0]["referenceImageIds"] = ["asset-char-1"]
        project["storyboard"][0]["characterIds"] = ["char-1", "char-2"]
        project["storyboard"][0]["referenceAssetIds"] = ["asset-scene"]
        project["assets"] = [
            {
                "id": "asset-char-1",
                "ownerType": "character",
                "ownerId": "char-1",
                "prompt": "林舟角色参考",
                "url": "/media/project/linzhou.png",
            },
            {
                "id": "asset-char-2",
                "ownerType": "character",
                "ownerId": "char-2",
                "prompt": "顾言角色参考",
                "url": "/media/project/guyan.png",
            },
            {
                "id": "asset-scene",
                "ownerType": "scene",
                "ownerId": "scene-1",
                "prompt": "雨夜街道场景参考",
                "url": "/media/project/scene.png",
            },
        ]
        jobs = build_jobs(project)
        self.assertEqual(
            jobs[0]["referenceAssetIds"],
            ["asset-scene", "asset-char-1", "asset-char-2"],
        )
        self.assertIn("角色“林舟”=林舟角色参考", jobs[0]["prompt"])
        self.assertIn("角色“顾言”=顾言角色参考", jobs[0]["prompt"])
        self.assertIn("不得交换脸、发型、服装", jobs[0]["prompt"])

    def test_build_jobs_limits_reference_images_to_nine(self):
        project = sample_project()
        project["assets"] = [
            {
                "id": f"asset-{index}",
                "ownerType": "character",
                "ownerId": "char-1",
                "prompt": f"角色参考 {index}",
                "url": f"/media/project/assets/{index}.png",
            }
            for index in range(12)
        ]
        project["storyboard"][0]["referenceAssetIds"] = [
            f"asset-{index}" for index in range(12)
        ]
        jobs = build_jobs(project)
        self.assertEqual(
            jobs[0]["referenceAssetIds"],
            [f"asset-{index}" for index in range(9)],
        )
        self.assertEqual(len(jobs[0]["referenceImageUrls"]), 9)

    def test_provider_uses_seedance_json_url_without_duplicate_path(self):
        settings = SeedanceSettings(
            base_url="https://api.177911.com/v1/video/generations",
            api_key="test-key",
            model="doubao-seedance-2-0-260128",
            poll_interval=10,
            request_timeout=30,
            download_max_bytes=10 * 1024 * 1024,
            ffmpeg_path="",
        )
        provider = SeedanceProvider(settings)
        with patch(
            "urllib.request.urlopen",
            return_value=FakeHTTPResponse({"id": "cgt-test"}),
        ) as mocked, patch("builtins.print") as mocked_print:
            result = provider.create_task(
                prompt="雨夜中的人物缓慢抬头",
                duration=5,
                ratio="9:16",
                resolution="720p",
                generate_audio=True,
                watermark=False,
                reference_images=[
                    "data:image/png;base64,Y2hhcmFjdGVy",
                    "https://example.test/scene.png",
                ],
            )
        request = mocked.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(result["id"], "cgt-test")
        self.assertEqual(
            request.full_url,
            "https://api.177911.com/v1/video/generations",
        )
        self.assertEqual(
            request.get_header("Content-type"),
            "application/json",
        )
        self.assertEqual(
            payload["model"],
            "doubao-seedance-2-0-260128",
        )
        self.assertEqual(payload["duration"], 5)
        self.assertEqual(payload["size"], "720p")
        self.assertEqual(payload["metadata"]["ratio"], "9:16")
        self.assertTrue(payload["metadata"]["generate_audio"])
        self.assertEqual(
            [
                item["role"]
                for item in payload["metadata"]["content"]
            ],
            ["reference_image", "reference_image"],
        )
        self.assertNotIn("/contents/generations/tasks", request.full_url)
        terminal_log = "\n".join(
            str(call.args[0]) for call in mocked_print.call_args_list
        )
        self.assertIn("[视频 API]", terminal_log)
        self.assertIn(
            "POST https://api.177911.com/v1/video/generations",
            terminal_log,
        )
        self.assertIn("HTTP 200", terminal_log)
        self.assertIn("cgt-test", terminal_log)
        self.assertNotIn("test-key", terminal_log)

    def test_provider_logs_http_error_response(self):
        settings = SeedanceSettings(
            base_url="https://open.177911.com/v1/videos",
            api_key="secret-test-key",
            model="seedance-1.0-mini",
            poll_interval=10,
            request_timeout=30,
            download_max_bytes=10 * 1024 * 1024,
            ffmpeg_path="",
        )
        provider = SeedanceProvider(settings)
        error = urllib.error.HTTPError(
            settings.base_url,
            401,
            "Unauthorized",
            {},
            io.BytesIO(
                json.dumps(
                    {
                        "error": {
                            "message": "无效的令牌",
                            "request_id": "request-test",
                        }
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
            ),
        )
        with patch(
            "urllib.request.urlopen",
            side_effect=error,
        ), patch("builtins.print") as mocked_print:
            with self.assertRaisesRegex(Exception, "无效的令牌"):
                provider.create_task(
                    prompt="测试",
                    duration=4,
                    ratio="9:16",
                    resolution="720p",
                    generate_audio=True,
                    watermark=False,
                )
        terminal_log = "\n".join(
            str(call.args[0]) for call in mocked_print.call_args_list
        )
        self.assertIn("HTTP 401", terminal_log)
        self.assertIn("request-test", terminal_log)
        self.assertNotIn("secret-test-key", terminal_log)

    def test_provider_submits_seedance_first_and_last_frames(self):
        settings = SeedanceSettings(
            base_url="https://relay.test/v1/video/generations",
            api_key="test-key",
            model="doubao-seedance-2-0-260128",
            poll_interval=10,
            request_timeout=30,
            download_max_bytes=10 * 1024 * 1024,
            ffmpeg_path="",
        )
        with patch(
            "urllib.request.urlopen",
            return_value=FakeHTTPResponse({"task_id": "task-fixed"}),
        ) as mocked, patch("builtins.print"):
            result = SeedanceProvider(settings).create_task(
                prompt="从雨夜街口走进车站",
                duration=8,
                ratio="16:9",
                resolution="1080p",
                generate_audio=False,
                watermark=True,
                reference_images=["https://example.test/ignored.png"],
                first_frame="https://example.test/first.png",
                last_frame="https://example.test/last.png",
            )
        payload = json.loads(
            mocked.call_args.args[0].data.decode("utf-8")
        )
        self.assertEqual(result["id"], "task-fixed")
        self.assertEqual(
            [
                item["role"]
                for item in payload["metadata"]["content"]
            ],
            ["reference_image", "first_frame", "last_frame"],
        )

    def test_provider_rejects_more_than_nine_reference_images(self):
        settings = SeedanceSettings(
            base_url="https://relay.test/v1/video/generations",
            api_key="test-key",
            model="doubao-seedance-2-0-260128",
            poll_interval=10,
            request_timeout=30,
            download_max_bytes=10 * 1024 * 1024,
            ffmpeg_path="",
        )
        with self.assertRaisesRegex(Exception, "最多提交 9 张"):
            SeedanceProvider(settings).create_task(
                prompt="测试多参考图",
                duration=5,
                ratio="9:16",
                resolution="720p",
                generate_audio=True,
                watermark=False,
                reference_images=[
                    f"https://example.test/reference-{index}.png"
                    for index in range(10)
                ],
            )

    def test_provider_does_not_adapt_base_url(self):
        settings = SeedanceSettings(
            base_url="https://relay.test/custom/video-endpoint/",
            api_key="test-key",
            model="seedance-1.0-mini",
            poll_interval=10,
            request_timeout=30,
            download_max_bytes=10 * 1024 * 1024,
            ffmpeg_path="",
        )
        self.assertEqual(
            SeedanceProvider(settings).collection_url,
            "https://relay.test/custom/video-endpoint/",
        )

    def test_get_task_retries_ssl_eof(self):
        settings = SeedanceSettings(
            base_url="https://relay.test/v1/videos",
            api_key="test-key",
            model="seedance-1.0-mini",
            poll_interval=10,
            request_timeout=30,
            download_max_bytes=10 * 1024 * 1024,
            ffmpeg_path="",
            request_retries=2,
        )
        provider = SeedanceProvider(settings)
        with patch(
            "urllib.request.urlopen",
            side_effect=[
                urllib.error.URLError(
                    "SSL: UNEXPECTED_EOF_WHILE_READING"
                ),
                FakeHTTPResponse(
                    {"id": "task-test", "status": "completed"}
                ),
            ],
        ) as mocked, patch("time.sleep"), patch("builtins.print") as printed:
            result = provider.get_task("task-test")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(mocked.call_count, 2)
        terminal_log = "\n".join(
            str(call.args[0]) for call in printed.call_args_list
        )
        self.assertIn('"retrying": true', terminal_log)
        self.assertIn('"attempt": 2', terminal_log)

    def test_get_task_unwraps_nested_policy_failure(self):
        settings = SeedanceSettings(
            base_url="https://relay.test/v1/video/generations",
            api_key="test-key",
            model="seedance",
            poll_interval=10,
            request_timeout=30,
            download_max_bytes=10 * 1024 * 1024,
            ffmpeg_path="",
        )
        payload = {
            "code": "success",
            "data": {
                "task_id": "cgt-test",
                "status": "FAILURE",
                "fail_reason": "task failed",
                "data": {
                    "id": "cgt-test",
                    "status": "failed",
                    "error": {
                        "code": (
                            "OutputVideoSensitiveContentDetected."
                            "PolicyViolation"
                        ),
                        "message": (
                            "The output video may be related to "
                            "copyright restrictions."
                        ),
                    },
                },
            },
        }
        with patch(
            "urllib.request.urlopen",
            return_value=FakeHTTPResponse(payload),
        ), patch("builtins.print"):
            result = SeedanceProvider(settings).get_task("cgt-test")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            result["error"]["code"],
            "OutputVideoSensitiveContentDetected.PolicyViolation",
        )

    def test_download_retries_ssl_eof_and_resumes_with_range(self):
        settings = SeedanceSettings(
            base_url="https://relay.test/v1/videos",
            api_key="test-key",
            model="seedance-1.0-mini",
            poll_interval=10,
            request_timeout=30,
            download_max_bytes=10 * 1024 * 1024,
            ffmpeg_path="",
            download_retries=2,
        )
        first = FakeBinaryResponse(
            [
                b"12345",
                urllib.error.URLError(
                    "SSL: UNEXPECTED_EOF_WHILE_READING"
                ),
            ],
            content_length=10,
        )
        second = FakeBinaryResponse(
            [b"67890", b""],
            status=206,
            content_length=5,
        )
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "clip.mp4"
            with patch(
                "urllib.request.urlopen",
                side_effect=[first, second],
            ) as mocked, patch("time.sleep"), patch("builtins.print"):
                SeedanceProvider(settings).download(
                    "https://cdn.test/clip.mp4",
                    destination,
                )
            self.assertEqual(destination.read_bytes(), b"1234567890")
            self.assertEqual(mocked.call_count, 2)
            second_request = mocked.call_args_list[1].args[0]
            self.assertEqual(second_request.get_header("Range"), "bytes=5-")


class VideoPipelineTests(unittest.TestCase):
    def test_policy_failure_marks_prompt_stale_and_preserves_details(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ProjectStore(root / "projects")
            project = sample_project()
            project["storyboard"][0]["id"] = "shot-1"
            project["storyboard"][0]["completeVideoPrompt"] = (
                "模仿某具体影视作品和角色"
            )
            project["storyboard"][0]["completeVideoPromptStale"] = False
            store.save(project)
            manager = VideoPipelineManager(
                store,
                root / "media",
                settings=SeedanceSettings(
                    base_url="https://relay.test/v1/video/generations",
                    api_key="test-key",
                    model="seedance",
                    poll_interval=1,
                    request_timeout=10,
                    download_max_bytes=10 * 1024 * 1024,
                    ffmpeg_path="",
                ),
                provider=PolicyViolationProvider(),
                sleep=lambda _: None,
            )
            manager.start(
                project["id"],
                {
                    "resolution": "720p",
                    "ratio": "9:16",
                    "generateAudio": True,
                    "continuity": True,
                },
            )
            deadline = time.monotonic() + 3
            result = store.get(project["id"])
            while (
                result["videoProduction"]["status"]
                in {"queued", "running"}
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
                result = store.get(project["id"])
            job = result["videoProduction"]["jobs"][0]
            self.assertEqual(job["status"], "failed")
            self.assertTrue(job["policyViolation"])
            self.assertIn("版权/内容政策审核未通过", job["error"])
            self.assertTrue(
                result["storyboard"][0]["completeVideoPromptStale"]
            )
            self.assertEqual(
                result["storyboard"][0]["videoPolicyViolation"]["code"],
                "OutputVideoSensitiveContentDetected.PolicyViolation",
            )

    def test_stale_production_cannot_retry_old_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ProjectStore(root / "projects")
            project = sample_project()
            job = build_jobs(project)[0]
            job["status"] = "failed"
            project["videoProduction"] = {
                "id": "stale-production",
                "status": "failed",
                "stale": True,
                "settings": {
                    "model": "seedance",
                    "resolution": "720p",
                    "ratio": "9:16",
                    "generateAudio": True,
                    "watermark": False,
                    "continuity": True,
                },
                "jobs": [job],
            }
            store.save(project)
            manager = VideoPipelineManager(
                store,
                root / "media",
                settings=SeedanceSettings(
                    base_url="https://relay.test/v1/video/generations",
                    api_key="test-key",
                    model="seedance",
                    poll_interval=1,
                    request_timeout=10,
                    download_max_bytes=10 * 1024 * 1024,
                    ffmpeg_path="",
                ),
                provider=FakeSeedanceProvider(),
                sleep=lambda _: None,
            )
            with self.assertRaisesRegex(
                AgentError,
                "按最新分镜重新提交",
            ):
                manager.retry_failed(project["id"])

    def test_bound_reference_is_submitted_when_continuity_is_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ProjectStore(root / "projects")
            project = sample_project()
            asset_id = "character-reference"
            project["assets"] = [
                {
                    "id": asset_id,
                    "ownerType": "character",
                    "ownerId": "char-1",
                    "prompt": "固定人物脸型、短黑发和绿色夹克",
                    "url": f"/media/{project['id']}/assets/character.png",
                }
            ]
            project["storyboard"][0]["referenceAssetIds"] = [asset_id]
            store.save(project)
            reference_path = (
                root
                / "media"
                / project["id"]
                / "assets"
                / "character.png"
            )
            reference_path.parent.mkdir(parents=True)
            reference_path.write_bytes(b"\x89PNG\r\n\x1a\nreference")
            settings = SeedanceSettings(
                base_url="https://relay.test/v1/videos",
                api_key="test-key",
                model="seedance-1.0-mini",
                poll_interval=1,
                request_timeout=10,
                download_max_bytes=10 * 1024 * 1024,
                ffmpeg_path="",
            )
            provider = FakeSeedanceProvider()
            manager = VideoPipelineManager(
                store,
                root / "media",
                settings=settings,
                provider=provider,
                sleep=lambda _: None,
            )
            manager.start(
                project["id"],
                {
                    "resolution": "720p",
                    "ratio": "9:16",
                    "generateAudio": True,
                    "continuity": False,
                },
            )
            deadline = time.monotonic() + 3
            while not provider.created and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(provider.created)
            self.assertTrue(
                provider.created[0]["reference_images"][0].startswith(
                    "data:image/png;base64,"
                )
            )
            self.assertEqual(provider.created[0]["first_frame"], "")
            result = store.get(project["id"])
            while (
                result["videoProduction"]["status"] in {"queued", "running"}
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
                result = store.get(project["id"])
            self.assertEqual(
                result["videoProduction"]["status"],
                "succeeded",
            )
            submitted_job = result["videoProduction"]["jobs"][0]
            self.assertEqual(
                submitted_job["inputReferenceSource"],
                "reference_images",
            )
            self.assertEqual(
                submitted_job["inputReferenceAssetId"],
                asset_id,
            )
            self.assertEqual(
                submitted_job["submittedReferenceAssetIds"],
                [asset_id],
            )

    def test_pipeline_creates_downloads_and_finishes_single_clip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ProjectStore(root / "projects")
            project = sample_project()
            store.save(project)
            settings = SeedanceSettings(
                base_url="https://example.test/api/v3",
                api_key="test-key",
                model="doubao-seedance-2-0",
                poll_interval=1,
                request_timeout=10,
                download_max_bytes=10 * 1024 * 1024,
                ffmpeg_path="",
            )
            provider = FakeSeedanceProvider()
            manager = VideoPipelineManager(
                store,
                root / "media",
                settings=settings,
                provider=provider,
                sleep=lambda _: None,
            )
            manager.start(
                project["id"],
                {
                    "resolution": "720p",
                    "ratio": "9:16",
                    "generateAudio": True,
                    "continuity": True,
                },
            )

            deadline = time.monotonic() + 3
            result = store.get(project["id"])
            while (
                result["videoProduction"]["status"] in {"queued", "running"}
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
                result = store.get(project["id"])

            production = result["videoProduction"]
            self.assertEqual(production["status"], "succeeded")
            self.assertEqual(production["completedJobs"], 1)
            self.assertTrue(production["finalVideoUrl"].endswith("clip-001.mp4"))
            self.assertTrue((root / "media" / project["id"] / "clip-001.mp4").is_file())
            self.assertTrue(provider.created[0]["generate_audio"])

    def test_manual_assemble_uses_successful_clips_in_job_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ProjectStore(root / "projects")
            project = sample_project()
            media_directory = root / "media" / project["id"]
            media_directory.mkdir(parents=True)
            (media_directory / "clip-001.mp4").write_bytes(b"first")
            (media_directory / "clip-002.mp4").write_bytes(b"second")
            project["videoProduction"] = {
                "id": "production-for-assembly",
                "status": "succeeded",
                "jobs": [
                    {
                        "id": "job-1",
                        "label": "镜头 1",
                        "status": "succeeded",
                        "localVideoUrl": (
                            f"/media/{project['id']}/clip-001.mp4"
                        ),
                        "sourceShot": 1,
                    },
                    {
                        "id": "job-2",
                        "label": "镜头 2",
                        "status": "succeeded",
                        "localVideoUrl": (
                            f"/media/{project['id']}/clip-002.mp4"
                        ),
                        "sourceShot": 2,
                    },
                ],
                "manifestUrl": "",
                "finalVideoUrl": "",
                "assembly": {"status": "pending", "message": ""},
            }
            store.save(project)
            settings = SeedanceSettings(
                base_url="https://relay.test/v1/videos",
                api_key="test-key",
                model="seedance-model",
                poll_interval=1,
                request_timeout=10,
                download_max_bytes=10 * 1024 * 1024,
                ffmpeg_path="ffmpeg",
            )
            manager = VideoPipelineManager(
                store,
                root / "media",
                settings=settings,
                provider=FakeSeedanceProvider(),
                sleep=lambda _: None,
            )
            with patch.object(
                manager,
                "_assemble",
                return_value=(
                    f"/media/{project['id']}/final.mp4",
                    {
                        "status": "succeeded",
                        "message": "所有镜头已合成为完整 MP4。",
                    },
                ),
            ) as mocked:
                production = manager.assemble(project["id"])

            assembled_jobs = mocked.call_args.args[1]
            self.assertEqual(
                [job["id"] for job in assembled_jobs],
                ["job-1", "job-2"],
            )
            self.assertEqual(
                production["finalVideoUrl"],
                f"/media/{project['id']}/final.mp4",
            )
            self.assertEqual(
                production["assembly"]["status"],
                "succeeded",
            )
            self.assertTrue((media_directory / "manifest.json").is_file())

    def test_delete_video_asset_removes_files_and_invalidates_final_cut(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ProjectStore(root / "projects")
            project = sample_project()
            media_directory = root / "media" / project["id"]
            media_directory.mkdir(parents=True)
            for name in (
                "clip-001.mp4",
                "clip-001-last.png",
                "clip-002.mp4",
                "final.mp4",
                "manifest.json",
            ):
                (media_directory / name).write_bytes(b"content")
            prefix = f"/media/{project['id']}"
            project["videoProduction"] = {
                "id": "production-assets",
                "status": "succeeded",
                "completedJobs": 2,
                "failedJobs": 0,
                "jobs": [
                    {
                        "id": "job-delete",
                        "label": "镜头 1",
                        "status": "succeeded",
                        "videoUrl": "https://cdn.test/clip-1.mp4",
                        "lastFrameUrl": "https://cdn.test/frame-1.png",
                        "localVideoUrl": f"{prefix}/clip-001.mp4",
                        "localLastFrameUrl": (
                            f"{prefix}/clip-001-last.png"
                        ),
                    },
                    {
                        "id": "job-keep",
                        "label": "镜头 2",
                        "status": "succeeded",
                        "localVideoUrl": f"{prefix}/clip-002.mp4",
                    },
                ],
                "manifestUrl": f"{prefix}/manifest.json",
                "finalVideoUrl": f"{prefix}/final.mp4",
                "assembly": {"status": "succeeded", "message": "完成"},
            }
            store.save(project)
            manager = VideoPipelineManager(
                store,
                root / "media",
                settings=SeedanceSettings(
                    base_url="https://relay.test/v1/videos",
                    api_key="test-key",
                    model="seedance-model",
                    poll_interval=1,
                    request_timeout=10,
                    download_max_bytes=10 * 1024 * 1024,
                    ffmpeg_path="",
                ),
                provider=FakeSeedanceProvider(),
                sleep=lambda _: None,
            )
            production = manager.delete_video_asset(
                project["id"],
                "job-delete",
            )
            deleted_job = production["jobs"][0]
            self.assertEqual(deleted_job["status"], "failed")
            self.assertEqual(deleted_job["localVideoUrl"], "")
            self.assertEqual(production["status"], "partial")
            self.assertEqual(production["completedJobs"], 1)
            self.assertEqual(production["finalVideoUrl"], "")
            self.assertFalse((media_directory / "clip-001.mp4").exists())
            self.assertFalse(
                (media_directory / "clip-001-last.png").exists()
            )
            self.assertFalse((media_directory / "final.mp4").exists())
            self.assertFalse((media_directory / "manifest.json").exists())
            self.assertTrue((media_directory / "clip-002.mp4").exists())

    def test_remote_video_url_is_kept_when_local_download_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ProjectStore(root / "projects")
            project = sample_project()
            store.save(project)
            settings = SeedanceSettings(
                base_url="https://relay.test/v1/videos",
                api_key="test-key",
                model="seedance-1.0-mini",
                poll_interval=1,
                request_timeout=10,
                download_max_bytes=10 * 1024 * 1024,
                ffmpeg_path="",
            )
            manager = VideoPipelineManager(
                store,
                root / "media",
                settings=settings,
                provider=FailingDownloadProvider(),
                sleep=lambda _: None,
            )
            manager.start(
                project["id"],
                {
                    "resolution": "720p",
                    "ratio": "9:16",
                    "generateAudio": True,
                    "continuity": True,
                },
            )
            deadline = time.monotonic() + 3
            result = store.get(project["id"])
            while (
                result["videoProduction"]["status"] in {"queued", "running"}
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
                result = store.get(project["id"])
            job = result["videoProduction"]["jobs"][0]
            self.assertEqual(job["status"], "failed")
            self.assertEqual(
                job["videoUrl"],
                "https://example.test/task-1.mp4",
            )
            self.assertEqual(job["localVideoUrl"], "")

    def test_retry_failed_only_resubmits_unsuccessful_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ProjectStore(root / "projects")
            project = sample_project()
            store.save(project)
            settings = SeedanceSettings(
                base_url="https://open.177911.com/v1/videos",
                api_key="test-key",
                model="seedance-1.0-mini",
                poll_interval=1,
                request_timeout=10,
                download_max_bytes=10 * 1024 * 1024,
                ffmpeg_path="",
            )
            provider = FakeSeedanceProvider()
            manager = VideoPipelineManager(
                store,
                root / "media",
                settings=settings,
                provider=provider,
                sleep=lambda _: None,
            )
            production = {
                "id": "old-production",
                "status": "failed",
                "settings": {
                    "model": settings.model,
                    "resolution": "720p",
                    "ratio": "9:16",
                    "generateAudio": True,
                    "watermark": False,
                    "continuity": True,
                    "priority": 0,
                },
                "jobs": build_jobs(project),
                "completedJobs": 0,
                "failedJobs": 1,
                "totalJobs": 1,
                "manifestUrl": "",
                "finalVideoUrl": "",
                "assembly": {"status": "skipped", "message": ""},
                "error": "old failure",
            }
            production["jobs"][0]["status"] = "failed"
            production["jobs"][0]["taskId"] = "old-task"
            production["jobs"][0]["error"] = "HTTP 404"

            def assign(current):
                current["videoProduction"] = production

            store.mutate(project["id"], assign)
            retried = manager.retry_failed(project["id"])
            self.assertEqual(retried["retryOf"], "old-production")
            self.assertEqual(retried["jobs"][0]["taskId"], "")
            self.assertEqual(retried["jobs"][0]["error"], "")

            deadline = time.monotonic() + 3
            result = store.get(project["id"])
            while (
                result["videoProduction"]["status"] in {"queued", "running"}
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
                result = store.get(project["id"])
            self.assertEqual(result["videoProduction"]["status"], "succeeded")
            self.assertEqual(len(provider.created), 1)

    def test_retry_download_failure_reuses_completed_task(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ProjectStore(root / "projects")
            project = sample_project()
            store.save(project)
            settings = SeedanceSettings(
                base_url="https://relay.test/v1/videos",
                api_key="test-key",
                model="seedance-1.0-mini",
                poll_interval=1,
                request_timeout=10,
                download_max_bytes=10 * 1024 * 1024,
                ffmpeg_path="",
            )
            provider = FakeSeedanceProvider()
            manager = VideoPipelineManager(
                store,
                root / "media",
                settings=settings,
                provider=provider,
                sleep=lambda _: None,
            )
            production = {
                "id": "download-failed-production",
                "status": "failed",
                "settings": {
                    "model": settings.model,
                    "resolution": "720p",
                    "ratio": "9:16",
                    "generateAudio": True,
                    "watermark": False,
                    "continuity": True,
                    "priority": 0,
                },
                "jobs": build_jobs(project),
                "completedJobs": 0,
                "failedJobs": 1,
                "totalJobs": 1,
                "manifestUrl": "",
                "finalVideoUrl": "",
                "assembly": {"status": "skipped", "message": ""},
                "error": "download failed",
            }
            production["jobs"][0]["status"] = "failed"
            production["jobs"][0]["taskId"] = "completed-task"
            production["jobs"][0]["error"] = (
                "无法下载视频文件：SSL: UNEXPECTED_EOF_WHILE_READING"
            )

            def assign(current):
                current["videoProduction"] = production

            store.mutate(project["id"], assign)
            retried = manager.retry_failed(project["id"])
            self.assertEqual(retried["jobs"][0]["taskId"], "completed-task")
            self.assertEqual(retried["jobs"][0]["retryMode"], "download")

            deadline = time.monotonic() + 3
            result = store.get(project["id"])
            while (
                result["videoProduction"]["status"] in {"queued", "running"}
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
                result = store.get(project["id"])
            self.assertEqual(result["videoProduction"]["status"], "succeeded")
            self.assertEqual(provider.created, [])

    def test_retry_status_network_failure_reuses_task_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ProjectStore(root / "projects")
            project = sample_project()
            jobs = build_jobs(project)
            jobs[0]["status"] = "failed"
            jobs[0]["taskId"] = "existing-task"
            jobs[0]["error"] = (
                "无法连接视频 API（已尝试 4 次）："
                "SSL: UNEXPECTED_EOF_WHILE_READING"
            )
            project["videoProduction"] = {
                "id": "network-failed-production",
                "status": "failed",
                "settings": {
                    "model": "seedance-1.0-mini",
                    "resolution": "720p",
                    "ratio": "9:16",
                    "generateAudio": True,
                    "watermark": False,
                    "continuity": True,
                    "priority": 0,
                },
                "jobs": jobs,
                "completedJobs": 0,
                "failedJobs": 1,
                "totalJobs": 1,
                "manifestUrl": "",
                "finalVideoUrl": "",
                "assembly": {"status": "skipped", "message": ""},
                "error": "network failure",
            }
            store.save(project)
            settings = SeedanceSettings(
                base_url="https://relay.test/v1/videos",
                api_key="test-key",
                model="seedance-1.0-mini",
                poll_interval=1,
                request_timeout=10,
                download_max_bytes=10 * 1024 * 1024,
                ffmpeg_path="",
            )
            provider = FakeSeedanceProvider()
            manager = VideoPipelineManager(
                store,
                root / "media",
                settings=settings,
                provider=provider,
                sleep=lambda _: None,
            )
            retried = manager.retry_failed(project["id"])
            self.assertEqual(retried["jobs"][0]["taskId"], "existing-task")
            self.assertEqual(retried["jobs"][0]["retryMode"], "resume")

            deadline = time.monotonic() + 3
            result = store.get(project["id"])
            while (
                result["videoProduction"]["status"] in {"queued", "running"}
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
                result = store.get(project["id"])
            self.assertEqual(result["videoProduction"]["status"], "succeeded")
            self.assertEqual(provider.created, [])

    def test_retry_job_only_regenerates_selected_clip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ProjectStore(root / "projects")
            project = sample_project()
            project["storyboard"][0]["id"] = "shot-1"
            second_shot = dict(project["storyboard"][0])
            second_shot["shot"] = 2
            second_shot["id"] = "shot-2"
            second_shot["action"] = "林舟转身离开"
            project["storyboard"].append(second_shot)
            jobs = build_jobs(project)
            for index, job in enumerate(jobs, start=1):
                job["status"] = "succeeded"
                job["taskId"] = f"old-task-{index}"
                job["localVideoUrl"] = (
                    f"/media/{project['id']}/clip-{index:03d}.mp4"
                )
            project["videoProduction"] = {
                "id": "old-production",
                "status": "succeeded",
                "settings": {
                    "model": "seedance-1.0-mini",
                    "resolution": "720p",
                    "ratio": "9:16",
                    "generateAudio": True,
                    "watermark": False,
                    "continuity": True,
                    "priority": 0,
                },
                "jobs": jobs,
                "completedJobs": 2,
                "failedJobs": 0,
                "totalJobs": 2,
                "manifestUrl": "",
                "finalVideoUrl": "",
                "assembly": {"status": "succeeded", "message": ""},
                "error": "",
            }
            store.save(project)
            settings = SeedanceSettings(
                base_url="https://relay.test/v1/videos",
                api_key="test-key",
                model="seedance-1.0-mini",
                poll_interval=1,
                request_timeout=10,
                download_max_bytes=10 * 1024 * 1024,
                ffmpeg_path="",
            )
            provider = FakeSeedanceProvider()
            manager = VideoPipelineManager(
                store,
                root / "media",
                settings=settings,
                provider=provider,
                sleep=lambda _: None,
            )
            selected_id = jobs[0]["id"]
            retried = manager.retry_job(project["id"], selected_id)
            self.assertEqual(retried["retryScope"], "single")
            self.assertEqual(retried["retryJobId"], selected_id)

            deadline = time.monotonic() + 3
            result = store.get(project["id"])
            while (
                result["videoProduction"]["status"] in {"queued", "running"}
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
                result = store.get(project["id"])
            self.assertEqual(len(provider.created), 1)
            selected = next(
                job
                for job in result["videoProduction"]["jobs"]
                if job["id"] == selected_id
            )
            untouched = next(
                job
                for job in result["videoProduction"]["jobs"]
                if job["id"] != selected_id
            )
            self.assertEqual(selected["taskId"], "task-1")
            self.assertEqual(untouched["taskId"], "old-task-2")

    def test_start_shot_generates_target_and_preserves_other_video(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ProjectStore(root / "projects")
            project = sample_project()
            project["storyboard"][0]["id"] = "shot-1"
            project["storyboard"][0]["completeVideoPrompt"] = (
                "已确认的完整视频提示词"
            )
            project["storyboard"][0]["completeVideoPromptStale"] = False
            second_shot = dict(project["storyboard"][0])
            second_shot["shot"] = 2
            second_shot["id"] = "shot-2"
            second_shot["action"] = "林舟离开雨夜街道"
            project["storyboard"].append(second_shot)
            old_jobs = build_jobs(project)
            old_jobs[1]["status"] = "succeeded"
            old_jobs[1]["taskId"] = "old-second-task"
            old_jobs[1]["localVideoUrl"] = (
                f"/media/{project['id']}/clip-002.mp4"
            )
            project["videoProduction"] = {
                "id": "old-production",
                "status": "partial",
                "stale": True,
                "settings": {
                    "model": "seedance-1.0-mini",
                    "resolution": "720p",
                    "ratio": "9:16",
                    "generateAudio": True,
                    "watermark": False,
                    "continuity": True,
                    "priority": 0,
                },
                "jobs": old_jobs,
            }
            store.save(project)
            provider = FakeSeedanceProvider()
            manager = VideoPipelineManager(
                store,
                root / "media",
                settings=SeedanceSettings(
                    base_url="https://relay.test/v1/videos",
                    api_key="test-key",
                    model="seedance-1.0-mini",
                    poll_interval=1,
                    request_timeout=10,
                    download_max_bytes=10 * 1024 * 1024,
                    ffmpeg_path="",
                ),
                provider=provider,
                sleep=lambda _: None,
            )
            result = manager.start_shot(
                project["id"],
                project["storyboard"][0]["id"],
                {},
            )
            self.assertEqual(result["retryScope"], "shot")
            deadline = time.monotonic() + 3
            latest = store.get(project["id"])
            while (
                latest["videoProduction"]["status"] in {"queued", "running"}
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
                latest = store.get(project["id"])
            self.assertEqual(len(provider.created), 1)
            preserved = next(
                job
                for job in latest["videoProduction"]["jobs"]
                if job["sourceShotId"] == "shot-2"
            )
            self.assertEqual(preserved["taskId"], "old-second-task")
            self.assertEqual(
                preserved["localVideoUrl"],
                f"/media/{project['id']}/clip-002.mp4",
            )


if __name__ == "__main__":
    unittest.main()
