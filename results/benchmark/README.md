# Historical recall

One case from the mined benchmark, run the way the protocol asks: check out
the **parent** of a commit that fixed a reachability bug, run the pipeline
there, and see whether kreach reports the pair the fix addressed.

| | |
|---|---|
| fix | [`999fb9d51f93`](https://git.kernel.org/linus/999fb9d51f93) "ASoC: qcom: sm8250: Fix possibly undefined reference" |
| what it did | `depends on SND_SOC_QCOM_OFFLOAD_UTILS \|\| !SND_SOC_QCOM_OFFLOAD_UTILS` |
| run at | `86731a2a651e`, v6.16-rc3, `ARCH=x86_64` |
| result | **hit** — `recall 1.0 of 1 case` |

The fix is the `X || !X` pattern, which says "if the provider is a module I
must be a module too" — the y/m case kreach is built for. At the parent, S5
found one satisfiable query among the five naming `CONFIG_SND_SOC_SM8250`, and
the `vmlinux` link failed in 650 s on `qcom_snd_usb_offload_jack_remove`, the
symbol the fix's commit message names. kreach also reports
`qcom_snd_usb_offload_jack_setup`, which it does not.

## Method

S5 and S6 were filtered to the caller option, so this answers "does kreach
confirm this pair at the parent commit", not "does a full sweep surface it
among everything else". Filtering cannot change the verdict — same query, same
build — but it skips the rest of the sweep, so one case is answered rather
than a recall figure earned for the benchmark as a whole.

[docs/results.md](../../docs/results.md) has the per-stage numbers and what a
figure for the whole benchmark would still cost.

## Files

| | |
|---|---|
| `score-999fb9d51f93.json` | the scorer's verdict |
| `case-999fb9d51f93/` | the finding as published: report, records, witness, link log |
