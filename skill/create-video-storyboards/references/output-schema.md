# Production package schema

Use this shape when the user requests JSON, automation, or direct import into the Jingzhou app.

```json
{
  "title": "string",
  "brief": {
    "hook": "string",
    "audience": "string",
    "goal": "string",
    "coreConflict": "string",
    "tone": "string",
    "durationSeconds": 60,
    "aspectRatio": "9:16"
  },
  "characters": [
    {
      "id": "char-1",
      "name": "string",
      "role": "string",
      "visualIdentity": "string",
      "personality": "string",
      "voice": "string"
    }
  ],
  "scenes": [
    {
      "id": "scene-1",
      "name": "string",
      "imagePrompt": "string",
      "referenceImageIds": []
    }
  ],
  "script": {
    "logline": "string",
    "synopsis": "string",
    "beats": [
      {
        "beat": "string",
        "duration": 8,
        "purpose": "string",
        "content": "string"
      }
    ],
    "narration": "string"
  },
  "storyboard": [
    {
      "shot": 1,
      "duration": 4,
      "scene": "string",
      "action": "string",
      "camera": "string",
      "visualPrompt": "string",
      "videoPrompt": "string",
      "completeVideoPrompt": "",
      "completeVideoPromptStale": false,
      "dialogue": "string",
      "audio": "string",
      "characterIds": ["char-1"],
      "continuity": "string"
    }
  ],
  "deliverables": {
    "titleOptions": ["string"],
    "caption": "string",
    "hashtags": ["#string"],
    "coverPrompt": "string",
    "musicPrompt": "string",
    "negativePrompt": "string",
    "checklist": ["string"]
  }
}
```

## Field rules

- Keep character IDs stable across all storyboard items.
- Make `visualPrompt` independently usable by an image model.
- Make `videoPrompt` focus on motion, temporal order, camera behavior, and the final frame.
- Leave `completeVideoPrompt` empty during initial storyboard generation. The app generates it after the user selects characters, reference images, and optional first/last frames.
- Mark `completeVideoPromptStale` when any storyboard or reference input changes; video submission requires a non-stale complete prompt.
- Keep durations numeric and measured in seconds.
- Keep `hashtags` as individual array items.
- Omit no top-level object; use an empty array only when a mode genuinely has no recurring characters.
