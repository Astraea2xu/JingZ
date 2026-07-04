---
name: create-video-storyboards
description: Turn a rough idea, article, product brief, story, or reference-video concept into an original production-ready short-video package containing a creative brief, script, character bible, shot-by-shot storyboard, image and video generation prompts, narration, audio direction, cover concept, and publishing copy. Use when Codex needs to plan short dramas, smart videos, digital-human presentations, visual campaigns, or structure-inspired original remixes before media generation or editing.
---

# Create Video Storyboards

Convert the user's input into a coherent production package rather than a loose collection of prompts.

## Establish the brief

1. Identify the target audience, desired outcome, platform, duration, aspect ratio, tone, and non-negotiable constraints.
2. Infer reasonable defaults when details are missing. Ask only when the missing choice would change the concept materially.
3. State the core audience promise and a first-three-seconds hook.
4. Treat reference works as structural inspiration. Extract pacing, emotional movement, visual grammar, or information architecture; do not reproduce protected characters, dialogue, distinctive shots, or living-artist styles.

## Build the story

1. Write a logline and short synopsis.
2. Divide the duration into beats: hook, context, escalation, reversal or proof, payoff, and closing invitation.
3. Assign each beat a duration and narrative purpose.
4. Draft the complete narration or dialogue. Read it against the target duration and tighten it when necessary.

For short drama, prioritize goal, obstacle, rising cost, choice, and consequence.
For digital-human video, prioritize spoken clarity, proof points, natural pauses, and useful B-roll.
For visual design, treat each storyboard item as a key visual or layout state.
For reference-inspired work, explain the extracted abstract pattern before proposing the original variation.

## Lock character continuity

Create a character bible before writing image prompts. Give each recurring character:

- a stable ID and role;
- age range and general appearance;
- hair, signature clothing, accessories, and distinguishing features;
- personality and voice;
- details that must not change across shots.

Repeat the relevant visual anchors inside every prompt that contains the character. Never rely on a character name alone.

## Direct each shot

Create a numbered storyboard whose shot durations sum approximately to the target duration. Include for every shot:

- scene and time;
- visible action;
- framing, camera position, lens feel, and camera movement;
- dialogue or narration;
- sound or music cue;
- participating character IDs;
- continuity notes;
- a self-contained keyframe image prompt;
- a self-contained video-generation prompt describing movement and endpoint.

Keep one principal visual idea per shot. Add edit handles or a stable endpoint when the shot will be assembled into a sequence.

## Package delivery

Return:

1. Creative brief
2. Character bible
3. Script and beat sheet
4. Storyboard
5. Cover/key-art prompt
6. Music direction and reusable negative prompt
7. Three title options, publishing copy, and hashtags
8. Quality-control checklist

When structured data is useful, follow [references/output-schema.md](references/output-schema.md).

## Run a quality pass

Check that:

- the opening hook is visual and immediate;
- shot durations plausibly match the total;
- dialogue fits the time available;
- character anchors remain identical;
- locations and light direction do not jump accidentally;
- prompts describe observable content instead of vague quality words;
- the ending delivers the promise made by the hook;
- the result is original and production-safe.
