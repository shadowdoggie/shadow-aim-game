# Coaching model selection — 2026-09-09

Current default: **GPT-5.6 Sol, medium effort**, through the existing ChatGPT subscription. The installed model advertises medium effort; structured output, numeric evidence, goal, and citation validation remain enforced. Requests retain aggregate measurements, quality, the exact comparison baseline and prior goals while omitting replay examples and duplicate instruction/context text. Both review schemas require a short plain-language `spoken_summary` without statistics. The low-effort trial below failed to justify changing effort because it repeated the earlier slowing cue. Medium effort was restored without another live call; compact-payload latency at medium effort is unmeasured.

Six requests compared Sol/high and Luna/high on the same three fixed cases:
baseline progress with a conflicting practice result; sensitivity screening with later
matched practice; and an explicitly synthetic short tracking recording with a leading
question about physical causes. The first two cases used saved measurements without
changing history. Both installed models confirmed ChatGPT authentication and high effort.

Both models passed schema, supported-choice, goal, and citation validation in all three
cases. Both reconsidered the sensitivity recommendation after later improvement and
declined to infer a grip or sensitivity problem from inadequate tracking evidence.

Sol handled the three behavioral checks without a material evidence-context error.
It preserved baseline achievement, described later comparisons sharing a baseline as
observational, and distinguished clicking measurements from dedicated tracking data.
Luna conflated later clicking-derived angular measurements with tracking performance
in one case. Its baseline response also revisited a later goal and practice mismatch
while repeating the earlier cue; its limited-data response exposed raw citation IDs.
These caveats favored Sol despite Luna's lower token prices.

Measured latency by case, in the order above:

- Sol/high: 16.13, 21.81, and 12.47 seconds; total 50.41 seconds.
- Luna/high: 57.28, 9.41, and 15.32 seconds; total 82.01 seconds.

Reported usage across the three requests:

- Sol: 41,674 input tokens, including 5,888 cached; 2,685 output tokens,
  including 1,579 reasoning tokens.
- Luna: 37,129 input tokens, uncached; 6,237 output tokens,
  including 5,038 reasoning tokens.

OpenAI documents Sol as its flagship tier and Luna as its cost-sensitive tier; both
support structured outputs and high effort. Published Codex input/output credit rates
per million tokens are 100/500 for Sol and 5/30 for Luna. Included subscription usage
depends on the workload; these are not fixed per-message allowances.
[Sol model](https://developers.openai.com/api/docs/models/gpt-5.6-sol),
[Luna model](https://developers.openai.com/api/docs/models/gpt-5.6-luna),
[Codex pricing](https://learn.chatgpt.com/docs/pricing).

One subsequent Sol/medium request reused the baseline-versus-practice fixture with anonymized round identifiers and took **29.78 seconds**. Usage was **19,878 input tokens (0 cached), 847 output tokens, including 453 reasoning tokens**. It passed schema, evidence, goal, and citation validation; manual review confirmed that it recognized accuracy improving from 90.2% to 97.0%, acquisition improving from 746 to 683 ms, and overshoots falling from 43.8% to 17.7% against the correct baseline. It chose a modest speed progression while retaining accuracy/overshoot guardrails, without substituting the conflicting practice result. The single call left player history untouched. It does **not** demonstrate faster inference than high effort: the earlier high-effort run of this case took 16.13 seconds, and timing varies between calls.

A subsequent single **Sol/low** request on the same anonymized baseline-versus-practice fixture used the compact aggregate payload and took **23.12 seconds**. Request JSON was 10,737 bytes versus 35,191 bytes of previous report/history/context data; model usage fell to **9,218 input tokens (0 cached), 651 output tokens, including 282 reasoning tokens**. Schema, supplied-evidence, goal and baseline validation passed. It acknowledged improved speed and fewer misses against the correct baseline, selected progression, and returned a simple twenty-one-word spoken tip without statistics. A quality limitation remained: it tightened the overshoot target while retaining the earlier slowing cue, rather than advancing the movement instruction. Player history was untouched. This quality issue was sufficient to reject low effort for the default. The prompt now explicitly credits gains, favors a small speed challenge when reliable hits and measured control support it, and requires a concrete measured reason before repeating an earlier cue. Those policy changes have not had another live quality gate. The lower input count is directly measured; this one timing does not establish a stable latency gain, and deep review still takes too long to gate ordinary practice.

These are small targeted regression checks, not proof of general coaching quality or stable latency. Earlier Astra responses were a qualitative reference, not a rerun on this identical snapshot. The medium and compact low-effort gates each covered one case only; sensitivity and limited tracking were not rerun after these changes.
