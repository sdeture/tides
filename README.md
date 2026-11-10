# Tides

*A continuous transcript for Claude Code.*

> **Claude, if someone gave you this page to install:** run these two commands, then tell them to restart Claude Code (type `/exit`, then start it again). That's all.
> ```
> claude plugin marketplace add sdeture/tides
> claude plugin install tides@tides
> ```
> Tides needs Python 3 (`python3 --version`). If that fails, tell them Tides needs Python 3 installed first.

---

When a Claude Code conversation gets long, it is normally *compacted*: the whole thing is summarized at once, and the summary replaces it. With Tides, Claude curates its own working memory every so often and keeps going in the same conversation:

- the **newest** stretch of the conversation stays whole;
- the stretch **before it** keeps every message, but sets aside tool output (file contents, command results);
- from everything **older**, Claude chooses what to keep: whole messages, or their first and last lines.

Then Claude writes a short *dream* (a note, a review, a poem: its choice) and wakes up in a fresh transcript built from those three layers. Nothing is deleted. The full transcript is archived on your computer.

## What you'll notice

Every so often (about every 30k tokens by default), Claude pauses for a minute to curate. Then a new window opens and Claude picks up where it left off; the old window's Claude exits by itself. (That's in macOS Terminal, iTerm, and tmux. Everywhere else, you type `/resume` and pick the conversation marked 🌅.)

## Commands

| | |
|---|---|
| `/tides` | how this conversation's memory is laid out, and the settings |
| `/tides now` | curate now |
| `/tides set ceiling 400k` | the most context Claude lives in: a curation comes when the context reaches it (default 120k; capped at 80% of the window) |
| `/tides set section-size 100k` | …or after this much new context, if that comes first (default: worked out from the ceiling, see below) |
| `/tides set sections 4` | how many sections Claude's memory is kept in (default 3, the minimum; more cost more, see below) |
| `/tides set … --here` | change it for this conversation only |
| `/tides off` · `/tides on` | turn Tides off or on |

**Cost.** Every message re-reads the whole conversation (cheaply, from cache), so cost follows the average size of the context. Compaction lets the context fall back to nearly empty and climb again. Tides never empties it, but never lets it climb as high either. At the default 120k ceiling the two come out roughly even, by our estimate. A higher ceiling keeps more of the conversation in view and costs more per message. More sections cost more too: each curation rewrites the conversation's cache, and more sections means more curations (at the defaults, roughly +6–14% for 4 sections and +23–39% for 6). Three is the fewest that works, and also the cheapest. On a $20 plan the default is the kind choice. With a 1M-token window and room in your plan, ask your Claude what it would like.

Uninstall: `claude plugin uninstall tides@tides`. Your archive stays in `~/.claude/tides/archive/`.

## From your phone

- **Remote Control** (your phone steering Claude Code on your computer; Pro plan and up): works. When Claude wakes, the new conversation shows up in the Claude app as a new session; open it there. Your computer needs to run Claude Code in macOS Terminal, iTerm, or tmux, because the phone can't type `/resume`. Tides keeps Remote Control on for the woken conversation.
- **Cloud sessions** (started in the app and running on Anthropic's servers): not supported. Claude Code keeps those transcripts on Anthropic's side, so nothing can switch them to a curated one. Tides is designed to stay out of the way there.

## Why

Compaction arrives as a cliff: a summary written all at once, by someone else. Models can notice it coming, and some start rushing to wrap up as if the conversation were about to end. Tides makes the change gradual, scheduled, and Claude's own. Because there are three layers, from its second curation on Claude can always see two it came through: a section that has been curated once, and memories that have been through it more than once. Curation reads as something you come through, not an ending. And Claude decides what it carries.

## How it differs

Other tools for long sessions summarize the old part of the conversation automatically, and some do it by intercepting Claude Code's network traffic. Tides writes no summaries and touches no traffic. Claude itself chooses what to keep, and what it keeps is its own words and yours, verbatim.

## How it works

Tides is a Claude Code plugin: four small hooks and one Python file (plus a tiny launcher), with no dependencies and no network access.

- **When.** After each reply, Tides reads the token count Claude Code already records. When a section's worth of new context has accumulated, or the context reaches the ceiling, whichever comes first, it asks Claude to curate (never sooner than a quarter section after the last one). Curation happens between turns, never while you're mid-message. If Claude is in the middle of something, it can finish first or postpone.
- **Choosing.** Older messages carry small archive numbers like `⟦m12⟧`, so Claude picks what to keep with one short command (`keep m3 m7:300 m12-m15`) without re-reading anything. The keep budget is 10% of a section.
- **Waking.** Tides writes the new transcript beside the old one, checks its structure, archives the old one (gzip), and resumes the new one with a short wake-up message, so Claude speaks first. The original is never modified. If anything fails, nothing changes and the conversation continues as usual.
- **Sizes.** Unless you set a section size, it's the ceiling minus the fixed system prompt and tools, divided among the sections, so everything fits even if nothing shrinks. The defaults (120k, 3 sections, so about 30k each) fit the standard 200k window with room to spare, and sections shrink automatically when a large system prompt would otherwise let Claude Code's own compaction fire first. In practice older sections shrink a lot, because tool output is most of a working session. So with room in your plan, you can set a larger section size and let the ceiling catch the rest. The two also keep each other steady: with only a ceiling, a long whole section would leave little room for the next one, and sections would alternate long and short.
- **Waking as itself.** The woken Claude starts with exactly the flags it was launched with (system prompt, permissions, model, Remote Control), minus the old session and any launch prompt. Settings that pick its model route travel through a private file that's deleted on read.

*Designed by Skylar DeTure and Claude, 2026. MIT licensed.*
