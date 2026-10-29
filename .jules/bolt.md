## 2026-10-22 - Single-pass argument normalization avoids deepcopy overhead
**Learning:** `copy.deepcopy()` on nested argument dicts before normalizing and computing `args_digest` incurred significant unnecessary overhead due to Python's generic reflection in `deepcopy`. Performing structural copying, scope adjustment, and float-to-int conversion in a single custom recursive pass achieved a 3-4x speedup.
**Action:** For performance-critical hashing/normalization paths operating on JSON-serializable dictionaries, write single-pass copy/transform utilities instead of generic `copy.deepcopy()`.

## 2026-10-23 - Eliminating redundant deepcopy calls in argument processing
**Learning:** Calling `copy.deepcopy()` before functions like `_redact()` that ALREADY recursively construct brand new dicts/lists is entirely redundant and slows down execution. Furthermore, in helper functions like `lift_gateway_context()`, checking whether any context key actually exists before invoking `copy.deepcopy()` skips deepcopying entirely for ~99% of tool calls, yielding a ~36x speedup.
**Action:** Before calling `copy.deepcopy()`, check if the operation itself produces new containers during recursion or if a fast-path condition can bypass copying when no transformation is needed.

## 2026-03-30 - Length fast-paths for string regexes vs tuple substring iteration
**Learning:** Checking string length or exact identity before running regex replacement (`_NOT_ALNUM.sub`) in hot path functions like `is_note_key()` yields ~1.58x speedup because `len(key)` is a zero-cost C-level attribute lookup (`PyUnicode_GET_LENGTH`). Conversely, attempting manual `any(sub in key_lower for sub in tuple)` substring pre-checks in Python bytecode was 0.45x slower than direct C-compiled `re.search` due to tuple iteration and string allocation overhead.
**Action:** Use simple integer length bounds (`len(s) < N`) or direct equality (`s == TARGET`) as regex fast-paths, but prefer compiled `re.search` over manual Python substring loops for multi-pattern checks.
