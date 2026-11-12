---
description: "Tides: show this conversation's memory tiers and settings, or curate now"
argument-hint: "[now | later | set section-size 100k | set tool-calls off | set tool-share 5% | set wake manual | off | on]"
allowed-tools: ["Bash(sh ${CLAUDE_PLUGIN_ROOT}/scripts/tides:*)"]
---

```!
sh "${CLAUDE_PLUGIN_ROOT}/scripts/tides" ui $ARGUMENTS
```

Tell the person, in two or three plain sentences, what the output above says. If it printed a curation notice, follow it.
