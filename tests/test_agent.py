import base64
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from agent import (
    AgentError,
    CreativeAgent,
    ModelSettings,
    OpenAICompatibleProvider,
    ProjectStore,
    public_project_view,
    public_video_production_view,
)


class FakeJSONResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class FakeProjectEditorProvider:
    def edit_project(self, context, _history, _message):
        self.last_context = context
        return {
            "reply": "建议补充角色并调整剧本，等待你的确认。",
            "operations": [
                {
                    "action": "update_script",
                    "fields": {"synopsis": "新的故事梗概"},
                },
                {
                    "action": "add_character",
                    "character": {
                        "name": "闻溪",
                        "role": "线索提供者",
                        "visualIdentity": "银灰短发，蓝色风衣",
                        "personality": "冷静",
                        "voice": "清晰低声",
                    },
                },
                {
                    "action": "add_shot",
                    "shot": {
                        "scene": "站台",
                        "duration": 6,
                        "action": "闻溪递出一张旧车票",
                        "camera": "手部特写",
                    },
                },
            ],
        }

    def describe_character(self, image_data_uri, _character_name):
        if not image_data_uri.startswith("data:image/png;base64,"):
            raise AssertionError("角色图片没有作为 data URI 传入")
        return {
            "visualIdentity": "短黑发，绿色夹克，银色耳钉",
            "personality": "敏锐克制",
            "role": "调查者",
            "voice": "年轻、沉稳",
            "imagePrompt": "原创角色设定图，短黑发，绿色夹克",
        }

    def generate_video_prompt(self, context):
        self.last_video_prompt_context = context
        return (
            "雨夜站台中，闻溪保持银灰短发和蓝色风衣，"
            "从固定首帧开始递出旧车票，镜头缓慢推近并稳定结束。"
        )


class CreativeAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        settings = ModelSettings(
            base_url="https://example.invalid/v1",
            api_key="",
            model="demo",
            image_model="",
            timeout=1,
        )
        self.store = ProjectStore(Path(self.temp_dir.name))
        self.media_root = Path(self.temp_dir.name) / "media"
        self.agent = CreativeAgent(
            self.store,
            settings,
            media_root=self.media_root,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_demo_generation_creates_complete_project(self):
        project = self.agent.generate(
            {
                "mode": "short_drama",
                "idea": "一个邮差只能投递来自明天的信。",
                "audience": "科幻故事爱好者",
                "duration": 60,
                "aspectRatio": "9:16",
            }
        )
        self.assertEqual(project["engine"], "demo")
        self.assertEqual(project["brief"]["durationSeconds"], 60)
        self.assertGreaterEqual(len(project["storyboard"]), 4)
        self.assertTrue(project["characters"][0]["visualIdentity"])
        self.assertIsNotNone(self.store.get(project["id"]))

    def test_project_list_contains_saved_metadata(self):
        project = self.agent.generate(
            {
                "mode": "digital_human",
                "idea": "解释为什么睡前刷手机更难入睡。",
                "duration": 30,
                "aspectRatio": "16:9",
            }
        )
        listing = self.store.list()
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["id"], project["id"])
        self.assertEqual(listing[0]["modeLabel"], "数字人口播")

    def test_active_video_urls_are_hidden_until_production_finishes(self):
        production = {
            "status": "running",
            "finalVideoUrl": "/media/project/final.mp4",
            "manifestUrl": "/media/project/manifest.json",
            "jobs": [
                {
                    "status": "succeeded",
                    "videoUrl": "https://cdn.test/clip.mp4",
                    "lastFrameUrl": "https://cdn.test/frame.png",
                    "localVideoUrl": "/media/project/clip.mp4",
                    "localLastFrameUrl": "/media/project/frame.png",
                }
            ],
        }
        public = public_video_production_view(production)
        self.assertEqual(public["finalVideoUrl"], "")
        self.assertEqual(public["manifestUrl"], "")
        self.assertEqual(public["jobs"][0]["videoUrl"], "")
        self.assertEqual(public["jobs"][0]["localVideoUrl"], "")
        self.assertEqual(
            production["jobs"][0]["videoUrl"],
            "https://cdn.test/clip.mp4",
        )

        finished = {**production, "status": "succeeded"}
        delivered = public_project_view({"videoProduction": finished})
        self.assertEqual(
            delivered["videoProduction"]["jobs"][0]["videoUrl"],
            "https://cdn.test/clip.mp4",
        )

    def test_single_retry_keeps_other_completed_videos_visible(self):
        production = {
            "status": "running",
            "retryScope": "single",
            "retryJobId": "job-retrying",
            "finalVideoUrl": "/media/project/final.mp4",
            "jobs": [
                {
                    "id": "job-retrying",
                    "status": "running",
                    "videoUrl": "https://cdn.test/old-retrying.mp4",
                    "localVideoUrl": "/media/project/old-retrying.mp4",
                },
                {
                    "id": "job-completed",
                    "status": "succeeded",
                    "videoUrl": "https://cdn.test/completed.mp4",
                    "localVideoUrl": "/media/project/completed.mp4",
                },
            ],
        }
        public = public_video_production_view(production)
        self.assertEqual(public["finalVideoUrl"], "")
        self.assertEqual(public["jobs"][0]["videoUrl"], "")
        self.assertEqual(
            public["jobs"][1]["videoUrl"],
            "https://cdn.test/completed.mp4",
        )
        self.assertEqual(
            public["jobs"][1]["localVideoUrl"],
            "/media/project/completed.mp4",
        )

    def test_rejects_empty_idea(self):
        with self.assertRaises(AgentError):
            self.agent.generate({"mode": "short_drama", "idea": ""})

    def test_rejects_unknown_mode(self):
        with self.assertRaises(AgentError):
            self.agent.generate({"mode": "unknown", "idea": "测试"})

    def test_deepseek_text_endpoint_is_rejected_for_image_understanding(self):
        settings = ModelSettings(
            base_url="https://api.deepseek.com",
            api_key="text-key",
            model="deepseek-v4-flash",
            image_model="",
            timeout=1,
            vision_base_url="https://api.deepseek.com",
            vision_api_key="vision-key",
            vision_model="deepseek-v4-flash",
        )
        provider = OpenAICompatibleProvider(settings)
        with patch("urllib.request.urlopen") as request:
            with self.assertRaisesRegex(
                AgentError,
                "DeepSeek Chat Completions 当前只接受文本",
            ):
                provider.describe_character(
                    "data:image/png;base64,YWJj",
                    "测试角色",
                )
        request.assert_not_called()

    def test_saved_project_is_valid_json(self):
        project = self.agent.generate(
            {
                "mode": "visual_design",
                "idea": "为夏日气泡水设计一组竖屏海报。",
                "duration": 15,
                "aspectRatio": "9:16",
            }
        )
        saved = Path(self.temp_dir.name, f"{project['id']}.json")
        payload = json.loads(saved.read_text(encoding="utf-8"))
        self.assertEqual(payload["title"], project["title"])

    def test_project_contains_editable_prompts_scenes_and_asset_bindings(self):
        project = self.agent.generate(
            {
                "mode": "short_drama",
                "idea": "雨夜车站的重逢。",
                "duration": 30,
                "aspectRatio": "16:9",
            }
        )
        self.assertIn("storyboard", project["stagePrompts"])
        self.assertTrue(project["characters"][0]["imagePrompt"])
        self.assertTrue(project["scenes"])
        self.assertIn("sceneId", project["storyboard"][0])
        self.assertEqual(project["storyboard"][0]["referenceAssetIds"], [])

    def test_manual_character_and_shot_management(self):
        project = self.agent.generate(
            {
                "mode": "short_drama",
                "idea": "夜班列车上的秘密。",
                "duration": 30,
                "aspectRatio": "16:9",
            }
        )
        updated = self.agent.add_character(
            project["id"],
            {
                "name": "闻溪",
                "role": "线索提供者",
                "visualIdentity": "银灰短发，蓝色风衣",
                "personality": "冷静",
                "voice": "清晰低声",
            },
        )
        character = updated["characters"][-1]
        self.assertEqual(character["name"], "闻溪")

        updated = self.agent.add_shot(
            project["id"],
            {
                "scene": "空站台",
                "duration": 7,
                "action": "闻溪递出旧车票",
                "camera": "手部特写",
                "characterIds": [character["id"]],
            },
        )
        added_shot = updated["storyboard"][-1]
        self.assertEqual(added_shot["startFrameAssetId"], "")
        self.assertEqual(added_shot["characterIds"], [character["id"]])
        updated_character = next(
            item
            for item in updated["characters"]
            if item["id"] == character["id"]
        )
        self.assertIn(
            added_shot["id"],
            [
                appearance["shotId"]
                for appearance in updated_character["appearances"]
            ],
        )

        updated = self.agent.delete_shot(
            project["id"],
            added_shot["id"],
        )
        self.assertNotIn(
            added_shot["id"],
            [shot["id"] for shot in updated["storyboard"]],
        )
        self.assertEqual(
            [shot["shot"] for shot in updated["storyboard"]],
            list(range(1, len(updated["storyboard"]) + 1)),
        )
        updated_character = next(
            item
            for item in updated["characters"]
            if item["id"] == character["id"]
        )
        self.assertNotIn(
            added_shot["id"],
            [
                appearance["shotId"]
                for appearance in updated_character["appearances"]
            ],
        )

    def test_manual_scene_management_and_full_chat_context(self):
        project = self.agent.generate(
            {
                "mode": "short_drama",
                "idea": "一间会改变布局的旧书店。",
                "duration": 30,
                "aspectRatio": "16:9",
            }
        )
        updated = self.agent.add_scene(
            project["id"],
            {
                "name": "地下档案室",
                "imagePrompt": "狭长地下室，绿色台灯，铁架与潮湿砖墙",
            },
        )
        scene = updated["scenes"][-1]
        self.assertEqual(scene["name"], "地下档案室")
        provider = FakeProjectEditorProvider()
        self.agent.provider = provider
        self.agent.chat_edit_project(
            project["id"],
            {"message": "检查场景和分镜的连续性。"},
        )
        self.assertIn("brief", provider.last_context)
        self.assertIn("scenes", provider.last_context)
        self.assertIn("stagePrompts", provider.last_context)
        self.assertIn("deliverables", provider.last_context)
        updated = self.agent.resolve_chat_proposal(
            project["id"],
            {
                "proposalId": self.store.get(project["id"])[
                    "pendingChatProposal"
                ]["id"]
            },
            accept=False,
        )["project"]
        deleted = self.agent.delete_scene(project["id"], scene["id"])
        self.assertNotIn(
            scene["id"],
            [item["id"] for item in deleted["scenes"]],
        )

    def test_complete_video_prompt_uses_story_and_reference_text(self):
        project = self.agent.generate(
            {
                "mode": "short_drama",
                "idea": "雨夜站台上的旧车票。",
                "duration": 30,
                "aspectRatio": "16:9",
            }
        )
        shot = project["storyboard"][0]
        character = project["characters"][0]
        asset = {
            "id": "asset-character-one",
            "type": "image",
            "ownerType": "character",
            "ownerId": character["id"],
            "prompt": "固定角色短黑发、墨绿色夹克与银色耳钉",
            "url": f"/media/{project['id']}/assets/reference.png",
        }

        def bind_reference(current):
            current.setdefault("assets", []).append(asset)
            current["storyboard"][0]["referenceAssetIds"] = [asset["id"]]
            current["storyboard"][0]["startFrameAssetId"] = asset["id"]

        self.store.mutate(project["id"], bind_reference)
        provider = FakeProjectEditorProvider()
        self.agent.provider = provider
        updated = self.agent.generate_shot_video_prompt(
            project["id"],
            shot["id"],
        )
        updated_shot = updated["storyboard"][0]
        self.assertTrue(updated_shot["completeVideoPrompt"])
        self.assertFalse(updated_shot["completeVideoPromptStale"])
        context = provider.last_video_prompt_context
        self.assertEqual(context["project"]["script"], project["script"])
        self.assertEqual(
            context["referenceImages"][0]["prompt"],
            asset["prompt"],
        )
        self.assertIn(
            "first_frame",
            context["referenceImages"][0]["usage"],
        )

        changed = self.agent.update_field(
            project["id"],
            {
                "scope": "shot",
                "id": shot["id"],
                "field": "camera",
                "value": "低机位缓慢推近",
            },
        )
        self.assertTrue(
            changed["storyboard"][0]["completeVideoPromptStale"]
        )
        confirmed = self.agent.update_field(
            project["id"],
            {
                "scope": "shot",
                "id": shot["id"],
                "field": "completeVideoPrompt",
                "value": changed["storyboard"][0]["completeVideoPrompt"],
            },
        )
        self.assertFalse(
            confirmed["storyboard"][0]["completeVideoPromptStale"]
        )

    def test_shot_edit_syncs_character_appearances_and_video_staleness(self):
        project = self.agent.generate(
            {
                "mode": "short_drama",
                "idea": "两名角色在车站交换线索。",
                "duration": 30,
                "aspectRatio": "16:9",
            }
        )
        shot = project["storyboard"][0]
        target_character = project["characters"][1]

        def add_completed_production(current):
            current["videoProduction"] = {
                "id": "completed-production",
                "status": "succeeded",
                "settings": {
                    "model": "seedance",
                    "resolution": "720p",
                    "ratio": "16:9",
                    "generateAudio": True,
                    "continuity": True,
                },
                "jobs": [{"id": "old-job", "status": "succeeded"}],
                "assembly": {"status": "succeeded", "message": ""},
            }

        self.store.mutate(project["id"], add_completed_production)
        updated = self.agent.update_field(
            project["id"],
            {
                "scope": "shot",
                "id": shot["id"],
                "field": "characterIds",
                "value": [target_character["id"]],
            },
        )
        synced_character = next(
            item
            for item in updated["characters"]
            if item["id"] == target_character["id"]
        )
        self.assertIn(
            shot["id"],
            [
                appearance["shotId"]
                for appearance in synced_character["appearances"]
            ],
        )
        self.assertTrue(updated["videoProduction"]["stale"])
        self.assertEqual(
            updated["videoProduction"]["currentStoryboardShotCount"],
            len(updated["storyboard"]),
        )
        self.assertEqual(
            updated["videoProduction"]["jobs"][0]["id"],
            "old-job",
        )

    def test_chat_proposes_then_applies_structured_operations(self):
        project = self.agent.generate(
            {
                "mode": "short_drama",
                "idea": "雨夜车站的重逢。",
                "duration": 30,
                "aspectRatio": "16:9",
            }
        )
        provider = FakeProjectEditorProvider()
        self.agent.provider = provider
        original_synopsis = project["script"]["synopsis"]
        result = self.agent.chat_edit_project(
            project["id"],
            {"message": "增加线索角色，并补一个递车票的镜头。"},
        )
        updated = result["project"]
        self.assertEqual(result["proposedOperations"], 3)
        self.assertEqual(updated["script"]["synopsis"], original_synopsis)
        self.assertNotEqual(updated["characters"][-1]["name"], "闻溪")
        self.assertEqual(
            provider.last_context["script"]["synopsis"],
            original_synopsis,
        )
        pending = updated["pendingChatProposal"]
        self.assertEqual(pending["operationCount"], 3)
        self.assertTrue(pending["preview"])
        self.assertEqual(len(updated["chatHistory"]), 2)

        accepted = self.agent.resolve_chat_proposal(
            project["id"],
            {"proposalId": pending["id"]},
            accept=True,
        )
        applied = accepted["project"]
        self.assertEqual(accepted["appliedOperations"], 3)
        self.assertEqual(applied["script"]["synopsis"], "新的故事梗概")
        self.assertEqual(applied["characters"][-1]["name"], "闻溪")
        self.assertEqual(applied["storyboard"][-1]["scene"], "站台")
        self.assertIsNone(applied["pendingChatProposal"])

    def test_chat_proposal_can_be_revised_or_rejected_without_changes(self):
        project = self.agent.generate(
            {
                "mode": "short_drama",
                "idea": "雨夜车站的重逢。",
                "duration": 30,
                "aspectRatio": "16:9",
            }
        )
        provider = FakeProjectEditorProvider()
        self.agent.provider = provider
        original_synopsis = project["script"]["synopsis"]
        first = self.agent.chat_edit_project(
            project["id"],
            {"message": "先给我一个修改方案。"},
        )
        first_id = first["project"]["pendingChatProposal"]["id"]
        second = self.agent.chat_edit_project(
            project["id"],
            {"message": "再克制一些，重新建议。"},
        )
        second_project = second["project"]
        second_pending = second_project["pendingChatProposal"]
        self.assertNotEqual(first_id, second_pending["id"])
        self.assertIsNotNone(
            provider.last_context["pendingProposal"],
        )
        self.assertEqual(
            second_project["script"]["synopsis"],
            original_synopsis,
        )
        rejected = self.agent.resolve_chat_proposal(
            project["id"],
            {"proposalId": second_pending["id"]},
            accept=False,
        )
        self.assertFalse(rejected["accepted"])
        self.assertEqual(
            rejected["project"]["script"]["synopsis"],
            original_synopsis,
        )
        self.assertIsNone(rejected["project"]["pendingChatProposal"])

    def test_character_description_is_generated_from_reference_image(self):
        project = self.agent.generate(
            {
                "mode": "short_drama",
                "idea": "雨夜车站的重逢。",
                "duration": 30,
                "aspectRatio": "16:9",
            }
        )
        character = project["characters"][0]
        asset = self.agent.upload_project_asset(
            project["id"],
            {
                "ownerType": "character",
                "ownerId": character["id"],
                "dataUrl": (
                    "data:image/png;base64,"
                    + base64.b64encode(
                        b"\x89PNG\r\n\x1a\nfake-reference"
                    ).decode("ascii")
                ),
            },
        )
        self.agent.provider = FakeProjectEditorProvider()
        updated = self.agent.describe_character_from_asset(
            project["id"],
            character["id"],
            {"assetId": asset["id"]},
        )
        target = updated["characters"][0]
        self.assertEqual(
            target["visualIdentity"],
            "短黑发，绿色夹克，银色耳钉",
        )
        self.assertEqual(target["descriptionSourceAssetId"], asset["id"])

    def test_shot_reference_binding_is_limited_to_nine_images(self):
        project = self.agent.generate(
            {
                "mode": "short_drama",
                "idea": "九名角色在车站汇合。",
                "duration": 30,
                "aspectRatio": "16:9",
            }
        )
        project["assets"] = [
            {
                "id": f"asset-{index}",
                "ownerType": "character",
                "ownerId": project["characters"][0]["id"],
                "url": f"/media/{project['id']}/assets/{index}.png",
            }
            for index in range(10)
        ]
        self.store.save(project)
        shot = project["storyboard"][0]
        accepted = self.agent.update_field(
            project["id"],
            {
                "scope": "shot",
                "id": shot["id"],
                "field": "referenceAssetIds",
                "value": [f"asset-{index}" for index in range(9)],
            },
        )
        self.assertEqual(
            accepted["storyboard"][0]["referenceAssetIds"],
            [f"asset-{index}" for index in range(9)],
        )
        with self.assertRaisesRegex(AgentError, "最多选择 9 张"):
            self.agent.update_field(
                project["id"],
                {
                    "scope": "shot",
                    "id": shot["id"],
                    "field": "referenceAssetIds",
                    "value": [f"asset-{index}" for index in range(10)],
                },
            )

    def test_update_field_and_upload_character_reference(self):
        project = self.agent.generate(
            {
                "mode": "short_drama",
                "idea": "雨夜车站的重逢。",
                "duration": 30,
                "aspectRatio": "16:9",
            }
        )
        character = project["characters"][0]
        updated = self.agent.update_field(
            project["id"],
            {
                "scope": "character",
                "id": character["id"],
                "field": "imagePrompt",
                "value": "固定角色黑发与绿色夹克",
            },
        )
        self.assertEqual(
            updated["characters"][0]["imagePrompt"],
            "固定角色黑发与绿色夹克",
        )
        fake_png = b"\x89PNG\r\n\x1a\nfake"
        asset = self.agent.upload_project_asset(
            project["id"],
            {
                "ownerType": "character",
                "ownerId": character["id"],
                "dataUrl": (
                    "data:image/png;base64,"
                    + base64.b64encode(fake_png).decode("ascii")
                ),
            },
        )
        self.assertEqual(asset["source"], "uploaded")
        self.assertTrue(
            (
                self.media_root
                / project["id"]
                / "assets"
                / Path(asset["url"]).name
            ).is_file()
        )

    def test_delete_project_removes_json_and_media(self):
        project = self.agent.generate(
            {
                "mode": "short_drama",
                "idea": "删除测试。",
                "duration": 15,
                "aspectRatio": "9:16",
            }
        )
        media_dir = self.media_root / project["id"]
        media_dir.mkdir(parents=True)
        (media_dir / "test.txt").write_text("test", encoding="utf-8")
        self.agent.delete_project(project["id"])
        self.assertIsNone(self.store.get(project["id"]))
        self.assertFalse(media_dir.exists())

    def test_delete_image_asset_removes_file_and_all_bindings(self):
        project = self.agent.generate(
            {
                "mode": "short_drama",
                "idea": "素材删除绑定测试。",
                "duration": 15,
                "aspectRatio": "9:16",
            }
        )
        character = project["characters"][0]
        content = b"\x89PNG\r\n\x1a\nasset-to-delete"
        asset = self.agent.upload_project_asset(
            project["id"],
            {
                "ownerType": "character",
                "ownerId": character["id"],
                "dataUrl": (
                    "data:image/png;base64,"
                    + base64.b64encode(content).decode("ascii")
                ),
            },
        )
        asset_path = (
            self.media_root
            / project["id"]
            / "assets"
            / Path(asset["url"]).name
        )

        def bind_everywhere(current):
            shot = current["storyboard"][0]
            shot["referenceAssetIds"] = [asset["id"]]
            shot["startFrameAssetId"] = asset["id"]
            shot["endFrameAssetId"] = asset["id"]
            current["videoProduction"] = {
                "status": "succeeded",
                "jobs": [
                    {
                        "referenceAssetIds": [asset["id"]],
                        "referenceImageAssetIds": [asset["id"]],
                        "referenceImageUrls": [asset["url"]],
                        "startFrameAssetId": asset["id"],
                        "startFrameUrl": asset["url"],
                        "endFrameAssetId": asset["id"],
                        "endFrameUrl": asset["url"],
                        "inputReferenceAssetId": asset["id"],
                    }
                ],
            }

        self.store.mutate(project["id"], bind_everywhere)
        self.agent.delete_project_asset(project["id"], asset["id"])
        updated = self.store.get(project["id"])
        self.assertFalse(asset_path.exists())
        self.assertFalse(updated["assets"])
        self.assertNotIn(
            asset["id"],
            updated["characters"][0]["referenceImageIds"],
        )
        shot = updated["storyboard"][0]
        self.assertEqual(shot["referenceAssetIds"], [])
        self.assertEqual(shot["startFrameAssetId"], "")
        self.assertEqual(shot["endFrameAssetId"], "")
        job = updated["videoProduction"]["jobs"][0]
        self.assertEqual(job["referenceAssetIds"], [])
        self.assertEqual(job["startFrameUrl"], "")
        self.assertEqual(job["endFrameUrl"], "")
        self.assertTrue(job["inputReferenceAssetDeleted"])

    def test_image_provider_uses_separate_image_endpoint_and_key(self):
        settings = ModelSettings(
            base_url="https://text.test/v1",
            api_key="text-key",
            model="text-model",
            image_model="image-model",
            timeout=10,
            image_base_url="https://image.test/v1/images/generations",
            image_api_key="image-key",
        )
        provider = OpenAICompatibleProvider(settings)
        with patch(
            "urllib.request.urlopen",
            return_value=FakeJSONResponse(
                {"data": [{"b64_json": "aW1hZ2U="}]}
            ),
        ) as mocked:
            result = provider.generate_image("角色设定图")
        request = mocked.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://image.test/v1/images/generations",
        )
        self.assertEqual(
            request.get_header("Authorization"),
            "Bearer image-key",
        )
        self.assertEqual(result["type"], "base64")

    def test_image_provider_rejects_chat_completion_endpoint(self):
        settings = ModelSettings(
            base_url="https://text.test/v1",
            api_key="text-key",
            model="text-model",
            image_model="image-model",
            timeout=10,
            image_base_url="https://relay.test/v1/chat/completions",
            image_api_key="image-key",
        )
        provider = OpenAICompatibleProvider(settings)
        with patch.object(provider, "_request") as request:
            with self.assertRaisesRegex(
                AgentError,
                "images/generations",
            ):
                provider.generate_image("测试图片")
        request.assert_not_called()

    def test_image_edit_provider_uses_exact_multipart_endpoint(self):
        settings = ModelSettings(
            base_url="https://text.test/v1",
            api_key="text-key",
            model="text-model",
            image_model="image-model",
            timeout=10,
            image_edit_base_url="https://relay.test/custom/images/edits",
            image_edit_api_key="edit-key",
            image_edit_model="edit-model",
        )
        provider = OpenAICompatibleProvider(settings)
        with patch(
            "urllib.request.urlopen",
            return_value=FakeJSONResponse(
                {"data": [{"b64_json": "aW1hZ2U="}]}
            ),
        ) as mocked:
            result = provider.edit_image(
                "把外套改成蓝色",
                b"\x89PNG\r\n\x1a\nsource",
                "image/png",
                size="1024x1536",
            )
        request = mocked.call_args.args[0]
        body = request.data
        self.assertEqual(
            request.full_url,
            "https://relay.test/custom/images/edits",
        )
        self.assertEqual(
            request.get_header("Authorization"),
            "Bearer edit-key",
        )
        self.assertIn("multipart/form-data", request.get_header("Content-type"))
        self.assertIn(b'name="model"', body)
        self.assertIn(b"edit-model", body)
        self.assertIn(b'name="image"; filename="source.png"', body)
        self.assertIn(b"\x89PNG\r\n\x1a\nsource", body)
        self.assertEqual(result["type"], "base64")

    def test_image_edit_provider_rejects_generation_endpoint(self):
        settings = ModelSettings(
            base_url="https://text.test/v1",
            api_key="text-key",
            model="text-model",
            image_model="image-model",
            timeout=10,
            image_edit_base_url=(
                "https://relay.test/v1/images/generations"
            ),
            image_edit_api_key="edit-key",
            image_edit_model="edit-model",
        )
        provider = OpenAICompatibleProvider(settings)
        with patch.object(provider, "_request_multipart") as request:
            with self.assertRaisesRegex(AgentError, "images/edits"):
                provider.edit_image(
                    "修改图片",
                    b"\x89PNG\r\n\x1a\nfake",
                    "image/png",
                )
        request.assert_not_called()

    def test_image_edit_provider_sends_multiple_reference_images(self):
        settings = ModelSettings(
            base_url="https://text.test/v1",
            api_key="text-key",
            model="text-model",
            image_model="image-model",
            timeout=10,
            image_edit_base_url="https://relay.test/v1/images/edits",
            image_edit_api_key="edit-key",
            image_edit_model="edit-model",
        )
        provider = OpenAICompatibleProvider(settings)
        with patch(
            "urllib.request.urlopen",
            return_value=FakeJSONResponse(
                {"data": [{"b64_json": "aW1hZ2U="}]}
            ),
        ) as mocked:
            provider.edit_images(
                "保持两个角色身份一致",
                [
                    (b"\x89PNG\r\n\x1a\nfirst", "image/png"),
                    (b"\xff\xd8\xffsecond", "image/jpeg"),
                ],
            )
        body = mocked.call_args.args[0].data
        self.assertEqual(body.count(b'name="image[]"; filename='), 2)
        self.assertIn(b'filename="source-01.png"', body)
        self.assertIn(b'filename="source-02.jpg"', body)
        self.assertIn(b"\x89PNG\r\n\x1a\nfirst", body)
        self.assertIn(b"\xff\xd8\xffsecond", body)

    def test_asset_generation_uses_project_images_as_global_references(self):
        self.agent.settings.image_model = "image-model"
        self.agent.settings.image_base_url = (
            "https://relay.test/v1/images/generations"
        )
        self.agent.settings.image_api_key = "image-key"
        self.agent.settings.image_edit_base_url = (
            "https://relay.test/v1/images/edits"
        )
        self.agent.settings.image_edit_api_key = "edit-key"
        self.agent.settings.image_edit_model = "edit-model"
        project = self.agent.generate(
            {
                "mode": "short_drama",
                "idea": "两个人在固定咖啡馆里重逢。",
                "duration": 15,
                "aspectRatio": "16:9",
            }
        )
        character = project["characters"][0]
        second_character = project["characters"][1]
        reference_content = b"\x89PNG\r\n\x1a\ncharacter"
        second_reference_content = b"\x89PNG\r\n\x1a\nsecond-character"
        reference = self.agent.upload_project_asset(
            project["id"],
            {
                "ownerType": "character",
                "ownerId": character["id"],
                "dataUrl": (
                    "data:image/png;base64,"
                    + base64.b64encode(reference_content).decode("ascii")
                ),
            },
        )
        second_reference = self.agent.upload_project_asset(
            project["id"],
            {
                "ownerType": "character",
                "ownerId": second_character["id"],
                "dataUrl": (
                    "data:image/png;base64,"
                    + base64.b64encode(second_reference_content).decode("ascii")
                ),
            },
        )
        generated_content = b"\x89PNG\r\n\x1a\ngenerated"
        with patch.object(
            self.agent.provider,
            "edit_images",
            return_value={
                "type": "base64",
                "value": base64.b64encode(generated_content).decode("ascii"),
            },
        ) as mocked:
            asset = self.agent.generate_project_asset(
                project["id"],
                {
                    "ownerType": "scene",
                    "ownerId": project["scenes"][0]["id"],
                    "prompt": "固定咖啡馆全景",
                    "referenceAssetIds": [
                        reference["id"],
                        second_reference["id"],
                    ],
                    "useGlobalReferences": True,
                    "size": "1536x1024",
                },
            )
        self.assertEqual(asset["source"], "reference-generated")
        self.assertEqual(
            asset["referenceAssetIds"],
            [reference["id"], second_reference["id"]],
        )
        self.assertEqual(
            asset["requiredCharacters"],
            [character["name"], second_character["name"]],
        )
        self.assertTrue(asset["globalConsistency"])
        self.assertEqual(mocked.call_args.args[1][0][0], reference_content)
        self.assertEqual(
            mocked.call_args.args[1][1][0],
            second_reference_content,
        )
        self.assertIn(
            f"参考图 1：角色「{character['name']}」",
            mocked.call_args.args[0],
        )
        self.assertIn(
            f"参考图 2：角色「{second_character['name']}」",
            mocked.call_args.args[0],
        )
        self.assertIn("最终画面必须同时包含", mocked.call_args.args[0])
        self.assertIn("不同角色不能融合", mocked.call_args.args[0])

    def test_edit_project_asset_keeps_old_version_and_updates_bindings(self):
        self.agent.settings.image_edit_base_url = (
            "https://relay.test/v1/images/edits"
        )
        self.agent.settings.image_edit_api_key = "edit-key"
        self.agent.settings.image_edit_model = "edit-model"
        project = self.agent.generate(
            {
                "mode": "short_drama",
                "idea": "角色图片修改测试。",
                "duration": 15,
                "aspectRatio": "9:16",
            }
        )
        character = project["characters"][0]
        source_content = b"\x89PNG\r\n\x1a\nsource"
        source = self.agent.upload_project_asset(
            project["id"],
            {
                "ownerType": "character",
                "ownerId": character["id"],
                "dataUrl": (
                    "data:image/png;base64,"
                    + base64.b64encode(source_content).decode("ascii")
                ),
            },
        )

        def bind_source(current):
            shot = current["storyboard"][0]
            shot["referenceAssetIds"] = [source["id"]]
            shot["startFrameAssetId"] = source["id"]

        self.store.mutate(project["id"], bind_source)
        edited_content = b"\x89PNG\r\n\x1a\nedited"
        with patch.object(
            self.agent.provider,
            "edit_image",
            return_value={
                "type": "base64",
                "value": base64.b64encode(edited_content).decode("ascii"),
            },
        ) as mocked:
            edited = self.agent.edit_project_asset(
                project["id"],
                {
                    "assetId": source["id"],
                    "prompt": "把外套改成深蓝色",
                    "size": "1024x1536",
                },
            )
        mocked.assert_called_once()
        self.assertEqual(edited["source"], "edited")
        self.assertEqual(edited["parentAssetId"], source["id"])
        updated = self.store.get(project["id"])
        self.assertEqual(len(updated["assets"]), 2)
        self.assertEqual(
            updated["storyboard"][0]["referenceAssetIds"],
            [edited["id"]],
        )
        self.assertEqual(
            updated["storyboard"][0]["startFrameAssetId"],
            edited["id"],
        )
        self.assertEqual(
            updated["characters"][0]["referenceImageIds"][0],
            edited["id"],
        )


if __name__ == "__main__":
    unittest.main()
