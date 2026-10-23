## 2026-10-22 - Single-pass argument normalization avoids deepcopy overhead
**Learning:** `copy.deepcopy()` on nested argument dicts before normalizing and computing `args_digest` incurred significant unnecessary overhead due to Python's generic reflection in `deepcopy`. Performing structural copying, scope adjustment, and float-to-int conversion in a single custom recursive pass achieved a 3-4x speedup.
**Action:** For performance-critical hashing/normalization paths operating on JSON-serializable dictionaries, write single-pass copy/transform utilities instead of generic `copy.deepcopy()`.
