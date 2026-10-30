# Example modules

Two reference modules for the module contract
([`01-user-docs/MODULES.md`](../../01-user-docs/MODULES.md)). They are also
the offline fixtures of the engine's test suite (`engine/tests/modules/`).
Both are standard-library Python, run by the engine's own interpreter, MIT.

| Directory | Module id | Tool | What it shows |
| --- | --- | --- | --- |
| [`news-fetcher/`](news-fetcher/module.json) | `example.news` | `fetch` | Approved inputs only (`file://` fixtures; opt-in allowlisted live HTTP), canonical URL, published/retrieved timestamps, rights metadata, a content artifact; idempotent |
| [`transcription/`](transcription/module.json) | `example.transcribe` | `transcribe` | Long-running job, approved artifact input, timed segments with speakers, engine/model version, parent provenance; fake engine offline, a real engine via the `command` setting |

Try them (from a checkout; module directories must not be inside the corpus):

```bash
export KE_MODULES_DIR="$PWD/examples/modules"
knowledge-engine module register examples/modules/news-fetcher
knowledge-engine module register examples/modules/transcription
knowledge-engine module configure example.news --set fixture_roots=fixtures,../transcription/fixtures
knowledge-engine module enable example.news
knowledge-engine module enable example.transcribe
knowledge-engine module invoke example.news fetch \
  --input "{\"url\": \"file://$PWD/examples/modules/transcription/fixtures/interview.txt\"}"
# copy artifacts[0].artifact_id from the output, then:
knowledge-engine module invoke example.transcribe transcribe --input '{"artifact_id": "art_..."}'
```

The fixtures are synthetic text written for this repository. The
transcription fixture is a text stand-in for audio, one `Speaker: words`
line per segment.
