# Klepsydra timers file format

Klepsydra keeps your routines as a JSON file. Most of the time you don't need to see it: the timer
editor (**⚙ Settings → New timer**, or **Edit timers** and ✎) writes it for you. This page is for
editing it as text: in a timer editor's **JSON** tab (one timer), in **Edit all timers as JSON** (the
whole file), or in files you import (**Import a timer**, which adds them) and back up (**Back up all
timers** / **Restore from backup**). The bundled `timers.json` is the default until you change
anything. Your own copy is stored in the browser on your device, not on the server.

## Shape

```json
{
  "timers": [
    { "name": "…", "intervals": [ … ] },
    { "name": "…", "intervals": [ … ] }
  ]
}
```

A bare list of timers (`[ { … }, { … } ]`) is accepted too, and the list may be empty. Fields not listed
below are ignored, and the editor keeps them.

## Timer

| Field | Type | Default | Meaning |
|---|---|---|---|
| `name` | string | required | Shown in the list. Trimmed; cut to 60 characters. |
| `description` | string | none | Short text under the name in the list. Spaces and line breaks collapse to single spaces; cut to 200 characters. |
| `intervals` | list | required | Intervals and repeat blocks, run in order. At least one. |
| `prepare` | whole number, 0–120 | 0 | Seconds of a grey "Get ready" interval before the first one. |
| `countdown` | whole number, 0–10 | 3 | Default for every interval inside, see below. |
| `halfway` | true / false | false | Default for every interval inside, see below. |

## Interval

| Field | Type | Default | Meaning |
|---|---|---|---|
| `name` | string | required | Shown on the timer screen. Trimmed; cut to 60 characters. |
| `duration` | number or string | required | Seconds (`45`, `7.5`) or a string `"45"`, `"1:30"`, `"1:02:00"` (`m:ss` / `h:mm:ss`). 1 second to 24 hours. |
| `color` | string | from a palette | Background while the interval runs. Any opaque CSS colour: `"#43a047"`, `"#4a4"`, `"tomato"`, `"rgb(67 160 71)"`. Semi-transparent colours are rejected. Without it, each distinct name in the timer gets the next palette colour, so "Rest" is always the same colour. |
| `countdown` | whole number, 0–10 | inherited | Short beep on each of the last N seconds. 0 turns it off. |
| `halfway` | true / false | inherited | Double beep halfway through. Skipped when the interval is too short to fit it before the countdown. |
| `comment` | string | none | Short cue shown under the interval's name on the timer screen, in the Next card, and in the ⓘ details. Spaces and line breaks collapse to single spaces; cut to 100 characters. |

## Repeat block

An item with `repeat` is a block instead of an interval:

```json
{ "repeat": 8, "intervals": [
  { "name": "Work", "duration": 20 },
  { "name": "Rest", "duration": 10 }
]}
```

| Field | Type | Meaning |
|---|---|---|
| `repeat` | whole number, 1–100 | How many times to run the block. |
| `intervals` | list | Intervals and further blocks. Blocks nest up to 5 deep. |
| `countdown`, `halfway` | as above | Defaults for everything inside the block. |

The timer screen shows "Round 2/8" for intervals inside a block (the innermost block, when nested).

## Inheritance

`countdown` and `halfway` flow down: timer → block → nested block → interval. The closest one wins:

```json
{ "name": "HIIT", "halfway": true, "intervals": [
  { "name": "Squats", "duration": 40 },
  { "name": "Rest", "duration": 20, "halfway": false }
]}
```

## Sounds

- Countdown: a short beep on each of the last `countdown` seconds of an interval.
- Halfway: two short beeps (when `halfway` is true).
- End of an interval: one long beep.
- End of the timer: a three-note chime.

## Limits

- 200 timers per file, 2000 intervals per timer after repeats are expanded.
- Imported and restored files up to 1 MB.

## Errors

A file that isn't valid JSON, or has no list of timers, can't be saved, imported or restored; the message says
what's wrong (for JSON errors, the position). Inside a valid file, a timer with a mistake shows greyed out in the
list with the reason, and the other timers still work. **Import a timer** adds only the timers without
mistakes and says what's wrong with the others. Typical messages:

| Message | Fix |
|---|---|
| `Not valid JSON: … (line 12 column 5)` | Missing comma or quote, or a trailing comma, near that spot. **Format** in the editor only works on valid JSON. |
| `The file must be a list of timers or {"timers": [...]}` | Wrap your timers in `{"timers": [ … ]}`. |
| `"Tabata" #2: duration must be seconds or "m:ss"` | Durations are numbers or `"m:ss"` strings (`"1:30"`, not `"90s"`). `#2` is the item's position in its list. |
| `"Tabata" #1: "blue-ish" is not an opaque colour` | Use a hex code or CSS colour name. |
| `"Tabata" #1: repeat must be a whole number from 1 to 100` | |
| `"Tabata": more than 2000 intervals` | Nested repeats multiply; reduce them. |

## Full example

```json
{
  "timers": [
    {
      "name": "Tabata",
      "description": "8 rounds of 20 s all-out work and 10 s rest.",
      "prepare": 10,
      "countdown": 3,
      "intervals": [
        { "repeat": 8, "intervals": [
          { "name": "Work", "duration": 20, "color": "#43a047" },
          { "name": "Rest", "duration": 10, "color": "#e53935" }
        ]}
      ]
    },
    {
      "name": "Stretching",
      "description": "Per-side stretches with a transition before each exercise.",
      "intervals": [
        { "name": "Get in position", "duration": 10, "color": "#546e7a" },
        { "name": "Hamstring strap – left", "duration": "1:30", "color": "#1e88e5",
          "comment": "Hold 20 s → press 5 s → relax deeper." },
        { "name": "Hamstring strap – right", "duration": "1:30", "color": "#1565c0",
          "comment": "Hold 20 s → press 5 s → relax deeper." }
      ]
    }
  ]
}
```
