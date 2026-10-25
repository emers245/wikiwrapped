"""Download the sentence-transformers model at its pinned revision, as a
bundled resource.

    python scripts/fetch_model.py                      # into the dev resources (data/models/)
    python scripts/fetch_model.py --dest packaging/build/resources
    python scripts/fetch_model.py --record             # maintainer: re-pin checksums

The installed app never contacts Hugging Face: it loads the model from the
copy this script puts in <resources>/models/all-MiniLM-L6-v2/ (see
config.EMBEDDING_MODEL_DIR and app/embeddings.py). This is the one place
the files are fetched, at build time.

Two pins, so the shipped model is exactly the one the reference space was
built with:

- config.EMBEDDING_MODEL_REVISION, the Hugging Face commit to download.
- packaging/model.json, the expected sha256 of every file at that commit.
  Downloads are checked against it, so a changed or corrupted file stops
  the build instead of shipping. Use --record (once, after deliberately
  bumping the revision) to rewrite it from a fresh download.

Only the files a local load needs are fetched: the repo also holds ONNX,
OpenVINO, TensorFlow and Rust copies of the weights, which would add
hundreds of MB for nothing.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

# Standalone script: make the repo root importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
from app import bundle  # noqa: E402

PINS = config.PROJECT_ROOT / "packaging" / "model.json"

# Everything SentenceTransformer(<local folder>) reads for this model: the
# transformer weights and config, the tokenizer, and the pooling module's
# config. (Its third module, Normalize, has no files.)
FILES = (
    "config.json",
    "config_sentence_transformers.json",
    "model.safetensors",
    "modules.json",
    "sentence_bert_config.json",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.txt",
    "1_Pooling/config.json",
)


def download(dest: Path) -> None:
    """Fetch FILES at the pinned revision into dest (replacing it).

    Through a temp folder: huggingface_hub keeps its own bookkeeping folder
    inside whatever local_dir it is given, which must not end up in the
    bundle (or in the manifest's file list).
    """
    from huggingface_hub import hf_hub_download

    with tempfile.TemporaryDirectory(prefix="wikiwrapped-model-") as tmp:
        for name in FILES:
            hf_hub_download(
                repo_id=config.EMBEDDING_MODEL,
                filename=name,
                revision=config.EMBEDDING_MODEL_REVISION,
                local_dir=tmp,
            )
            print(f"  fetched {name}")
        if dest.exists():
            shutil.rmtree(dest)
        for name in FILES:
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path(tmp) / name, target)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--dest", type=Path, default=None,
        help="resources root to put models/ under (default: the dev "
             f"resources, {config.resource_dir()})",
    )
    parser.add_argument(
        "--record", action="store_true",
        help="rewrite packaging/model.json from this download (after bumping "
             "config.EMBEDDING_MODEL_REVISION on purpose)",
    )
    args = parser.parse_args(argv)

    root = Path(args.dest) if args.dest else config.resource_dir()
    dest = root / "models" / config.EMBEDDING_MODEL_DIR.name
    print(f"{config.EMBEDDING_MODEL} @ {config.EMBEDDING_MODEL_REVISION[:12]} -> {dest}")
    download(dest)
    hashes = bundle.file_hashes(dest)

    if args.record:
        PINS.write_text(json.dumps({
            "repo": config.EMBEDDING_MODEL,
            "revision": config.EMBEDDING_MODEL_REVISION,
            "files": hashes,
        }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"Recorded {len(hashes)} checksums in {PINS}")
    else:
        pins = json.loads(PINS.read_text(encoding="utf-8"))
        if pins["revision"] != config.EMBEDDING_MODEL_REVISION:
            raise SystemExit(
                f"{PINS} pins revision {pins['revision']} but config has "
                f"{config.EMBEDDING_MODEL_REVISION}; re-run with --record if "
                "the bump was intended."
            )
        if hashes != pins["files"]:
            bad = sorted(set(hashes.items()) ^ set(pins["files"].items()))
            raise SystemExit(f"Model files do not match {PINS}: {bad}")
        print(f"Verified {len(hashes)} files against {PINS.name}")

    bundle.write_manifest(
        dest, kind="model", repo=config.EMBEDDING_MODEL,
        revision=config.EMBEDDING_MODEL_REVISION,
    )
    size = sum(p.stat().st_size for p in dest.rglob("*") if p.is_file())
    print(f"Wrote {dest} ({size / 1e6:.1f} MB) with {bundle.MANIFEST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
