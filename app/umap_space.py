"""Project visited articles into the frozen UMAP reference space (repo_spec
§7.5, D4).

Loads the reducer fitted once by scripts/build_reference_space.py and
.transform()s visited-article embeddings into that fixed space — never
refits. Coordinates stored in umap_coords. Raises with a pointer to the
build script if reference artifacts are missing.

Implemented in Step 8.
"""
