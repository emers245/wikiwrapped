"""Record the embedding-consistency fixture (app/bundle_fixture.json).

    python scripts/make_embedding_fixture.py

Run in the environment that builds the reference space, with the bundled
model in place (scripts/fetch_model.py), and only when the model revision
or the embedding stack changes on purpose. The installed app and the tests
re-encode these texts and compare against the stored vectors
(app/bundle.py, COSINE_THRESHOLD), which is what catches an installer whose
model or libraries quietly differ from what built the map.

The texts are fixed rather than fetched: an article's embedding depends on
its live Wikipedia summary, which changes, so only fixed text gives a
stable expected vector. They are written like the summaries the app embeds
(description + extract) and span unrelated topics, so a wrong model cannot
match all of them by luck.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Standalone script: make the repo root importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
from app import bundle, embeddings  # noqa: E402

TEXTS = [
    "English author and humorist Douglas Adams was an English author, "
    "humourist and screenwriter, best known for The Hitchhiker's Guide to the Galaxy.",
    "Chemical element with atomic number 79 Gold is a chemical element with the "
    "symbol Au. It is a bright, slightly orange-yellow, dense, soft, malleable metal.",
    "Capital city of France Paris is the capital and largest city of France, "
    "on the river Seine in the north of the country.",
    "Branch of mathematics Topology is the study of properties of geometric objects "
    "that are preserved under continuous deformations.",
    "Association football club Liverpool Football Club is a professional football "
    "club based in Liverpool, England.",
    "Family of flowering plants The Rosaceae, the rose family, is a medium-sized "
    "family of flowering plants including apples, pears, cherries and roses.",
    "1962 confrontation The Cuban Missile Crisis was a 13-day confrontation between "
    "the governments of the United States and the Soviet Union.",
    "Programming language Python is a high-level, general-purpose programming "
    "language whose design philosophy emphasizes code readability.",
]


def main() -> int:
    if not (config.EMBEDDING_MODEL_DIR / "modules.json").is_file():
        raise SystemExit("Fetch the bundled model first: python scripts/fetch_model.py")
    model = embeddings.get_model()
    vectors = model.encode(TEXTS, convert_to_numpy=True, normalize_embeddings=True,
                           show_progress_bar=False)
    fixture = {
        "model": config.EMBEDDING_MODEL,
        "revision": config.EMBEDDING_MODEL_REVISION,
        "made_with": bundle.runtime_versions(),
        "texts": TEXTS,
        # float32 round-trips exactly through 9 significant digits.
        "vectors": [[float(f"{x:.9g}") for x in row] for row in vectors],
    }
    bundle.FIXTURE.write_text(json.dumps(fixture, indent=1) + "\n", encoding="utf-8")
    print(f"Wrote {bundle.FIXTURE} ({len(TEXTS)} texts x {vectors.shape[1]} dims)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
