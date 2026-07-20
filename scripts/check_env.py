"""Environment smoke test (repo_spec S9): must pass before any other work.

Verifies imports of the fragile umap/numba/llvmlite cluster plus the other
core dependencies, and that numba can actually JIT-compile on this machine.

Usage:
    python scripts/check_env.py          # imports + numba JIT check
    python scripts/check_env.py --full   # also fit-transforms a tiny UMAP
"""

import importlib
import sqlite3
import sys

CHECKS = [
    "numpy",
    "numba",
    "llvmlite",
    "umap",
    "sklearn",
    "torch",
    "sentence_transformers",
    "pandas",
    "requests",
    "tqdm",
    "pyarrow",
    "tzlocal",
    "fastapi",
    "uvicorn",
    "httpx",
    "pytest",
]


def main() -> int:
    failed = []
    for name in CHECKS:
        try:
            mod = importlib.import_module(name)
            version = getattr(mod, "__version__", "?")
            print(f"  ok  {name:<22} {version}")
        except Exception as exc:  # noqa: BLE001 - report every failure kind
            print(f"FAIL  {name:<22} {exc}")
            failed.append(name)

    print(f"  ok  sqlite3 (stdlib)      {sqlite3.sqlite_version}")

    if not failed:
        # Imports alone don't prove llvmlite works - force a JIT compile.
        import numba
        import numpy as np

        @numba.njit
        def _jit_probe(x):
            return x + 1

        assert _jit_probe(np.int64(41)) == 42
        print("  ok  numba JIT compile")

        if "--full" in sys.argv:
            import umap

            rng = np.random.RandomState(42)
            coords = umap.UMAP(n_neighbors=5, random_state=42).fit_transform(
                rng.rand(30, 8)
            )
            assert coords.shape == (30, 2)
            print("  ok  umap fit_transform (30x8 toy data)")

    if failed:
        print(f"\nEnvironment NOT ready - failed imports: {', '.join(failed)}")
        return 1
    print("\nEnvironment ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
