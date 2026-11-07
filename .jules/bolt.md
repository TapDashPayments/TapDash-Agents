## 2026-10-22 - Single-pass argument normalization avoids deepcopy overhead
**Learning:** `copy.deepcopy()` on nested argument dicts before normalizing and computing `args_digest` incurred significant unnecessary overhead due to Python's generic reflection in `deepcopy`. Performing structural copying, scope adjustment, and float-to-int conversion in a single custom recursive pass achieved a 3-4x speedup.
**Action:** For performance-critical hashing/normalization paths operating on JSON-serializable dictionaries, write single-pass copy/transform utilities instead of generic `copy.deepcopy()`.

## 2026-10-23 - Eliminating redundant deepcopy calls in argument processing
**Learning:** Calling `copy.deepcopy()` before functions like `_redact()` that ALREADY recursively construct brand new dicts/lists is entirely redundant and slows down execution. Furthermore, in helper functions like `lift_gateway_context()`, checking whether any context key actually exists before invoking `copy.deepcopy()` skips deepcopying entirely for ~99% of tool calls, yielding a ~36x speedup.
**Action:** Before calling `copy.deepcopy()`, check if the operation itself produces new containers during recursion or if a fast-path condition can bypass copying when no transformation is needed.

## 2026-10-28 - Fast-path short-circuiting for frequent key pattern checks
**Learning:** Checking whether arbitrary dictionary keys match special metadata field patterns (like `withhuman_context`) via regex replacement on every key during recursive walks creates unnecessary regex overhead. Since `"withhumancontext"` is 16 alphanumeric characters, checking `key == CONTEXT_FIELD` and short-circuiting on `len(key) < 16` bypasses regex execution for >95% of typical dictionary keys, achieving a ~3.1x speedup.
**Action:** For pattern checks on dictionary keys during payload walks, add fast-path checks for exact matches and minimum possible length before executing regular expressions.
