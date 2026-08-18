# Local ASR fallback

Use this reference only after every configured caption attempt is explicitly `unavailable`.

## Safety gate

Allow local ASR only when all non-cache attempts use one of these codes:

- `source_not_configured`
- `captions_unavailable`
- `no_transcript_found`
- `chrome_transcript_unavailable`

Do not download audio after authentication, rate-limit, site-block, timeout, dependency, empty-caption, stale-caption, or parse failures. Report that blocking attempt instead.

## Two-stage setup

1. If the run returns `asr_dependency_missing`, install only the optional local ASR dependencies:

```powershell
uv sync --directory "<skill-directory>" --extra asr
```

2. Retry without a model-download flag. If the model is absent, the run must stop before metadata or audio work with `asr_model_required`. The default multilingual `small` model is estimated at 486,215,847 bytes, about 464 MiB of repository files. Explain that download storage, temporary files, CPU time, and model loading require additional headroom.
3. Ask for explicit confirmation. Only after confirmation, rerun with:

```powershell
uv run --extra asr --directory "<skill-directory>" python -m video_digest "<video-url>" --output $evidencePath --allow-asr-model-download
```

The command prints a final cost notice to stderr before model preparation begins. Do not suppress it. Never infer model-download consent from the original summarization request.

## Model and device profiles

Keep `small`, `cpu`, and `int8` as the portable default. Larger models are explicit opt-ins, and each first download requires a fresh cost confirmation:

| Model | Approximate download | Intended use |
| --- | ---: | --- |
| `small` | 464 MiB | Portable default and CPU fallback |
| `medium` | 1,460 MiB | Higher accuracy when extra local compute is acceptable |
| `turbo` | 1,547 MiB | Recommended speed/accuracy GPU profile |
| `large-v3` | 2,948 MiB | Highest available accuracy; substantially heavier |

For an explicitly requested NVIDIA GPU run, select the device and compute type rather than changing the default:

```powershell
--asr-model turbo --asr-device cuda --asr-compute-type float16
```

`int8_float16` is another GPU option when supported by the local CTranslate2 runtime. The runner checks that CUDA and the requested compute type are visible before metadata or audio download. A CUDA model-load failure still stops before audio download and should be reported as a local NVIDIA/CTranslate2 runtime problem. Do not silently switch devices or quantization modes.

## Storage controls

Defaults live below the current user's local application-data directory:

- model: `video-digest-skill/asr/models/<model>`
- temporary audio: `video-digest-skill/asr/temporary-audio`

If preflight returns `disk_space_insufficient`, choose explicit directories on a drive with sufficient free space:

```powershell
--asr-model-directory "D:\VideoDigest\models\small" --asr-temporary-directory "D:\VideoDigest\temporary-audio"
```

Do not put models or temporary media inside the Git repository. Do not silently choose a network or cloud-synced directory.

## Evidence contract

A complete local ASR result must contain:

- timed segments with valid `start_seconds`, `end_seconds`, and text;
- detected `transcript_language` when available;
- `transcript_source: local_faster_whisper`;
- `transcript_is_generated: true`;
- `media.downloaded: true` and `media.kind: audio`;
- `media.sent_to_cloud: false`;
- `media.cleanup_status: deleted` and `media.retained: false` by default;
- `run.cache.status: bypassed` when caching is enabled.

Use `--keep-asr-audio` only for an explicit debugging request. In that mode require `media.cleanup_status: retained` and do not delete the current-run audio during final cleanup.

## Failure handling

- `asr_dependency_missing`: install the optional `asr` extra locally.
- `asr_model_required`: report cost and wait for model-download confirmation.
- `asr_model_download_failed` / `asr_model_load_failed`: report the local model stage; do not download audio or call cloud ASR.
- `disk_space_insufficient`: select larger local model or temporary directories.
- `audio_metadata_failed` / `audio_download_failed`: report yt-dlp audio stage diagnostics after redaction.
- `audio_decode_failed`: report local decode failure and suggest updating the optional ASR environment or checking the source audio.
- `asr_empty_transcript`: say that local speech recognition found no usable timed speech.
- `asr_transcript_quality_failed`: do not summarize corrupted replacement-character output; suggest a larger model or clearer source audio.
- `media_cleanup_failed`: report the exact current-run directory for manual cleanup, without deleting any broader path.

Never include cookies, authorization headers, signed media URLs, browser paths, or model-host tokens in evidence, logs, or the digest.
