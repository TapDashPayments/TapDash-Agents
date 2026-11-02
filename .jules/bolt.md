## 2026-10-22 - Single-pass argument normalization avoids deepcopy overhead
**Learning:** `copy.deepcopy()` on nested argument dicts before normalizing and computing `args_digest` incurred significant unnecessary overhead due to Python's generic reflection in `deepcopy`. Performing structural copying, scope adjustment, and float-to-int conversion in a single custom recursive pass achieved a 3-4x speedup.
**Action:** For performance-critical hashing/normalization paths operating on JSON-serializable dictionaries, write single-pass copy/transform utilities instead of generic `copy.deepcopy()`.

## 2026-10-23 - Eliminating redundant deepcopy calls in argument processing
**Learning:** Calling `copy.deepcopy()` before functions like `_redact()` that ALREADY recursively construct brand new dicts/lists is entirely redundant and slows down execution. Furthermore, in helper functions like `lift_gateway_context()`, checking whether any context key actually exists before invoking `copy.deepcopy()` skips deepcopying entirely for ~99% of tool calls, yielding a ~36x speedup.
**Action:** Before calling `copy.deepcopy()`, check if the operation itself produces new containers during recursion or if a fast-path condition can bypass copying when no transformation is needed.

## 2026-10-28 - Fast-path check bypasses regex normalization for note keys
**Learning:** In `gatecall.py`, `is_note_key` is evaluated for every dictionary key visited when scanning arguments for leftover context notes. The canonical target `"withhumancontext"` contains 16 alphanumeric characters; any key shorter than 16 characters or not a string can never match, and an exact match against `CONTEXT_FIELD` (`"withhuman_context"`) can be checked directly before falling back to regex `_NOT_ALNUM.sub("", key.lower())`. This fast-path check yields a ~3x speedup on key checks and ~1.6x speedup during payload tree walks.
**Action:** When performing regex-based normalization against a known constant string, check length lower bounds and common exact string matches first to bypass regex overhead.
