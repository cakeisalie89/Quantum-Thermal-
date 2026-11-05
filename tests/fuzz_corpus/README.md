# Fuzz regression corpus

Every file here is an input that once produced a finding in
`tools/fuzz_substrate.py`, kept so the defect it found cannot come back
quietly. `tests/test_agent_fuzz.py` replays all of them on every run.

A file is the harness's own finding record, as JSON:

| field | meaning |
| --- | --- |
| `target` | which parser or trust boundary it was fed to |
| `kind` | `ACCEPTED` (malformed input taken as valid), `CRASHED` (an exception outside the target's declared refusals), or `HUNG` (the target did not return inside its bound) |
| `detail` | what went wrong, in the words the harness produced |
| `input_b64` | the exact bytes, base64-encoded so a corpus file stays diffable text |
| `seed` | the campaign seed it came from, so the surrounding cases can be re-run |

**Refusing is not a finding.** Each target declares which exceptions *are* its
refusal, and raising one of those is the correct behaviour. Only the three
kinds above are recorded.

To add to the corpus, run the harness with `--save`:

```
python3 tools/fuzz_substrate.py --cases 4000 --save
```

Fixing the code and discarding the input that found the defect means the next
person to touch that parser gets to rediscover the same thing. That is what
this directory exists to prevent.

**This is not a fuzzing programme.** It is a floor: a bounded, seeded campaign
plus a growing set of regressions. Nothing here claims the parsers are correct.
