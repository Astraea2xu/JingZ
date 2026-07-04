# 镜舟 · Seedance 视频创作 Agent

镜舟把一个模糊创意转换为完整生产链：创意分析 → 剧本 → 角色圣经 → 分镜 → Seedance 视频任务 → 本地保存 → FFmpeg 合片。

## 快速启动

需要 Python 3.10 或更高版本，Agent 本身不依赖第三方 Python 包。

```powershell
cd path\to\jingzhou-agent
Copy-Item .env.example .env
notepad .env
python server.py
```

打开 <http://127.0.0.1:8765>。

`server.py` 会自动读取项目目录中的 `.env`。密钥仅由本地 Python 服务读取，不会发给浏览器。

## 必需配置

### 1. 文本模型

文本模型负责生成创意方案、剧本、角色和分镜：

```dotenv
JINGZHOU_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
JINGZHOU_API_KEY=你的火山方舟APIKey
JINGZHOU_MODEL=你的文本模型ID或EndpointID
```

未配置文本模型时仍可使用演示模板，但剧本质量有限。

### 2. 图片模型

图片模型用于角色定妆图、场景概念图和视频分镜固定帧，可与文本模型使用
不同的服务地址与密钥：

```dotenv
IMAGE_API_BASE_URL=https://api.openai.com/v1/images/generations
IMAGE_API_KEY=你的图片模型APIKey
IMAGE_MODEL=你的图片模型名称
```

`IMAGE_API_BASE_URL` 是完整的 OpenAI 兼容图片生成地址。项目生成后可选择
自动批量生成角色/场景图片，也可以在“角色”页逐项生成或上传 PNG、JPEG、
WebP 图片。

图片修改使用独立的 OpenAI 兼容编辑接口：

```dotenv
IMAGE_EDIT_API_BASE_URL=https://api.openai.com/v1/images/edits
IMAGE_EDIT_API_KEY=
IMAGE_EDIT_MODEL=
```

编辑地址会原样使用，不会自动拼接路径。编辑密钥和模型留空时分别复用
`IMAGE_API_KEY` 与 `IMAGE_MODEL`。角色页中的每张参考图都可填写修改要求，
生成的新版本会成为主要参考图，旧版本仍保留。

角色参考图反推设定使用独立的视觉理解模型：

```dotenv
VISION_API_BASE_URL=https://api.177911.com/v1
VISION_API_KEY=
VISION_MODEL=填写支持图片输入的视觉模型名称
```

`VISION_API_KEY` 留空时会复用图片或文本模型密钥。角色页选择已上传或生成的
角色参考图后，可自动生成视觉锚点、角色功能、性格、声音和角色图片提示词。
这里必须使用支持 `image_url` 的多模态模型；DeepSeek 文本模型不能识别图片，
仍只用于创意生成和“对话编辑”。

若必须使用 DeepSeek 完成图生文本，需要部署 DeepSeek-VL2，并对外提供
OpenAI 兼容的 `/v1/chat/completions`：

```dotenv
VISION_API_BASE_URL=http://127.0.0.1:8000/v1
VISION_API_KEY=EMPTY
VISION_MODEL=deepseek-ai/deepseek-vl2-tiny
```

也可以把上述地址替换为提供 DeepSeek-VL2 且支持图片输入的第三方推理服务；
不能填写 `https://api.deepseek.com`。

### 对话与手动编辑

- “对话编辑”页每轮都会把当前剧本、角色和分镜发给文本模型。
- 模型只生成受限的结构化修改提案，并展示修改前后的差异；点击确认后才写入。
- 可以继续补充要求让模型重新生成提案，也可以放弃本轮建议，原项目保持不变。
- “角色”页可手动增加完整角色设定。
- “分镜”页可手动增加、删除分镜；删除后镜头编号自动重排。
- 起始参考帧可以留空，角色和场景参考图仍按 `reference_image` 提交。
- 修改、增加或删除分镜后，会立即重算每个角色的出场镜头；已有视频保留为
  旧版本并标记过期，需点击“按最新分镜重新生产”后才会创建新任务。

### 3. 中转站视频 API

```dotenv
VIDEO_API_BASE_URL=https://api.177911.com/v1/video/generations
VIDEO_CONTENT_API_BASE_URL=https://api.177911.com/v1/videos
VIDEO_API_KEY=你的中转站APIKey
VIDEO_MODEL=doubao-seedance-2-0-260128
```

`VIDEO_API_BASE_URL` 必须填写完整的 Seedance 任务创建地址，例如
`https://api.177911.com/v1/video/generations`。程序会原样使用该地址，不会补全、
替换或猜测任何路径。

接口使用 Seedance 任务协议：

- 创建任务：`POST /v1/video/generations`，`application/json`
- 查询任务：`GET /v1/video/generations/{task_id}`
- 角色和场景图：通过 `metadata.content` 以 `reference_image` 提交，最多 9 张
- 固定帧：通过 `first_frame` / `last_frame` 角色提交；所选参考图仍分别以
  `reference_image` 角色提交
- 下载结果：优先使用任务响应的视频 URL；缺少 URL 时使用
  `VIDEO_CONTENT_API_BASE_URL/{task_id}/content`
- 鉴权：`Authorization: Bearer <API_KEY>`

为了兼容已有 `.env`，原来的 `SEEDANCE_BASE_URL`、`SEEDANCE_API_KEY`、`SEEDANCE_MODEL` 仍然有效；新配置名优先。

## 视频生产流程

1. 在首页输入创意并生成项目。
2. 在每个环节修改并保存自定义提示词。
3. 在“角色”页生成或上传角色与场景参考图。
4. 在“分镜”页为每个镜头绑定角色/场景素材，并可指定固定首帧和固定尾帧。
5. 打开“视频生产”，选择分辨率、声音与连续镜头选项。
6. 确认费用后提交；镜舟按顺序生成并查询任务状态。
7. 每段成功后通过中转站返回的临时地址或配置的内容接口下载 MP4。
8. 中转站若返回尾帧，则将它作为下一段的参考图。
9. 所有镜头完成后自动生成合片清单；存在 FFmpeg 时自动输出 `final.mp4`。
10. 可重新生成全部失败镜头，也可在镜头卡片中只重生成一个分镜。

视频生产页面可直接播放已下载的本地镜头。若中转站已经返回可访问的
远程视频 URL、但本地下载仍在进行或失败，界面会先提供“远程预览”播放器。

Seedance 接口直接接收当前分镜的 4–15 秒时长、分辨率和画幅参数。

## FFmpeg 合片

视频接口返回的是镜头片段。要得到一条完整 MP4，需要安装 FFmpeg：

```dotenv
FFMPEG_PATH=C:\ffmpeg\bin\ffmpeg.exe
```

如果 `ffmpeg` 已加入系统 `PATH`，`FFMPEG_PATH` 可以留空。未检测到 FFmpeg 时，所有镜头仍会保存，并生成 `manifest.json`，但不会产生最终合片。

## 生成结果

```text
data/
├── projects/                 项目、任务 ID 与状态
└── media/
    └── <project-id>/
        ├── assets/            角色与场景参考图
        ├── clip-001.mp4
        ├── clip-001-last.png
        ├── ...
        ├── manifest.json
        └── final.mp4         安装 FFmpeg 后生成
```

## 终端请求日志

每次视频 API 创建、状态查询、删除和视频下载都会在运行
`python server.py` 的终端打印请求方法、URL、HTTP 状态与响应摘要。
API Key、Authorization 以及签名 URL 中的敏感查询参数会自动脱敏。

视频下载遇到 TLS EOF、连接重置或临时 5xx 时默认自动重试 4 次，
并在 CDN 支持时通过 HTTP Range 从已下载位置继续。可使用
`VIDEO_DOWNLOAD_RETRIES` 调整重试次数。

视频任务状态查询遇到 TLS EOF、超时或临时 5xx 时也会自动重试，
可使用 `VIDEO_API_RETRIES` 调整次数。创建视频的 POST 不自动重试，
避免网络状态不明时重复创建任务和计费。

## 安全与费用

- 浏览器看不到 API Key。
- 图片批量生成、整部视频生成和单镜头重生成都会在界面中确认费用。
- 项目删除会同时移除项目 JSON、图片和视频，且进行中的项目不可删除。
- 中转站远程视频地址可能短期失效，镜舟会自动下载到本机。
- 真实人物参考素材需符合平台授权、认证与内容安全要求。

## 测试

```powershell
python -B -m unittest discover -s tests -v
node --check static\app.js
```
