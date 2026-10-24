## 2026-10-22 - Single-pass argument normalization avoids deepcopy overhead
**Learning:** `copy.deepcopy()` on nested argument dicts before normalizing and computing `args_digest` incurred significant unnecessary overhead due to Python's generic reflection in `deepcopy`. Performing structural copying, scope adjustment, and float-to-int conversion in a single custom recursive pass achieved a 3-4x speedup.
**Action:** For performance-critical hashing/normalization paths operating on JSON-serializable dictionaries, write single-pass copy/transform utilities instead of generic `copy.deepcopy()`.

## 2026-10-23 - Eliminating redundant deepcopies in argument processing hot paths
**Learning:** Functions like `redact_arguments` and `lift_gateway_context` were allocating full `copy.deepcopy()` objects prior to secondary transformations. Since `_redact` already constructs new containers recursively and context lifting is only needed when nested `withhuman_context` dictionaries are present, pre-allocating deep copies wasted 70-90% of processing time on Python object reflection.
**Action:** Trust recursive structural constructors to handle container isolation instead of pre-copying with `deepcopy()`, and guard deep allocations with fast-path presence checks (`any(isinstance(v, dict) and KEY in v ...)`).
